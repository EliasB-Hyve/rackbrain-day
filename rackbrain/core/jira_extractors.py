import re
from datetime import datetime
from typing import Optional


SN_REGEX = re.compile(r"\b([A-Z0-9]{10,20})\b")
ARCH_REGEX = re.compile(r"\b(EVE|HOP|HOPPER|WOODCHUCK)\b", re.IGNORECASE)
FAILED_TC_REGEX = re.compile(r"Failed Testcase:\s*(.+)", re.IGNORECASE)
FIELD_LINE_RE = re.compile(r"^([A-Za-z0-9 _]+):\s*(.*)$")
# Accept pexpect-style arg dumps like:
#   args: ['/usr/bin/telnet', '10.8.33.168', '2012']
# and variants with optional u/b prefixes, optional quotes, extra args, and/or double-quotes.
TELNET_ARGS_RE = re.compile(
    r"args:\s*\[\s*"
    r"[^\]]*telnet[^\]]*"
    r",\s*(?:[rub]*['\"])?(\d{1,3}(?:\.\d{1,3}){3})(?:['\"])?"
    r"\s*,\s*(?:[rub]*['\"])?(\d{2,5})(?:['\"])?"
    r"(?:\s*,[^\]]*)?\s*\]",
    re.IGNORECASE,
)

# Fallback: plain telnet command in text
TELNET_CMD_RE = re.compile(
    r"\btelnet\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{2,5})\b",
    re.IGNORECASE,
)


def extract_sn_from_text(text: str) -> Optional[str]:
    """
    Find a plausible server SN in the given text.
    """
    if not text:
        return None

    m = SN_REGEX.search(text)
    if not m:
        return None
    return m.group(1)


def extract_arch_from_summary(summary: str) -> Optional[str]:
    if not summary:
        return None
    m = ARCH_REGEX.search(summary)
    if not m:
        return None
    val = m.group(1).upper()
    if val == "HOP":
        val = "HOPPER"
    return val


def extract_testcase_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = FAILED_TC_REGEX.search(text)
    if m:
        return m.group(1).strip()
    return None


def extract_error_details_from_text(text: str) -> Optional[str]:
    """
    Extract a crude failure-message block from the ticket description.
    """
    if not text:
        return None

    lines = text.splitlines()
    details_lines = []
    in_block = False

    for line in lines:
        if "Failure Message:" in line:
            details_lines.append(line.strip())
            in_block = True
            continue
        if in_block:
            if (
                line.strip().startswith("Retry count")
                or line.strip().startswith("Problem class:")
                or not line.strip()
            ):
                details_lines.append(line.strip())
                break
            details_lines.append(line.strip())

    if not details_lines:
        return None

    return "\n".join(details_lines)


def _strip_jira_formatting(line: str) -> str:
    if not line:
        return ""

    line = line.strip()
    line = re.sub(r"^[\*\-]\s+", "", line)
    line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
    line = re.sub(r"\*(.*?)\*", r"\1", line)
    line = re.sub(r"</?strong>", "", line, flags=re.IGNORECASE)
    line = re.sub(r"</?b>", "", line, flags=re.IGNORECASE)
    line = re.sub(r"<[^>]+>", "", line)

    return line.strip()


def extract_kv_fields(text: str) -> dict:
    fields = {}
    if not text:
        return fields

    for line in text.splitlines():
        clean = _strip_jira_formatting(line)
        m = FIELD_LINE_RE.match(clean)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            fields[key] = val

    return fields


def get_field_loose(fields: dict, needle: str) -> Optional[str]:
    needle = needle.lower()
    for k, v in fields.items():
        if needle in k.strip().lower():
            return v
    return None


def parse_jira_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def extract_option_value(field):
    if isinstance(field, dict):
        if "value" in field:
            return field["value"]
    return field


def strip_quotes(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def extract_telnet_cmd(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = TELNET_ARGS_RE.search(text)
    if m:
        ip, port = m.group(1), m.group(2)
        return f"telnet {ip} {port}"

    m2 = TELNET_CMD_RE.search(text)
    if m2:
        ip, port = m2.group(1), m2.group(2)
        return f"telnet {ip} {port}"

    return None
