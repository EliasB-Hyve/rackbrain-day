"""Offline tooling for clustering LLM dataset records into candidate rule families."""

from __future__ import absolute_import

import json
import re


_STOPWORDS = set(
    [
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "when",
        "then",
        "into",
        "over",
        "under",
        "such",
        "port",
        "lane",
        "test",
        "failed",
        "failure",
        "issue",
        "error",
        "errors",
        "fail",
        "fails",
    ]
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def load_llm_dataset(path):
    records = []
    with open(path, "r") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def cluster_key(record):
    signals = record.get("signals") or {}
    error_signatures = _as_list(signals.get("error_signatures"))
    if error_signatures:
        return ("error_signatures", tuple(sorted(error_signatures)))

    components = _as_list(signals.get("components"))
    if components:
        ports = _as_list(signals.get("ports"))
        lanes = _as_list(signals.get("lanes"))
        return (
            "components",
            tuple(sorted(components)),
            tuple(sorted(ports)),
            tuple(sorted(lanes)),
        )

    return ("misc",)


def cluster_records(records):
    clusters = {}
    for record in records:
        key = cluster_key(record)
        clusters.setdefault(key, []).append(record)
    return clusters


def top_terms(texts, max_terms=10):
    counts = {}
    for text in texts:
        if not text:
            continue
        for token in _TOKEN_RE.findall(text.lower()):
            if token in _STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
    sorted_terms = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [term for term, _count in sorted_terms[:max_terms]]


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    return [value]


def gather_text_fields(record):
    text = record.get("text") or {}
    parts = []
    for key in ("summary", "description", "comments_compact", "testview_compact"):
        value = text.get(key)
        if isinstance(value, dict):
            parts.append(json.dumps(value, sort_keys=True))
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def excerpt_text(text, max_chars=200):
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def serialize_cluster_key(key):
    return _serialize_value(key)


def _serialize_value(value):
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value
