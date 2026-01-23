import os
from typing import Any, Dict, List, Optional

import requests


class JiraClient:
    """
    Jira client using PAT/Bearer auth.
    Authorization: Bearer <PAT>
    """

    def __init__(
        self,
        *,
        base_url: str,
        pat: Optional[str] = None,
        pat_env: str = "RACKBRAIN_JIRA_PAT",
        timeout_seconds: int = 30,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise RuntimeError("Missing Jira base_url.")

        if pat is None or not str(pat).strip():
            pat = os.environ.get(pat_env, "")
        pat = str(pat).strip()
        if not pat:
            raise RuntimeError(
                "Missing Jira PAT. Set jira.pat in config/config.yaml "
                f"or set the environment variable {pat_env}."
            )

        self.timeout_seconds = int(timeout_seconds)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _raise_for_status(self, resp: requests.Response, *, context: str) -> None:
        if resp.status_code == 401:
            raise RuntimeError(f"Jira 401 Unauthorized — check PAT/permissions. ({context})")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Jira API error ({context}). HTTP {resp.status_code}: {resp.text}"
            )

    # ---------------------------
    # Core issue methods
    # ---------------------------

    def get_issue(self, key: str, fields: Optional[List[str]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        resp = self.session.get(
            self._url(f"/rest/api/2/issue/{key}"),
            params=params,
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(resp, context=f"get_issue({key})")
        return resp.json()

    def add_comment(self, key: str, body: str) -> None:
        resp = self.session.post(
            self._url(f"/rest/api/2/issue/{key}/comment"),
            json={"body": body},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(resp, context=f"add_comment({key})")

    def get_issue_comments(
        self,
        key: str,
        start_at: int = 0,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        params = {
            "startAt": int(start_at),
            "maxResults": int(max_results),
        }
        resp = self.session.get(
            self._url(f"/rest/api/2/issue/{key}/comment"),
            params=params,
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(resp, context=f"get_issue_comments({key})")
        return resp.json()

    # ---------------------------
    # Transitions
    # ---------------------------

    def get_transitions(self, key: str) -> List[Dict[str, Any]]:
        resp = self.session.get(
            self._url(f"/rest/api/2/issue/{key}/transitions"),
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(resp, context=f"get_transitions({key})")
        data = resp.json() or {}
        return data.get("transitions", []) or []

    # alias
    def list_transitions(self, key: str) -> List[Dict[str, Any]]:
        return self.get_transitions(key)

    def do_transition(
        self,
        key: str,
        transition_id: str,
        comment_body: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Transition an issue by transition ID.
        Optionally include:
          - comment_body (update.comment add)
          - fields (e.g., resolution)
        """
        payload: Dict[str, Any] = {"transition": {"id": str(transition_id)}}

        if fields:
            payload["fields"] = fields

        if comment_body:
            payload.setdefault("update", {})
            payload["update"]["comment"] = [{"add": {"body": comment_body}}]

        resp = self.session.post(
            self._url(f"/rest/api/2/issue/{key}/transitions"),
            json=payload,
            timeout=self.timeout_seconds,
        )

        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        # Jira often returns 204 No Content on success.
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to transition {key} using ID {transition_id}. "
                f"HTTP {resp.status_code}: {resp.text}"
            )

    def transition_issue(self, key: str, transition_id: str, fields: Optional[Dict[str, Any]] = None) -> None:
        """
        Backwards-compatible wrapper used by ticket_processor.
        """
        self.do_transition(key, transition_id, comment_body=None, fields=fields)

    def do_transition_by_name(
        self,
        key: str,
        transition_name: str,
        comment_body: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Try to transition issue by human-readable name (e.g. 'In Progress', 'Done').
        Returns True if transitioned, False if not found.
        """
        name_norm = (transition_name or "").strip().lower()
        if not name_norm:
            return False

        transitions = self.get_transitions(key)
        for t in transitions:
            nm = (t.get("name") or "").strip().lower()
            if nm == name_norm:
                tid = str(t.get("id") or "").strip()
                if tid:
                    self.do_transition(key, tid, comment_body=comment_body, fields=fields)
                    return True
        return False

    # ---------------------------
    # Assignee
    # ---------------------------

    def assign_issue(self, key: str, username_or_accountid: str) -> None:
        """
        Assign the Jira issue.

        Jira Server/DC usually accepts:
          PUT .../assignee  {"name": "<username>"}

        Jira Cloud (and some instances) require:
          {"accountId": "<id>"}

        This method tries "name" first, then falls back to "accountId".
        """
        val = (username_or_accountid or "").strip()
        if not val:
            raise RuntimeError("assign_issue: empty assignee value")

        url = self._url(f"/rest/api/2/issue/{key}/assignee")

        # 1) Try legacy 'name'
        resp = self.session.put(url, json={"name": val}, timeout=self.timeout_seconds)
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        # If not 400/404 etc, raise immediately
        if resp.status_code not in (400, 404):
            raise RuntimeError(
                f"Failed to assign {key} to {val} using name. HTTP {resp.status_code}: {resp.text}"
            )

        # 2) Fallback to accountId
        resp2 = self.session.put(url, json={"accountId": val}, timeout=self.timeout_seconds)
        if resp2.status_code in (200, 204):
            return
        if resp2.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        raise RuntimeError(
            f"Failed to assign {key} to {val}. "
            f"name attempt: HTTP {resp.status_code}: {resp.text} | "
            f"accountId attempt: HTTP {resp2.status_code}: {resp2.text}"
        )

    # ---------------------------
    # Search
    # ---------------------------

    def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        max_results: int = 200,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "jql": jql,
            "maxResults": int(max_results),
        }
        if fields:
            payload["fields"] = fields

        resp = self.session.post(
            self._url("/rest/api/2/search"),
            json=payload,
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(resp, context="search_issues")
        data = resp.json() or {}
        return data.get("issues", []) or []

    # ---------------------------
    # Issue links
    # ---------------------------

    def create_issue_link(
        self,
        *,
        link_type_name: str,
        inward_issue_key: str,
        outward_issue_key: str,
    ) -> None:
        payload: Dict[str, Any] = {
            "type": {"name": link_type_name},
            "inwardIssue": {"key": inward_issue_key},
            "outwardIssue": {"key": outward_issue_key},
        }

        resp = self.session.post(
            self._url("/rest/api/2/issueLink"),
            json=payload,
            timeout=self.timeout_seconds,
        )
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized - check PAT/permissions.")
        if resp.status_code == 400:
            body = (resp.text or "").lower()
            if "issue link" in body and ("already" in body or "exists" in body):
                return
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                "Failed to create issue link "
                f"({inward_issue_key} <-> {outward_issue_key}, type={link_type_name}). "
                f"HTTP {resp.status_code}: {resp.text}"
            )
