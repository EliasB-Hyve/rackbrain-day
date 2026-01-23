# rackbrain/core/context_builder.py

from typing import Any, Dict

from rackbrain.core.models import ErrorEvent, Ticket
from rackbrain.adapters.hyvetest_client import fetch_server_details_from_db
from rackbrain.adapters.ilom_client import get_open_problems_output
from rackbrain.adapters.ilom_parser import extract_ilom_problems
from rackbrain.core.jira_extractors import (
    extract_sn_from_text,
    extract_arch_from_summary,
    extract_testcase_from_text,
    extract_error_details_from_text,
    extract_kv_fields,
    get_field_loose,
    parse_jira_ts,
    extract_option_value,
    strip_quotes,
    extract_telnet_cmd,
)
from rackbrain.core.testview_context import add_testview_context


def build_ticket(issue: Dict[str, Any]) -> Ticket:
    """
    Convert raw Jira issue JSON into our clean Ticket model.
    """
    fields = issue.get("fields", {}) or {}
    return Ticket(
        key=issue.get("key", ""),
        summary=(fields.get("summary") or "").strip(),
        description=(fields.get("description") or "") or "",
        raw=issue,
    )


def build_error_event(ticket: Ticket) -> ErrorEvent:
    """
    Build a normalized ErrorEvent from a Jira Ticket.
    """
    summary = ticket.summary or ""
    description = ticket.description or ""

    comments = (
        ticket.raw.get("fields", {})
        .get("comment", {})
        .get("comments", [])
    )
    comments_text_parts = []
    for c in comments:
        body = c.get("body") or ""
        comments_text_parts.append(body)
    jira_comments_text = "\n\n".join(comments_text_parts)
    jira_latest_comment_text = None
    jira_latest_comment_author = None
    jira_latest_comment_author_display_name = None
    jira_latest_comment_author_email = None

    if comments:
        def _comment_sort_key(comment: dict):
            # Jira timestamps sort lexicographically (e.g. "2025-12-20T...+0000")
            return (
                str(comment.get("created") or ""),
                str(comment.get("updated") or ""),
                str(comment.get("id") or ""),
            )

        latest_comment = max(comments, key=_comment_sort_key)
        jira_latest_comment_text = latest_comment.get("body") or ""

        author = latest_comment.get("author") or {}
        jira_latest_comment_author = (
            author.get("name")
            or author.get("accountId")
            or author.get("emailAddress")
            or author.get("displayName")
        )
        jira_latest_comment_author_display_name = author.get("displayName")
        jira_latest_comment_author_email = author.get("emailAddress")

    combined_text = summary + "\n\n" + description
    sn = extract_sn_from_text(combined_text)

    # 1) Start with Jira-text-only extraction
    arch = extract_arch_from_summary(summary)
    testcase = extract_testcase_from_text(description)
    error_details = extract_error_details_from_text(description)

    fields = ticket.raw.get("fields", {}) or {}
    summary = fields.get("summary") or ""
    description = fields.get("description") or ""

    kv = extract_kv_fields(description)

    customer_field = fields.get("customfield_15119")
    location_field = fields.get("customfield_15143")

    jira_customer = extract_option_value(customer_field)
    jira_location = extract_option_value(location_field)

    jira_updated = fields.get("updated")
    assignee_field = fields.get("assignee") or {}
    jira_assignee = (
        assignee_field.get("name")
        or assignee_field.get("accountId")
        or assignee_field.get("emailAddress")
        or assignee_field.get("displayName")
    )
    reporter_field = fields.get("reporter") or {}
    jira_reporter = (
        reporter_field.get("name")
        or reporter_field.get("key")
        or reporter_field.get("accountId")
        or reporter_field.get("emailAddress")
        or reporter_field.get("displayName")
    )
    status_field = fields.get("status") or {}
    jira_status = (status_field.get("name") or "").strip() or None

    evbot_version = kv.get("EVEBOT Version")
    jira_server_status_id = kv.get("Server Status ID")
    jira_server_ok = kv.get("Server OK")

    raw_slt_attempts = get_field_loose(kv, "slt attempts")
    jira_slt_attempts = raw_slt_attempts.strip() if raw_slt_attempts else None

    jira_model = kv.get("Model")
    jira_customer_ipn = kv.get("Customer IPN")
    jira_slt_rack_sn = kv.get("SLT Rack SN")
    jira_tm2_version = kv.get("TM2 Version")
    jira_tester_email = kv.get("Tester Email")
    jira_test_started = kv.get("Test Started")
    jira_test_finished = kv.get("Test Finished")

    jira_started_dt = parse_jira_ts(jira_test_started)
    jira_finished_dt = parse_jira_ts(jira_test_finished)
    jira_duration_minutes = None
    if jira_started_dt and jira_finished_dt:
        delta = jira_finished_dt - jira_started_dt
        jira_duration_minutes = delta.total_seconds() / 60.0

    model = None
    customer_ipn = None
    rack_sn = None
    slt_rack_sn = None
    server_status_id = None
    failure_message = None
    failed_testset = None
    server_error_detail = None
    tester_email = None
    telnet_cmd = None

    if sn:
        try:
            db_row = fetch_server_details_from_db(sn)
        except Exception as e:
            print(f"[WARN] DB lookup failed for SN {sn}: {e}")
            db_row = None

        if isinstance(db_row, dict):
            server_status_id = db_row.get("server_status_id")
            rack_sn = db_row.get("rack_sn")

            model = strip_quotes(db_row.get("model"))
            customer_ipn = strip_quotes(db_row.get("customer_ipn"))
            slt_rack_sn = strip_quotes(db_row.get("test_rack_sn"))
            tester_email = strip_quotes(db_row.get("tester_email"))

            failure_message = strip_quotes(db_row.get("failure_message"))
            failed_testset = db_row.get("failed_testset")
            server_error_detail = db_row.get("server_error_detail")
            # Telnet args may appear in either DB failure_message or server_error_detail.
            # Try both (do not short-circuit to failure_message just because it's non-empty).
            telnet_cmd = extract_telnet_cmd(failure_message) or extract_telnet_cmd(
                server_error_detail
            )

            db_failed_testcase = strip_quotes(db_row.get("failed_testcase"))
            if not testcase and db_failed_testcase:
                testcase = db_failed_testcase

            if not error_details:
                error_details = failure_message or server_error_detail or error_details

    # Fallback: sometimes telnet args only appear in Jira description/combined text.
    if not telnet_cmd:
        telnet_cmd = extract_telnet_cmd(error_details) or extract_telnet_cmd(combined_text)

    ilom_problems = []
    ilom_open_problems_raw = None

    if arch == "EVE" and sn:
        try:
            raw_ilom = get_open_problems_output(sn)
            ilom_problems = extract_ilom_problems(raw_ilom)
            ilom_open_problems_raw = raw_ilom

            print("[DEBUG] ILOM problems count:", len(ilom_problems))
            for p in ilom_problems:
                print("  [DEBUG] ILOM component:", repr(p.component))
                print("  [DEBUG] ILOM desc:", repr(p.description))
        except Exception as e:
            print("[WARN] ILOM lookup failed for SN %s: %s" % (sn, e))
            ilom_problems = []
            ilom_open_problems_raw = None

    error_event = ErrorEvent(
        ticket=ticket,
        sn=sn,
        combined_text=combined_text,
        arch=arch,
        testcase=testcase,
        error_details=error_details,
        model=model,
        customer_ipn=customer_ipn,
        rack_sn=rack_sn,
        slt_rack_sn=slt_rack_sn,
        server_status_id=server_status_id,
        failure_message=failure_message,
        failed_testset=failed_testset,
        server_error_detail=server_error_detail,
        tester_email=tester_email,
        evbot_version=evbot_version,
        jira_server_status_id=jira_server_status_id,
        jira_server_ok=jira_server_ok,
        jira_slt_attempts=jira_slt_attempts,
        jira_model=jira_model,
        jira_customer_ipn=jira_customer_ipn,
        jira_slt_rack_sn=jira_slt_rack_sn,
        jira_tm2_version=jira_tm2_version,
        jira_tester_email=jira_tester_email,
        jira_test_started=jira_test_started,
        jira_test_finished=jira_test_finished,
        jira_test_duration_minutes=jira_duration_minutes,
        jira_status=jira_status,
        jira_updated=jira_updated,
        jira_assignee=jira_assignee,
        ilom_problems=ilom_problems,
        ilom_open_problems_raw=ilom_open_problems_raw,
        jira_customer=jira_customer,
        jira_location=jira_location,
        telnet_cmd=telnet_cmd,
        jira_comments_text=jira_comments_text,
        jira_latest_comment_text=jira_latest_comment_text,
        jira_latest_comment_author=jira_latest_comment_author,
        jira_latest_comment_author_display_name=jira_latest_comment_author_display_name,
        jira_latest_comment_author_email=jira_latest_comment_author_email,
        jira_reporter=jira_reporter,
    )

    add_testview_context(error_event)

    return error_event
