import os
from typing import Any, Dict, List, Optional

import requests


class JiraClient:
    """
    Jira client using the same PAT/Bearer auth pattern as:
      - autojira_menu.py
      - Jiraprecheck.py
    (Authorization: Bearer <PAT>, no basic auth). 
    """

    def __init__(
        self,
        *,
        base_url: str,
        pat: Optional[str] = None,
        pat_env: str = "RACKBRAIN_JIRA_PAT",
    ) -> None:
        self.base_url = base_url.rstrip("/")

        if pat is None or not str(pat).strip():
            pat = os.environ.get(pat_env, "")
        pat = str(pat).strip()

        if not pat:
            raise RuntimeError(
                "Missing Jira PAT. Set jira.pat in config/config.yaml "
                f"or set the environment variable {pat_env}."
            )

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })


    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def get_issue(self, key: str, fields: Optional[List[str]] = None) -> Dict[str, Any]:
        params = {}
        if fields:
            params["fields"] = ",".join(fields)
        resp = self.session.get(self._url(f"/rest/api/2/issue/{key}"), params=params)
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, key: str, body: str) -> None:
        resp = self.session.post(
            self._url(f"/rest/api/2/issue/{key}/comment"),
            json={"body": body},
        )
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        resp.raise_for_status()

    def get_issue_comments(
        self,
        key: str,
        start_at: int = 0,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        """
        Fetch comments for an issue via the dedicated comments endpoint.

        Useful when the issue payload's `fields.comment.comments` is truncated.
        """
        params = {
            "startAt": start_at,
            "maxResults": max_results,
        }
        resp = self.session.get(self._url(f"/rest/api/2/issue/{key}/comment"), params=params)
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized - check PAT/permissions.")
        resp.raise_for_status()
        return resp.json()

    def do_transition(
        self,
        key: str,
        transition_id: str,
        comment_body: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {"transition": {"id": transition_id}}
        if comment_body:
            payload["update"] = {
                "comment": [{"add": {"body": comment_body}}],
            }

        resp = self.session.post(
            self._url(f"/rest/api/2/issue/{key}/transitions"),
            json=payload,
        )
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        resp.raise_for_status()

    def assign_issue(self, key: str, username: str) -> None:
        """
        Assign the Jira issue to the given username.

        For Jira Server/Data Center, this uses the legacy 'name' field.
        If your instance uses accountId instead, update the JSON accordingly.
        """
        resp = self.session.put(
            self._url(f"/rest/api/2/issue/{key}/assignee"),
            json={"name": username},
        )
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        # Jira often returns 204 No Content on success.
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to assign {key} to {username}. "
                f"HTTP {resp.status_code}: {resp.text}"
            )
    def transition_issue(self, key: str, transition_id: str) -> None:
        """
        Run a Jira workflow transition (e.g. Open -> In Progress).

        Jira Data Center / Server uses:
        POST /rest/api/2/issue/{issueIdOrKey}/transitions
        with JSON: {"transition": {"id": "<id>"}}
        """
        resp = self.session.post(
            self._url(f"/rest/api/2/issue/{key}/transitions"),
            json={"transition": {"id": transition_id}},
        )

        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")

        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to transition {key} using ID {transition_id}. "
                f"HTTP {resp.status_code}: {resp.text}"
            )


    def get_transitions(self, key: str):
        """
        Retrieve all available workflow transitions for an issue.

        Returns a list of transition objects:
        [{"id": "31", "name": "In Progress", ...}, ...]
        """
        resp = self.session.get(self._url(f"/rest/api/2/issue/{key}/transitions"))

        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")

        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch transitions for {key}. "
                f"HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        return data.get("transitions", [])

    def search_issues(self, jql: str, fields: Optional[List[str]] = None, max_results: int = 200) -> List[Dict[str, Any]]:
        """
        Search for issues using JQL (Jira Query Language).

        Args:
            jql: JQL query string (e.g., 'project = MFGS AND status = Open')
            fields: Optional list of field names to return (default: all)
            max_results: Maximum number of results to return (default: 200)

        Returns:
            List of issue dictionaries
        """
        payload: Dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
        }
        if fields:
            payload["fields"] = fields

        resp = self.session.post(self._url("/rest/api/2/search"), json=payload)
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        resp.raise_for_status()

        data = resp.json()
        return data.get("issues", [])

    def create_issue_link(
        self,
        *,
        link_type_name: str,
        inward_issue_key: str,
        outward_issue_key: str,
    ) -> None:
        """
        Create a standard Jira Issue Link (not a web/remote link).

        Example:
          - "MFGS-1 is blocked by PRODISS-2"
            link_type_name="Blocks", inward_issue_key="MFGS-1", outward_issue_key="PRODISS-2"

        Jira API:
          POST /rest/api/2/issueLink
        """
        payload: Dict[str, Any] = {
            "type": {"name": link_type_name},
            "inwardIssue": {"key": inward_issue_key},
            "outwardIssue": {"key": outward_issue_key},
        }

        resp = self.session.post(self._url("/rest/api/2/issueLink"), json=payload)
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized - check PAT/permissions.")
        if resp.status_code == 400:
            # Jira returns 400 for duplicate links on some Server/DC versions.
            body = (resp.text or "").lower()
            if "issue link" in body and ("already" in body or "exists" in body):
                return
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                "Failed to create issue link "
                f"({inward_issue_key} <-> {outward_issue_key}, type={link_type_name}). "
                f"HTTP {resp.status_code}: {resp.text}"
            )
