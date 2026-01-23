#!/usr/bin/env python3
"""
Export Jira issues (summary + description + comments) to JSONL.
Also attempt to download TestView logs from any testdetail URL.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple
from urllib.parse import urlparse

SCRIPT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rackbrain.adapters.jira_client import JiraClient
from rackbrain.core.config_loader import load_app_config, load_config
from rackbrain.core.jira_extractors import extract_sn_from_text

import Testviewlog


TESTDETAIL_REGEX = re.compile(r"https?://[^\s\"']+/slt/testdetail/\d+", re.IGNORECASE)
TESTDETAIL_ID_REGEX = re.compile(r"/slt/testdetail/(\d+)", re.IGNORECASE)
JAR_REGEX = re.compile(r"https?://[^\s\"']+?\.jar(?:\?[^\s\"']*)?", re.IGNORECASE)
DEFAULT_INLINE_LOG_BYTES = 200 * 1024


def _clean_url(value: str) -> str:
    if not value:
        return value
    trimmed = value.strip()
    while trimmed and trimmed[-1] in ".,;)]}>\"'":
        trimmed = trimmed[:-1]
    return trimmed


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _get_comment_author(author: Dict[str, Any]) -> str:
    if not isinstance(author, dict):
        return ""
    for key in ("displayName", "emailAddress", "name", "accountId"):
        if author.get(key):
            return str(author.get(key))
    return ""


def _format_combined_text(summary: str, description: str) -> str:
    return "Summary:\n{summary}\n\nDescription:\n{description}".format(
        summary=_stringify(summary),
        description=_stringify(description),
    )


def _format_combined_text_with_comments(
    base_text: str, comments: List[Dict[str, Any]]
) -> str:
    lines = [base_text, "", "Comments:"]
    for comment in comments:
        header = "Comment {cid} by {author} at {created}:".format(
            cid=_stringify(comment.get("id")),
            author=_stringify(comment.get("author")),
            created=_stringify(comment.get("created")),
        )
        body = _stringify(comment.get("body"))
        lines.append(header)
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_combined_text_with_comments_and_logs(
    combined_text_with_comments: str, testview_result: Dict[str, Any]
) -> str:
    lines = [combined_text_with_comments, "", "=== TESTVIEW ==="]
    lines.append("download_attempted: {value}".format(
        value=_stringify(testview_result.get("download_attempted"))
    ))
    lines.append("download_ok: {value}".format(
        value=_stringify(testview_result.get("download_ok"))
    ))
    error = testview_result.get("error")
    lines.append("error: {value}".format(value=_stringify(error)))

    artifacts = testview_result.get("artifacts") or []
    lines.append("artifacts:")
    if artifacts:
        for artifact in artifacts:
            path = _stringify(artifact.get("path"))
            size = _stringify(artifact.get("size"))
            lines.append("- {path} ({size} bytes)".format(path=path, size=size))
    else:
        lines.append("- none")

    log_text = testview_result.get("log_text")
    truncated = testview_result.get("log_text_truncated")
    if log_text:
        lines.append("log_text (truncated={value}):".format(
            value=_stringify(truncated)
        ))
        lines.append("--- LOG START ---")
        lines.append(_stringify(log_text))
        lines.append("--- LOG END ---")
    else:
        lines.append("log_text: null (truncated={value})".format(
            value=_stringify(truncated)
        ))

    return "\n".join(lines).rstrip()


def _find_first(regex: Pattern, text: str) -> Optional[str]:
    if not text:
        return None
    match = regex.search(text)
    if not match:
        return None
    return _clean_url(match.group(0))


def _extract_slt_id_from_testdetail_url(url: str) -> Optional[int]:
    if not url:
        return None
    match = TESTDETAIL_ID_REGEX.search(url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _resolve_output_path(out_prefix: str) -> Path:
    if out_prefix.endswith(".jsonl"):
        return Path(out_prefix)
    return Path(out_prefix + ".jsonl")


def _load_audit_config(config_path: Path) -> Dict[str, Any]:
    config_path = Path(config_path)
    config = load_config(config_path) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("Audit config must be a YAML mapping: {path}".format(path=config_path))
    return config


def _get_inline_log_limit(config: Dict[str, Any]) -> int:
    testview_cfg = dict(config.get("testview", {}) or {})
    inline_max = testview_cfg.get("inline_max_bytes", DEFAULT_INLINE_LOG_BYTES)
    try:
        return int(inline_max)
    except (TypeError, ValueError):
        return DEFAULT_INLINE_LOG_BYTES


def _audit_config_value(config: Dict[str, Any], path: List[str], default: Any) -> Any:
    current = config
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    if current is None:
        return default
    return current


def _load_jira_client(explicit_config: Optional[str] = None) -> Tuple[JiraClient, str]:
    config, _, _ = load_app_config(explicit_config)
    jira_cfg = dict(config.get("jira", {}) or {})
    base_url = _stringify(jira_cfg.get("base_url", "")).strip()
    if not base_url:
        raise RuntimeError("jira.base_url is not set in config/config.yaml")

    pat = jira_cfg.get("pat")
    pat_env = jira_cfg.get("pat_env", "RACKBRAIN_JIRA_PAT")
    client = JiraClient(base_url=base_url, pat=pat, pat_env=pat_env)
    return client, base_url


def _fetch_comments(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []
    start_at = 0
    while True:
        payload = client.get_issue_comments(key, start_at=start_at, max_results=50)
        for comment in payload.get("comments", []) or []:
            author = _get_comment_author(comment.get("author", {}))
            comments.append({
                "id": _stringify(comment.get("id")),
                "author": author,
                "created": _stringify(comment.get("created")),
                "body": _stringify(comment.get("body")),
            })
        total = int(payload.get("total") or 0)
        start_at += int(payload.get("maxResults") or 0)
        if start_at >= total:
            break
    comments.sort(key=lambda c: (c.get("created", ""), c.get("id", "")))
    return comments


def _extract_links(text: str, jira_base_url: str, issue_key: str) -> Dict[str, Optional[str]]:
    test_detail_url = _find_first(TESTDETAIL_REGEX, text)
    jar_url = _find_first(JAR_REGEX, text)
    jira_url = jira_base_url.rstrip("/") + "/browse/" + issue_key
    return {
        "jira_url": jira_url,
        "test_detail_url": test_detail_url,
        "jar_url": jar_url,
    }


def _download_url_to_file(session: Any, url: str, dest_path: Path) -> Optional[str]:
    try:
        with session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        handle.write(chunk)
    except Exception as exc:
        return "Failed to download {url}: {err}".format(url=url, err=str(exc))
    return None


def _read_log_text(path: Path, inline_log_bytes: int) -> Tuple[Optional[str], bool]:
    size = path.stat().st_size
    if size > inline_log_bytes:
        return None, True
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(), False


def _attempt_testview_download(
    test_detail_url: Optional[str],
    issue_key: str,
    out_dir: Path,
    combined_text: str,
    testview_enabled: bool,
    inline_log_bytes: int,
) -> Tuple[Dict[str, Any], Optional[str]]:
    result: Dict[str, Any] = {
        "download_attempted": False,
        "download_ok": False,
        "error": None,
        "artifacts": [],
        "log_text": None,
        "log_text_truncated": False,
        "log_url": None,
        "slt_id": None,
        "failed_testset": None,
        "failed_testcase": None,
    }
    html_text: Optional[str] = None

    if not testview_enabled:
        return result, None

    if not test_detail_url:
        return result, None

    result["download_attempted"] = True

    cookie = os.environ.get("HYVE_TESTVIEW_COOKIE", "").strip()
    if not cookie:
        raise RuntimeError("HYVE_TESTVIEW_COOKIE is not set.")

    slt_id = _extract_slt_id_from_testdetail_url(test_detail_url)
    if slt_id is None:
        raise RuntimeError("Unable to parse slt_id from test_detail_url.")

    sn = extract_sn_from_text(combined_text)
    if not sn:
        raise RuntimeError("SN not found for Testviewlog.build_log_url.")

    run_info = Testviewlog.get_run_by_slt_id(sn=sn, slt_id=slt_id)
    if not run_info:
        raise RuntimeError(f"No run found for sn={sn} slt_id={slt_id}.")

    failed_testset = run_info.get("failed_testset") or run_info.get("operation")
    failed_testcase = run_info.get("failed_testcase")

    if not failed_testset:
        raise RuntimeError(f"Missing failed_testset for sn={sn} slt_id={slt_id}.")
    if not failed_testcase:
        raise RuntimeError(f"Missing failed_testcase for sn={sn} slt_id={slt_id}.")

    result["slt_id"] = int(slt_id)
    result["failed_testset"] = failed_testset
    result["failed_testcase"] = failed_testcase

    testcases = [tc.strip() for tc in (failed_testcase or "").split(",") if tc.strip()]
    if not failed_testset or not testcases:
        raise RuntimeError(f"No testcase found for sn={sn} slt_id={slt_id}.")

    base_url = "{scheme}://{netloc}".format(
        scheme=urlparse(test_detail_url).scheme,
        netloc=urlparse(test_detail_url).netloc,
    )
    session = Testviewlog._make_testview_session(cookie_header=cookie)
    log_url = Testviewlog.build_log_url(
        sn=sn,
        slt_id=int(slt_id),
        testset=failed_testset,
        testcase=testcases[0],
        filename="log.raw",
        base_url=base_url,
    )
    result["log_url"] = log_url

    artifacts_dir = out_dir / "testview" / issue_key
    log_path = artifacts_dir / "log.raw"
    err = _download_url_to_file(session, log_url, log_path)
    if err:
        raise RuntimeError(err)

    result["download_ok"] = True
    size = log_path.stat().st_size
    rel_path = str(log_path.relative_to(out_dir))
    result["artifacts"].append({
        "path": rel_path,
        "size": size,
    })

    log_text, truncated = _read_log_text(log_path, inline_log_bytes)
    result["log_text"] = log_text
    result["log_text_truncated"] = truncated

    try:
        resp = session.get(test_detail_url, timeout=30)
        resp.raise_for_status()
        html_text = resp.text
    except Exception as exc:
        raise RuntimeError(
            "Failed to fetch testdetail page for jar extraction: {err}".format(err=exc)
        )

    return result, html_text


def _augment_jar_link(existing: Optional[str], html_text: Optional[str]) -> Optional[str]:
    if existing:
        return existing
    if not html_text:
        return None
    return _find_first(JAR_REGEX, html_text)


def _fetch_issue_record(
    client: JiraClient,
    issue_key: str,
    jira_base_url: str,
    out_dir: Path,
    testview_enabled: bool,
    inline_log_bytes: int,
) -> Dict[str, Any]:
    issue = client.get_issue(issue_key, fields=["summary", "description", "created", "updated"])
    fields = issue.get("fields", {}) or {}
    summary = _stringify(fields.get("summary"))
    description = _stringify(fields.get("description"))
    created = _stringify(fields.get("created"))
    updated = _stringify(fields.get("updated"))

    comments = _fetch_comments(client, issue_key)

    combined_text = _format_combined_text(summary, description)
    combined_text_with_comments = _format_combined_text_with_comments(combined_text, comments)

    links = _extract_links(combined_text_with_comments, jira_base_url, issue_key)

    testview_result, testdetail_html = _attempt_testview_download(
        links.get("test_detail_url"),
        issue_key,
        out_dir,
        combined_text_with_comments,
        testview_enabled,
        inline_log_bytes,
    )

    if testdetail_html:
        links["jar_url"] = _augment_jar_link(links.get("jar_url"), testdetail_html)

    combined_text_with_comments_and_logs = _format_combined_text_with_comments_and_logs(
        combined_text_with_comments,
        testview_result,
    )

    sn = extract_sn_from_text(combined_text_with_comments)

    record = {
        "issue_key": issue_key,
        "created": created,
        "updated": updated,
        "sn": sn,
        "summary": summary,
        "description": description,
        "comments": comments,
        "combined_text": combined_text,
        "combined_text_with_comments": combined_text_with_comments,
        "combined_text_with_comments_and_logs": combined_text_with_comments_and_logs,
        "links": links,
        "testview": testview_result,
    }
    return record


def _write_jsonl(records: Iterable[Dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")


def _resolve_issue_keys(
    client: JiraClient,
    issue_key: Optional[str],
    jql: Optional[str],
    max_results: int,
) -> List[str]:
    if issue_key:
        return [issue_key]
    if not jql:
        raise ValueError("JQL is required when resolving issue keys.")

    keys: List[str] = []
    start_at = 0
    total = None
    remaining = max_results
    page_size = 50

    while True:
        if remaining <= 0:
            break
        batch_size = min(page_size, remaining)
        payload: Dict[str, Any] = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": batch_size,
            "fields": ["key"],
        }
        resp = client.session.post(client._url("/rest/api/2/search"), json=payload)
        if resp.status_code == 401:
            raise RuntimeError("Jira 401 Unauthorized — check PAT/permissions.")
        resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues", []) or []
        for issue in issues:
            if issue.get("key"):
                keys.append(issue.get("key"))
        total = data.get("total") if total is None else total
        start_at += len(issues)
        remaining = max_results - len(keys)
        if len(issues) < batch_size:
            break
        if total is not None and start_at >= int(total):
            break

    return keys


def smoke_test(
    testview_enabled: bool,
    inline_log_bytes: int,
) -> None:
    """Manual smoke test for exporting MFGS-462944."""
    client, jira_base_url = _load_jira_client(None)
    output_path = _resolve_output_path("audit_raw_export_smoke")
    out_dir = output_path.parent
    record = _fetch_issue_record(
        client,
        "MFGS-462944",
        jira_base_url,
        out_dir,
        testview_enabled,
        inline_log_bytes,
    )
    _write_jsonl([record], output_path)
    print("Smoke test exported to {path}".format(path=output_path))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Jira issues to JSONL with TestView logs.")
    parser.add_argument("--issue", help="Single Jira issue key (e.g. MFGS-123).")
    parser.add_argument("--jql", help="Jira Query Language string.")
    parser.add_argument("--max-results", type=int, help="Max results for JQL.")
    parser.add_argument("--out", help="Output path prefix (JSONL).")
    parser.add_argument("--config", help="Optional path to rackbrain config.yaml.")
    parser.add_argument(
        "--audit-config",
        default="audit_raw_export/audit_raw_export_config.yaml",
        help="Path to audit_raw_export config file.",
    )
    parser.add_argument("--smoke-test", action="store_true", help="Run smoke test export for MFGS-462944.")
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    audit_config_path = Path(args.audit_config)
    audit_config = _load_audit_config(audit_config_path)
    inline_log_bytes = _get_inline_log_limit(audit_config)
    testview_enabled = bool(_audit_config_value(audit_config, ["testview", "enabled"], True))

    if args.smoke_test:
        smoke_test(testview_enabled, inline_log_bytes)
        return

    client, jira_base_url = _load_jira_client(args.config)

    config_jql = _audit_config_value(audit_config, ["jql", "value"], "")
    config_max_results = _audit_config_value(audit_config, ["jql", "max_results"], 200)
    config_output = _audit_config_value(audit_config, ["output", "jsonl"], "")

    jql = args.jql if args.jql else config_jql
    max_results = args.max_results if args.max_results is not None else config_max_results
    output_target = args.out if args.out else config_output

    if args.issue:
        issue_keys = [args.issue]
    else:
        if not jql:
            raise SystemExit("Provide --issue, --jql, or configure jql.value in audit config.")
        issue_keys = _resolve_issue_keys(client, None, jql, max_results)

    if not output_target:
        raise SystemExit("Provide --out or configure output.jsonl in audit config.")

    output_path = _resolve_output_path(output_target)
    out_dir = output_path.parent

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for issue_key in issue_keys:
            record = _fetch_issue_record(
                client,
                issue_key,
                jira_base_url,
                out_dir,
                testview_enabled,
                inline_log_bytes,
            )
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            count += 1

    print("Wrote {count} records to {path}".format(count=count, path=output_path))


if __name__ == "__main__":
    main()
