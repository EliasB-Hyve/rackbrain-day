"""Feature extraction utilities for LLM dataset prep."""

from __future__ import absolute_import

import re
from typing import Dict, List


_WHITESPACE_RE = re.compile(r"\s+")
_PORT_RE = re.compile(r"\bport\s*:?\s*(\d+)\b|\bport(\d+)\b", re.IGNORECASE)
_LANE_RE = re.compile(r"\blane\s*(\d+)\b|\blane(\d+)\b", re.IGNORECASE)

_ERROR_SIGNATURES = [
    "run prbs test failed",
    "link down",
    "rx loss of signal",
    "crc error",
    "firmware mismatch",
    "link flapping",
    "ecc error",
    "training timeout",
    "pcie reset",
    "watchdog timeout",
    "thermal shutdown",
    "fan failure",
    "power fault",
    "memory training failed",
    "bit error",
    "packet drop",
    "asic fault",
    "tx fault",
    "port disabled",
    "timeout waiting",
]

_COMPONENT_KEYWORDS = {
    "switch": ["switch", "tor", "leaf", "spine"],
    "cable": ["cable", "dac", "aoc"],
    "nic": ["nic", "network card", "adapter"],
    "pcie": ["pcie", "pci-e", "pci express"],
    "dimm": ["dimm", "memory", "ram"],
    "bmc": ["bmc", "baseboard"],
    "psu": ["psu", "power supply"],
    "fan": ["fan"],
    "cpu": ["cpu", "processor"],
    "disk": ["disk", "ssd", "nvme", "drive"],
    "firmware": ["firmware", "bios", "uefi"],
    "optics": ["optic", "optical", "qsfp", "sfp"],
}

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
    ]
)


def normalize_whitespace(text):
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def extract_ports(text):
    if not text:
        return []
    ports = []
    for match in _PORT_RE.finditer(text):
        number = match.group(1) or match.group(2)
        if number:
            ports.append("port{0}".format(number))
    return _unique_preserve_order(ports)


def extract_lanes(text):
    if not text:
        return []
    lanes = []
    for match in _LANE_RE.finditer(text):
        number = match.group(1) or match.group(2)
        if number:
            lanes.append("lane{0}".format(number))
    return _unique_preserve_order(lanes)


def extract_error_signatures(text):
    if not text:
        return []
    lowered = text.lower()
    found = []
    for signature in _ERROR_SIGNATURES:
        if signature in lowered:
            found.append(signature)
    return found


def extract_components(text):
    if not text:
        return []
    lowered = text.lower()
    found = []
    for component, keywords in _COMPONENT_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                found.append(component)
                break
    return _unique_preserve_order(found)


def make_log_excerpt(log_text, max_lines=60, max_chars=4000):
    if not log_text:
        return ""
    lines = log_text.splitlines()
    error_lines = [line for line in lines if _line_has_error(line)]
    if error_lines:
        excerpt_lines = error_lines[:max_lines]
    else:
        head_count = max_lines // 2
        tail_count = max_lines - head_count
        excerpt_lines = lines[:head_count] + lines[-tail_count:]
    excerpt = "\n".join(excerpt_lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 3] + "..."
    return excerpt


def build_signals(summary, description, comments_text, testview_text):
    chunks = [summary, description, comments_text, testview_text]
    combined = "\n".join([chunk for chunk in chunks if chunk])
    return {
        "keywords": _extract_keywords(combined),
        "components": extract_components(combined),
        "error_signatures": extract_error_signatures(combined),
        "ports": extract_ports(combined),
        "lanes": extract_lanes(combined),
    }


def _line_has_error(line):
    upper = line.upper()
    return "ERROR" in upper or "FAIL" in upper or "FAILED" in upper


def _extract_keywords(text, max_keywords=12):
    if not text:
        return []
    counts = {}
    for word in re.findall(r"[A-Za-z0-9_-]{4,}", text.lower()):
        if word in _STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    sorted_words = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _count in sorted_words[:max_keywords]]


def _unique_preserve_order(items):
    seen = set()
    unique_items = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique_items.append(item)
    return unique_items
