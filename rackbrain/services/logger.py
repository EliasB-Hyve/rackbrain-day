# rackbrain/services/logger.py

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List
import threading
import re
from contextlib import contextmanager


class ProcessingLogger:
    """
    Logger for tracking processed tickets.
    Supports both JSON and text formats.
    """

    def __init__(
        self,
        log_dir: str = "logs",
        log_file: str = "rackbrain_processed.log",
        log_format: str = "json",
        rotate_daily: bool = True,
    ):
        self.log_dir = Path(log_dir)
        self.log_file = log_file
        self.log_format = log_format.lower()
        self.rotate_daily = rotate_daily

        # Create log directory if it doesn't exist
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _get_log_path(self) -> Path:
        """Get the full path to the log file, with date rotation if enabled."""
        if self.rotate_daily:
            date_str = datetime.now().strftime("%Y-%m-%d")
            name, ext = os.path.splitext(self.log_file)
            filename = f"{name}_{date_str}{ext}"
        else:
            filename = self.log_file
        return self.log_dir / filename

    def log_processed(
        self,
        issue_key: str,
        rule_id: Optional[str] = None,
        rule_name: Optional[str] = None,
        confidence: Optional[float] = None,
        success: bool = True,
        error: Optional[str] = None,
        dry_run: bool = False,
        actions_taken: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log a processed ticket.

        Args:
            issue_key: Jira ticket key (e.g., "MFGS-123456")
            rule_id: Rule ID that matched
            rule_name: Rule name that matched
            confidence: Match confidence score
            success: Whether processing succeeded
            error: Error message if failed
            dry_run: Whether this was a dry run
            actions_taken: Dict of actions taken (assigned, transitioned, commented, etc.)
        """
        timestamp = datetime.now().isoformat()

        if self.log_format == "json":
            log_entry = {
                "timestamp": timestamp,
                "issue_key": issue_key,
                "rule_id": rule_id,
                "rule_name": rule_name,
                "confidence": confidence,
                "success": success,
                "error": error,
                "dry_run": dry_run,
                "actions_taken": actions_taken or {},
            }
            log_line = json.dumps(log_entry, ensure_ascii=False)
        else:
            # Text format
            status = "DRY-RUN" if dry_run else ("SUCCESS" if success else "FAILED")
            log_parts = [
                timestamp,
                status,
                issue_key,
            ]
            if rule_id:
                log_parts.append(f"rule={rule_id}")
            if confidence is not None:
                log_parts.append(f"conf={confidence:.2f}")
            if error:
                log_parts.append(f"error={error}")
            if actions_taken:
                actions_str = ", ".join([f"{k}={v}" for k, v in actions_taken.items() if v])
                if actions_str:
                    log_parts.append(f"actions=[{actions_str}]")

            log_line = " | ".join(log_parts)

        # Append to log file
        log_path = self._get_log_path()
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            # Don't fail processing if logging fails
            print(f"[WARN] Failed to write to log file {log_path}: {e}")

    def log_no_match(self, issue_key: str, dry_run: bool = False) -> None:
        """Log a ticket that didn't match any rules."""
        self.log_processed(
            issue_key=issue_key,
            success=True,  # Not a failure, just no match
            dry_run=dry_run,
            actions_taken={"action": "no_match"},
        )


# Global logger instance (will be initialized from config)
_logger_instance: Optional[ProcessingLogger] = None
_rule_match_history_logger_instance: Optional["RuleMatchHistoryLogger"] = None


def get_logger() -> Optional[ProcessingLogger]:
    """Get the global logger instance."""
    return _logger_instance


def get_rule_match_history_logger() -> Optional["RuleMatchHistoryLogger"]:
    """Get the global rule match history logger instance."""
    return _rule_match_history_logger_instance


@contextmanager
def _best_effort_file_lock(file_obj):
    """
    Best-effort cross-platform exclusive file lock.

    Intended to reduce corruption when multiple RackBrain workers write the same history file.
    """
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore

            try:
                file_obj.seek(0)
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
                yield
            finally:
                try:
                    file_obj.seek(0)
                    msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
        else:
            import fcntl  # type: ignore

            try:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception:
        yield


class RuleMatchHistoryLogger:
    """
    Append-only (per-rule) history of which tickets matched which rule IDs.

    File format:
      === <rule_id> ===
      YYYY-MM-DD <TICKETKEY>
      YYYY-MM-DD <TICKETKEY>

    One blank line is kept between sections for readability.
    """

    _SECTION_RE = re.compile(r"^===\s*(?P<rule_id>.+?)\s*===\s*$")
    _ENTRY_RE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})\s+(?P<issue>\S+)\s*$")
    _HEADER_LINES = [
        "# RackBrain rule match history",
        "# Sections are grouped by rule_id; entries are 'YYYY-MM-DD TICKETKEY'.",
        "",
    ]

    def __init__(
        self,
        log_dir: str = "logs",
        history_file: str = "rackbrain_rule_matches.txt",
        enabled: bool = True,
        include_dry_runs: bool = False,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.history_file = history_file
        self.enabled = enabled
        self.include_dry_runs = include_dry_runs
        self._lock = threading.Lock()

        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.enabled:
            self._ensure_initialized_file()

    def _get_path(self) -> Path:
        return self.log_dir / self.history_file

    def _ensure_initialized_file(self) -> None:
        """
        Ensure the history file exists and has a small human-readable header.

        This makes it obvious where to look even before any rule matches occur.
        """
        path = self._get_path()
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    if path.exists() and path.stat().st_size > 0:
                        return
                except OSError:
                    pass

                with open(path, "a+", encoding="utf-8") as f:
                    with _best_effort_file_lock(f):
                        f.seek(0)
                        existing = f.read()
                        if isinstance(existing, str) and existing.strip():
                            return

                        out = "\n".join(self._HEADER_LINES).rstrip("\n") + "\n"
                        f.seek(0)
                        f.truncate(0)
                        f.write(out)
        except Exception as e:
            print(f"[WARN] Failed to initialize rule match history log {path}: {e}")

    @staticmethod
    def _normalize_lines(text: str) -> List[str]:
        # Keep line structure stable across Windows/macOS/Linux.
        return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def log_match(self, *, rule_id: str, issue_key: str, dry_run: bool = False) -> None:
        if not self.enabled:
            return
        if dry_run and not self.include_dry_runs:
            return
        if not rule_id or not issue_key:
            return

        day = datetime.now().strftime("%Y-%m-%d")
        header = f"=== {rule_id} ==="
        entry = f"{day} {issue_key}"

        path = self._get_path()
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a+", encoding="utf-8") as f:
                    with _best_effort_file_lock(f):
                        f.seek(0)
                        existing = f.read()
                        lines = self._normalize_lines(existing)

                        if lines and lines[-1] == "":
                            # Trailing empty split element from final newline; simplify editing.
                            lines = lines[:-1]

                        # Ensure file has a small header if empty (human-readable only).
                        if not lines:
                            lines = list(self._HEADER_LINES)

                        # Locate section and collect existing issue keys for that rule.
                        section_start = None
                        for idx, line in enumerate(lines):
                            if line.strip() == header:
                                section_start = idx
                                break

                        if section_start is None:
                            # Append a new section at the end.
                            if lines and lines[-1].strip() != "":
                                lines.append("")
                            lines.extend([header, entry, ""])
                        else:
                            # Find end of section (next header or EOF).
                            section_end = len(lines)
                            for j in range(section_start + 1, len(lines)):
                                if self._SECTION_RE.match(lines[j].strip()):
                                    section_end = j
                                    break

                            existing_issues = set()
                            for line in lines[section_start + 1 : section_end]:
                                m = self._ENTRY_RE.match(line.strip())
                                if m:
                                    existing_issues.add(m.group("issue"))

                            if issue_key in existing_issues:
                                return

                            # Insert before trailing blank lines in the section.
                            insert_at = section_end
                            while insert_at > section_start + 1 and lines[insert_at - 1].strip() == "":
                                insert_at -= 1
                            lines.insert(insert_at, entry)

                            # Ensure a blank line after the section for readability.
                            if insert_at == section_end and (section_end == len(lines) or lines[section_end].strip() != ""):
                                lines.insert(insert_at + 1, "")

                        # Rewrite the full file (keeps per-rule grouping).
                        out = "\n".join(lines).rstrip("\n") + "\n"
                        f.seek(0)
                        f.truncate(0)
                        f.write(out)
        except Exception as e:
            print(f"[WARN] Failed to write rule match history log {path}: {e}")


def init_logger(config: Dict[str, Any]) -> Optional[ProcessingLogger]:
    """
    Initialize the global logger from config.

    Args:
        config: Config dict with 'logging' section

    Returns:
        ProcessingLogger instance or None if logging disabled
    """
    global _logger_instance
    global _rule_match_history_logger_instance

    logging_cfg = config.get("logging", {})
    if not logging_cfg.get("enabled", True):
        _logger_instance = None
        _rule_match_history_logger_instance = None
        return None

    _logger_instance = ProcessingLogger(
        log_dir=logging_cfg.get("log_dir", "logs"),
        log_file=logging_cfg.get("log_file", "rackbrain_processed.log"),
        log_format=logging_cfg.get("log_format", "json"),
        rotate_daily=logging_cfg.get("rotate_daily", True),
    )

    history_cfg = (logging_cfg.get("rule_match_history", {}) or {})
    _rule_match_history_logger_instance = RuleMatchHistoryLogger(
        log_dir=logging_cfg.get("log_dir", "logs"),
        history_file=history_cfg.get("file", "rackbrain_rule_matches.txt"),
        enabled=history_cfg.get("enabled", True),
        include_dry_runs=history_cfg.get("include_dry_runs", False),
    )

    return _logger_instance

