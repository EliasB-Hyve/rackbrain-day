import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List


DEFAULT_TIMER_DB_PATH = os.path.join("state", "rackbrain_state.sqlite")


@dataclass(frozen=True)
class TimerRecord:
    issue_key: str
    rule_id: str
    rearm_key: str
    started_at: float
    duration_seconds: int
    state: str  # "active" | "expired"

    @property
    def expires_at(self) -> float:
        return float(self.started_at) + float(self.duration_seconds)

    def seconds_remaining(self, now: Optional[float] = None) -> float:
        if now is None:
            now = time.time()
        return max(0.0, self.expires_at - float(now))


def _resolve_db_path(processing_config: Optional[dict]) -> str:
    if isinstance(processing_config, dict):
        path = processing_config.get("timer_db_path") or processing_config.get("state_db_path")
        if isinstance(path, str) and path.strip():
            return path.strip()

    env_path = os.environ.get("RACKBRAIN_TIMER_DB_PATH", "").strip()
    if env_path:
        return env_path

    home = os.environ.get("RACKBRAIN_HOME", "").strip()
    if home:
        return os.path.join(home, "state", "rackbrain_state.sqlite")
    return DEFAULT_TIMER_DB_PATH


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS timers (
            issue_key TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            rearm_key TEXT NOT NULL,
            started_at REAL NOT NULL,
            duration_seconds INTEGER NOT NULL,
            state TEXT NOT NULL,
            PRIMARY KEY (issue_key, rule_id, rearm_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_timers_issue_state ON timers(issue_key, state)"
    )
    conn.commit()


class TimerStore:
    """
    Simple persistent timer store.

    Semantics:
    - Any active timer for an issue_key blocks *all* rule matching for that ticket.
    - When a timer expires, it is kept as state=expired to suppress re-running the
      same timer rule in a loop.
    - The expired suppression clears automatically when the ticket's "rearm_key"
      changes (typically assignee/status change).
    """

    def __init__(self, processing_config: Optional[dict] = None) -> None:
        self.db_path = _resolve_db_path(processing_config)

    @staticmethod
    def build_rearm_key(status: Optional[str], assignee: Optional[str]) -> str:
        return f"assignee={assignee or ''}|status={status or ''}"

    def _fetch_timer(
        self, issue_key: str, rule_id: str, rearm_key: str
    ) -> Optional[TimerRecord]:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT issue_key, rule_id, rearm_key, started_at, duration_seconds, state
                FROM timers
                WHERE issue_key=? AND rule_id=? AND rearm_key=?
                """,
                (issue_key, rule_id, rearm_key),
            ).fetchone()
        if not row:
            return None
        return TimerRecord(
            issue_key=row[0],
            rule_id=row[1],
            rearm_key=row[2],
            started_at=float(row[3]),
            duration_seconds=int(row[4]),
            state=row[5],
        )

    def cleanup_expired(self, issue_key: str, current_rearm_key: str) -> None:
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                DELETE FROM timers
                WHERE issue_key=?
                  AND state='expired'
                  AND rearm_key<>?
                """,
                (issue_key, current_rearm_key),
            )
            conn.commit()

    def get_active_timer(self, issue_key: str, now: Optional[float] = None) -> Optional[TimerRecord]:
        if now is None:
            now = time.time()

        active: List[TimerRecord] = []
        expired_rows: List[Tuple[str, str, str]] = []

        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT issue_key, rule_id, rearm_key, started_at, duration_seconds, state
                FROM timers
                WHERE issue_key=? AND state='active'
                """,
                (issue_key,),
            ).fetchall()

            for row in rows:
                rec = TimerRecord(
                    issue_key=row[0],
                    rule_id=row[1],
                    rearm_key=row[2],
                    started_at=float(row[3]),
                    duration_seconds=int(row[4]),
                    state=row[5],
                )
                if rec.expires_at <= float(now):
                    expired_rows.append((rec.issue_key, rec.rule_id, rec.rearm_key))
                else:
                    active.append(rec)

            if expired_rows:
                conn.executemany(
                    """
                    UPDATE timers
                    SET state='expired'
                    WHERE issue_key=? AND rule_id=? AND rearm_key=?
                    """,
                    expired_rows,
                )
                conn.commit()

        if not active:
            return None
        # Return the one that expires soonest (best message)
        return min(active, key=lambda r: r.expires_at)

    def is_rule_suppressed(self, issue_key: str, rule_id: str, current_rearm_key: str) -> bool:
        rec = self._fetch_timer(issue_key, rule_id, current_rearm_key)
        if not rec:
            return False

        if rec.state == "active":
            # If it's expired but not yet marked, flip it now.
            if rec.expires_at <= time.time():
                with _connect(self.db_path) as conn:
                    conn.execute(
                        """
                        UPDATE timers
                        SET state='expired'
                        WHERE issue_key=? AND rule_id=? AND rearm_key=?
                        """,
                        (issue_key, rule_id, current_rearm_key),
                    )
                    conn.commit()
                return True
            return True

        # expired
        return True

    def start_timer(
        self,
        issue_key: str,
        rule_id: str,
        seconds: int,
        rearm_key: str,
        now: Optional[float] = None,
    ) -> TimerRecord:
        if now is None:
            now = time.time()

        seconds_int = int(seconds)
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO timers(issue_key, rule_id, rearm_key, started_at, duration_seconds, state)
                VALUES(?, ?, ?, ?, ?, 'active')
                """,
                (issue_key, rule_id, rearm_key, float(now), seconds_int),
            )
            conn.commit()

        return TimerRecord(
            issue_key=issue_key,
            rule_id=rule_id,
            rearm_key=rearm_key,
            started_at=float(now),
            duration_seconds=seconds_int,
            state="active",
        )

    def list_expired_rule_ids(self, issue_key: str, current_rearm_key: str) -> List[str]:
        """
        Return rule ids with expired timers for this ticket under the current rearm_key.

        This is used for workflow follow-ups that should run only after a prior rule's
        timer has elapsed, without relying on Jira comment markers.
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT rule_id
                FROM timers
                WHERE issue_key=? AND rearm_key=? AND state='expired'
                """,
                (issue_key, current_rearm_key),
            ).fetchall()
        return sorted({str(r[0]) for r in rows if r and r[0]})
