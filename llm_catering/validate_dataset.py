"""Offline validation tool for the RackBrain LLM dataset contract."""

from __future__ import absolute_import

import argparse
import json
import math
import sys
from collections import Counter


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = _parse_args(argv)
    result = _validate_dataset(args.input_path, include_stats=args.stats)
    _print_summary(result, include_stats=args.stats)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Validate a RackBrain LLM dataset JSONL file."
    )
    parser.add_argument(
        "--in",
        dest="input_path",
        required=True,
        help="Input dataset JSONL file",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print additional dataset statistics.",
    )
    return parser.parse_args(argv)


def _validate_dataset(input_path, include_stats=False):
    total_records = 0
    failure_counts = Counter()
    testview_records = 0
    text_lengths = []
    error_signature_counts = Counter()
    component_counts = Counter()

    with open(input_path, "r") as infile:
        for line in infile:
            line = line.strip()
            if not line:
                continue
            total_records += 1
            try:
                record = json.loads(line)
            except ValueError:
                raise ValueError(
                    "record {index} failed contract validation: invalid json".format(
                        index=total_records
                    )
                )

            errors = _validate_record(record)
            if errors:
                for error in errors:
                    failure_counts[error] += 1
                identifier = None
                if isinstance(record, dict):
                    identifier = record.get("id")
                label = "record {index}".format(index=total_records)
                if identifier is not None:
                    label = "{label} (id={identifier})".format(
                        label=label, identifier=identifier
                    )
                raise ValueError(
                    "{label} failed contract validation: {errors}".format(
                        label=label, errors="; ".join(errors)
                    )
                )

            if include_stats and isinstance(record, dict):
                if _has_testview(record):
                    testview_records += 1
                text_lengths.append(_text_length(record))
                _update_signal_counts(record, error_signature_counts, component_counts)

    return {
        "total_records": total_records,
        "invalid_records": 0,
        "failure_counts": failure_counts,
        "testview_records": testview_records,
        "text_lengths": text_lengths,
        "error_signature_counts": error_signature_counts,
        "component_counts": component_counts,
    }


def _print_summary(result, include_stats=False):
    print("total_records: {total}".format(total=result["total_records"]))
    print("invalid_records: {invalid}".format(invalid=result["invalid_records"]))
    print("top_validation_failures:")
    failures = result["failure_counts"].most_common(10)
    if not failures:
        print("- none")
    else:
        for message, count in failures:
            print("- {message}: {count}".format(message=message, count=count))

    if include_stats:
        _print_stats(result)


def _print_stats(result):
    total = result["total_records"]
    testview = result["testview_records"]
    percent_testview = 0.0
    if total:
        percent_testview = (testview * 100.0) / total
    avg_text = 0.0
    p95_text = 0
    if result["text_lengths"]:
        avg_text = sum(result["text_lengths"]) / float(len(result["text_lengths"]))
        p95_text = _percentile(result["text_lengths"], 0.95)

    print("stats:")
    print("  testview_percent: {percent:.1f}%".format(percent=percent_testview))
    print("  avg_text_length: {avg:.1f}".format(avg=avg_text))
    print("  p95_text_length: {p95}".format(p95=p95_text))
    print("  most_common_error_signatures:")
    _print_top_counts(result["error_signature_counts"])
    print("  most_common_components:")
    _print_top_counts(result["component_counts"])


def _print_top_counts(counter, limit=10):
    if not counter:
        print("    - none")
        return
    for value, count in counter.most_common(limit):
        print("    - {value}: {count}".format(value=value, count=count))


def _percentile(values, percentile):
    if not values:
        return 0
    sorted_values = sorted(values)
    rank = int(math.ceil(percentile * len(sorted_values)))
    index = max(rank - 1, 0)
    return sorted_values[index]


def _has_testview(record):
    text = record.get("text")
    if not isinstance(text, dict):
        return False
    testview = text.get("testview_compact")
    if not isinstance(testview, dict):
        return False
    return any(value is not None and value != "" for value in testview.values())


def _text_length(record):
    text = record.get("text")
    if not isinstance(text, dict):
        return 0
    summary = text.get("summary") or ""
    description = text.get("description") or ""
    comments = text.get("comments_compact") or ""
    testview = text.get("testview_compact")
    testview_text = ""
    if isinstance(testview, dict) and testview:
        try:
            testview_text = json.dumps(testview, sort_keys=True)
        except (TypeError, ValueError):
            testview_text = ""
    combined = "{summary}\n{description}\n{comments}\n{testview}".format(
        summary=summary,
        description=description,
        comments=comments,
        testview=testview_text,
    )
    return len(combined.strip())


def _update_signal_counts(record, error_signature_counts, component_counts):
    signals = record.get("signals")
    if not isinstance(signals, dict):
        return
    error_signatures = signals.get("error_signatures")
    if isinstance(error_signatures, list):
        for value in error_signatures:
            if isinstance(value, str):
                error_signature_counts[value] += 1
    components = signals.get("components")
    if isinstance(components, list):
        for value in components:
            if isinstance(value, str):
                component_counts[value] += 1


def _validate_record(record):
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

    if not _is_str_list(record.get("source_links")):
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
