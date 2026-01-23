"""CLI tool for building LLM datasets from RackBrain audit exports."""

from __future__ import absolute_import

import argparse
import json
import sys
from typing import Any, Dict, Iterable, List, Optional

from .features import build_signals, make_log_excerpt, normalize_whitespace
from .schemas import LLMTicketExample, RawTicketRecord

_COMMENT_MAX_CHARS = 4000


def build_llm_dataset(input_path, output_path, strict=True):
    records_read = 0
    records_written = 0
    records_with_testview = 0

    with open(input_path, "r") as infile, open(output_path, "w") as outfile:
        for line in infile:
            records_read += 1
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue

            raw_record = RawTicketRecord(payload=payload)
            example = _build_example(raw_record)
            if not example:
                continue
            if example.text.get("testview_compact"):
                records_with_testview += 1
            record = example.to_dict()
            if strict:
                errors = _validate_llm_example(record)
                if errors:
                    identifier = record.get("id")
                    label = "record {index}".format(index=records_read)
                    if identifier:
                        label = "{label} (id={identifier})".format(
                            label=label, identifier=identifier
                        )
                    raise ValueError(
                        "{label} failed contract validation: {errors}".format(
                            label=label, errors="; ".join(errors)
                        )
                    )
            outfile.write(json.dumps(record, sort_keys=True) + "\n")
            records_written += 1

    percent_testview = 0.0
    if records_written:
        percent_testview = (records_with_testview * 100.0) / records_written
    summary = (
        "records read: {read}, records written: {written}, "
        "with testview: {percent:.1f}%"
    ).format(read=records_read, written=records_written, percent=percent_testview)
    print(summary)


def _build_example(raw_record):
    if not raw_record or not raw_record.payload:
        return None
    record = raw_record.payload

    summary = _safe_get(record, ["summary", "title", "issue_summary"])
    description = _safe_get(record, ["description", "issue_description"])
    created = _safe_get(record, ["created", "created_at"])
    updated = _safe_get(record, ["updated", "updated_at"])
    issue_key = _safe_get(record, ["issue_key", "key", "id"])
    sn = _safe_get(record, ["sn", "serial_number", "serial"])
    source_links_value = None
    if isinstance(record, dict) and "source_links" in record:
        source_links_value = record.get("source_links")
    else:
        source_links_value = _safe_get(record, ["links", "url"])
    source_links = _normalize_source_links(source_links_value)

    comments_compact = _build_comments_compact(record.get("comments"))
    testview_compact = _build_testview_compact(record.get("testview"))

    text = {
        "summary": normalize_whitespace(summary),
        "description": normalize_whitespace(description),
        "comments_compact": comments_compact,
        "testview_compact": testview_compact,
    }

    testview_text = ""
    if testview_compact:
        testview_text = normalize_whitespace(json.dumps(testview_compact, sort_keys=True))

    signals = build_signals(
        text.get("summary"),
        text.get("description"),
        comments_compact,
        testview_text,
    )

    labels = {
        "rackbrain_match": _safe_get(record, ["rackbrain_match"]),
        "matched_rule_id": _safe_get(record, ["matched_rule_id"]),
        "observed_action": _safe_get(record, ["observed_action"]),
        "resolution": _safe_get(record, ["resolution"]),
    }

    return LLMTicketExample(
        id=issue_key,
        created=created,
        updated=updated,
        sn=sn,
        source_links=source_links,
        text=text,
        signals=signals,
        labels=labels,
    )


def _build_comments_compact(comments):
    if not comments:
        return ""
    if isinstance(comments, dict):
        comments = [comments]
    parts = []
    total_chars = 0
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = _safe_get(comment, ["author", "user", "name"])
        date = _safe_get(comment, ["created", "updated", "date"])
        body = _safe_get(comment, ["body", "text", "comment"])
        header = ""
        if author or date:
            header = "[{author} {date}] ".format(
                author=author or "unknown", date=date or "unknown"
            )
        body = normalize_whitespace(body)
        snippet = "{header}{body}".format(header=header, body=body)
        if not snippet.strip():
            continue
        remaining = _COMMENT_MAX_CHARS - total_chars
        if remaining <= 0:
            break
        if len(snippet) > remaining:
            snippet = snippet[: remaining - 3] + "..."
        parts.append(snippet)
        total_chars += len(snippet)
    return "\n".join(parts)


def _build_testview_compact(testview):
    if not testview or not isinstance(testview, dict):
        return {}
    compact = {
        "download_ok": testview.get("download_ok"),
        "failed_testset": _safe_get(testview, ["failed_testset", "failed_set"]),
        "failed_testcase": _safe_get(testview, ["failed_testcase", "failed_case"]),
    }
    log_text = _find_any_log(testview)
    if log_text:
        compact["log_excerpt"] = make_log_excerpt(log_text)
    return compact


def _find_any_log(testview):
    for key, value in testview.items():
        if "log" in key.lower():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        log_text = item.get("log") or item.get("text") or item.get("body")
                        if log_text:
                            return log_text
                    elif isinstance(item, str):
                        return item
            elif isinstance(value, dict):
                log_text = value.get("log") or value.get("text") or value.get("body")
                if log_text:
                    return log_text
            elif isinstance(value, str):
                return value
    return ""


def _safe_get(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    return [value]


def _normalize_source_links(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        return _extract_source_links(value, _source_link_key_order())
    if isinstance(value, (list, tuple)):
        return _normalize_source_links_list(value)
    raise TypeError("source_links must be a string, dict, list, tuple, or None")


def _normalize_source_links_list(value):
    normalized = []
    for item in value:
        if item is None or item == "":
            continue
        if isinstance(item, str):
            if item:
                normalized.append(item)
            continue
        if isinstance(item, dict):
            normalized.extend(_extract_source_links(item, _source_link_key_order_list()))
            continue
        raise TypeError("source_links entries must be strings or dicts")
    _validate_source_links(normalized)
    return normalized


def _extract_source_links(mapping, key_order):
    if not isinstance(mapping, dict):
        raise TypeError("source_links mapping must be a dict")
    links = []
    for key in key_order:
        value = mapping.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise TypeError("source_links values must be strings")
        links.append(value)
    _validate_source_links(links)
    return links


def _validate_source_links(links):
    for link in links:
        if not isinstance(link, str):
            raise TypeError("source_links must be a list of strings")


def _source_link_key_order():
    return ["jira_url", "test_detail_url", "jar_url", "url", "href"]


def _source_link_key_order_list():
    return ["url", "jira_url", "test_detail_url", "jar_url", "href"]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Build LLM dataset JSONL from audit exports.")
    parser.add_argument("--in", dest="input_path", required=True, help="Input JSONL file")
    parser.add_argument(
        "--out", dest="output_path", required=True, help="Output JSONL file"
    )
    parser.add_argument(
        "--strict",
        dest="strict",
        action="store_true",
        default=True,
        help="Enforce the dataset contract validation (default).",
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="Disable dataset contract validation.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = _parse_args(argv)
    build_llm_dataset(args.input_path, args.output_path, strict=args.strict)


def _validate_llm_example(record):
    errors = []
    if not isinstance(record, dict):
        return ["record is not an object"]

    required_keys = {
        "id",
        "created",
        "updated",
        "sn",
        "source_links",
        "text",
        "signals",
        "labels",
    }
    _validate_exact_keys(errors, record, required_keys, "record")

    if not _is_optional_str(record.get("id")):
        errors.append("id must be a string or null")
    if not _is_optional_str(record.get("created")):
        errors.append("created must be a string or null")
    if not _is_optional_str(record.get("updated")):
        errors.append("updated must be a string or null")
    if not _is_optional_str(record.get("sn")):
        errors.append("sn must be a string or null")

    source_links = record.get("source_links")
    if not _is_str_list(source_links):
        errors.append("source_links must be a list of strings")

    _validate_text(errors, record.get("text"))
    _validate_signals(errors, record.get("signals"))
    _validate_labels(errors, record.get("labels"))
    return errors


def _validate_text(errors, text):
    if not isinstance(text, dict):
        errors.append("text must be an object")
        return
    required_keys = {"summary", "description", "comments_compact", "testview_compact"}
    _validate_exact_keys(errors, text, required_keys, "text")

    if not isinstance(text.get("summary"), str):
        errors.append("text.summary must be a string")
    if not isinstance(text.get("description"), str):
        errors.append("text.description must be a string")
    if not isinstance(text.get("comments_compact"), str):
        errors.append("text.comments_compact must be a string")

    testview = text.get("testview_compact")
    if not isinstance(testview, dict):
        errors.append("text.testview_compact must be an object")
        return
    allowed_testview_keys = {
        "download_ok",
        "failed_testset",
        "failed_testcase",
        "log_excerpt",
    }
    extra_keys = set(testview.keys()) - allowed_testview_keys
    if extra_keys:
        errors.append(
            "text.testview_compact has unexpected keys: {keys}".format(
                keys=", ".join(sorted(extra_keys))
            )
        )
    download_ok = testview.get("download_ok")
    if download_ok is not None and not isinstance(download_ok, bool):
        errors.append("text.testview_compact.download_ok must be a boolean or null")
    if not _is_optional_str(testview.get("failed_testset")):
        errors.append("text.testview_compact.failed_testset must be a string or null")
    if not _is_optional_str(testview.get("failed_testcase")):
        errors.append("text.testview_compact.failed_testcase must be a string or null")
    if "log_excerpt" in testview and not isinstance(testview.get("log_excerpt"), str):
        errors.append("text.testview_compact.log_excerpt must be a string")


def _validate_signals(errors, signals):
    if not isinstance(signals, dict):
        errors.append("signals must be an object")
        return
    required_keys = {"keywords", "components", "error_signatures", "ports", "lanes"}
    _validate_exact_keys(errors, signals, required_keys, "signals")
    for key in required_keys:
        if not _is_str_list(signals.get(key)):
            errors.append("signals.{key} must be a list of strings".format(key=key))


def _validate_labels(errors, labels):
    if not isinstance(labels, dict):
        errors.append("labels must be an object")
        return
    required_keys = {
        "rackbrain_match",
        "matched_rule_id",
        "observed_action",
        "resolution",
    }
    _validate_exact_keys(errors, labels, required_keys, "labels")
    rackbrain_match = labels.get("rackbrain_match")
    if rackbrain_match is not None and not isinstance(rackbrain_match, bool):
        errors.append("labels.rackbrain_match must be a boolean or null")
    if not _is_optional_str(labels.get("matched_rule_id")):
        errors.append("labels.matched_rule_id must be a string or null")
    if not _is_optional_str(labels.get("observed_action")):
        errors.append("labels.observed_action must be a string or null")
    if not _is_optional_str(labels.get("resolution")):
        errors.append("labels.resolution must be a string or null")


def _validate_exact_keys(errors, mapping, required_keys, label):
    actual_keys = set(mapping.keys())
    missing = required_keys - actual_keys
    if missing:
        errors.append(
            "{label} missing keys: {keys}".format(
                label=label, keys=", ".join(sorted(missing))
            )
        )
    extra = actual_keys - required_keys
    if extra:
        errors.append(
            "{label} has unexpected keys: {keys}".format(
                label=label, keys=", ".join(sorted(extra))
            )
        )


def _is_optional_str(value):
    return value is None or isinstance(value, str)


def _is_str_list(value):
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) for item in value)


if __name__ == "__main__":
    main()
