import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional, Tuple


PASS_COMMENT = "Pass"

# Only scan these summary markers (case/punct-insensitive), mirroring the original precheck script.
_ALLOWED_TOKENS_RAW = ["pre-rlt", "prerlt", "precheck", "pre-check", "pre rlt", "pre check"]


def _norm_summary(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_ALLOWED_TOKENS = {_norm_summary(t) for t in _ALLOWED_TOKENS_RAW}


def summary_has_precheck_marker(summary: str) -> bool:
    ns = _norm_summary(summary)
    return any(tok in ns for tok in _ALLOWED_TOKENS)


# ---- Fuzzy phrase matcher (ported from precheck/jiraprecheck.py; behavior preserved) ----
_REQUIRED = {"please", "start", "rlt", "without", "wait", "te", "respond"}
_OPTIONAL = {"the", "for"}
_ALIASES = {
    "te": {"te", "tes", "te's", "te s"},
    "wait": {"wait", "waiting"},
    "respond": {"respond", "response", "responding"},
}


def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokenize(text: str):
    return _norm_text(text).split()


def _canonicalize(tok: str) -> str:
    for k, forms in _ALIASES.items():
        if tok in forms:
            return k
    return tok


def text_has_target_line(text: str) -> bool:
    """
    True iff there exists a contiguous token window that:
      * contains all REQUIRED tokens (considering ALIASES),
      * may include OPTIONAL tokens ('the','for'),
      * and contains NO other tokens.
    Order within the window does not matter.
    """
    toks = [_canonicalize(t) for t in _tokenize(text)]

    if not _REQUIRED.issubset({_canonicalize(t) for t in _tokenize(text)}):
        return False

    allowed = _REQUIRED | _OPTIONAL
    need_n = len(_REQUIRED)

    left = 0
    counts: Dict[str, int] = {}
    have = 0

    def add(tok: str):
        nonlocal have
        counts[tok] = counts.get(tok, 0) + 1
        if tok in _REQUIRED and counts[tok] == 1:
            have += 1

    for right, tok in enumerate(toks):
        add(tok)

        if tok not in allowed:
            counts.clear()
            have = 0
            left = right + 1
            continue

        if have == need_n and left <= right:
            return True

    return False


_IMG_MIME_RE = re.compile(r"^image/")


def _attachment_is_image(att: Dict[str, Any]) -> bool:
    mime = str(att.get("mimeType") or "").lower()
    filename = str(att.get("filename") or "").lower()
    if _IMG_MIME_RE.match(mime):
        return True
    for ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"):
        if filename.endswith(ext):
            return True
    return False


def _ocr_debug_enabled() -> bool:
    return os.getenv("JIRA_OCR_DEBUG", "0").lower() not in ("0", "false", "")


def _ocr_debug_dir() -> str:
    return os.getenv("JIRA_OCR_DEBUG_DIR", os.path.expanduser("~/ocr_debug"))


def _dbg_write(name: str, content: str) -> None:
    if not _ocr_debug_enabled():
        return
    try:
        os.makedirs(_ocr_debug_dir(), exist_ok=True)
        path = os.path.join(_ocr_debug_dir(), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        print(f"[DEBUG] wrote {path}")
    except Exception as exc:
        print(f"[DEBUG] failed to write debug file {name}: {exc}")


_rapid_ocr = None


def _get_rapid_ocr():
    global _rapid_ocr
    if _rapid_ocr is None:
        from rapidocr_onnxruntime import RapidOCR

        _rapid_ocr = RapidOCR()
    return _rapid_ocr


def _ocr_image_bytes(img_bytes: bytes, *, dump_basename: Optional[str] = None) -> str:
    if not img_bytes:
        return ""

    try:
        from PIL import Image
        import numpy as np
    except Exception as exc:
        raise RuntimeError(
            "Missing OCR dependencies. Install Pillow, numpy, rapidocr-onnxruntime, and onnxruntime."
        ) from exc

    try:
        img = Image.open(__import__("io").BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return ""

    texts = []
    ocr = _get_rapid_ocr()
    for scale in (1.0, 1.75):
        try:
            scaled = img
            if scale != 1.0:
                w, h = img.size
                scaled = img.resize((int(w * scale), int(h * scale)), resample=Image.LANCZOS)

            arr = np.asarray(scaled)
            result, _ = ocr(arr)
            if not result:
                continue
            chunk = "\n".join((r[1] or "") for r in result if isinstance(r, (list, tuple)) and len(r) >= 2)
            if chunk.strip():
                texts.append(chunk)
                if dump_basename:
                    _dbg_write(f"{dump_basename}_scale_{scale}.txt", chunk)
        except Exception:
            continue

    return "\n".join(texts).strip()


def populate_precheck_context(*, error_event: Any, jira: Any) -> None:
    """
    Populate precheck-specific fields on ErrorEvent.

    This is a read-only enrichment step:
      - checks latest comment (spam prevention),
      - checks description + comments for the target phrase,
      - OCRs image attachments if needed.
    """
    ticket = getattr(error_event, "ticket", None)
    summary = getattr(ticket, "summary", "") if ticket else ""

    marker_found = summary_has_precheck_marker(summary)
    setattr(error_event, "precheck_marker_found", bool(marker_found))

    latest = (getattr(error_event, "jira_latest_comment_text", "") or "").strip()
    latest_is_pass = latest.lower() == PASS_COMMENT.lower()
    setattr(error_event, "precheck_latest_comment_is_pass", bool(latest_is_pass))

    if not marker_found:
        setattr(error_event, "precheck_phrase_found", False)
        setattr(error_event, "precheck_phrase_source", None)
        return

    if latest_is_pass:
        setattr(error_event, "precheck_phrase_found", False)
        setattr(error_event, "precheck_phrase_source", None)
        return

    description = getattr(ticket, "description", "") if ticket else ""
    if description and text_has_target_line(description):
        setattr(error_event, "precheck_phrase_found", True)
        setattr(error_event, "precheck_phrase_source", "description")
        return

    comments_text = getattr(error_event, "jira_comments_text", "") or ""
    if comments_text and text_has_target_line(comments_text):
        setattr(error_event, "precheck_phrase_found", True)
        setattr(error_event, "precheck_phrase_source", "comments")
        return

    # OCR attachments (only images). Attachments come from ticket.raw fields.
    raw = getattr(ticket, "raw", {}) if ticket else {}
    fields = (raw.get("fields") or {}) if isinstance(raw, dict) else {}
    atts = fields.get("attachment") or []
    if not isinstance(atts, list) or not atts:
        setattr(error_event, "precheck_phrase_found", False)
        setattr(error_event, "precheck_phrase_source", None)
        return

    # Performance knobs (mirror precheck script defaults)
    max_workers = int(os.getenv("RACKBRAIN_PRECHECK_MAX_ATTACHMENT_WORKERS", "2"))

    def _scan_one(att: Dict[str, Any]) -> Tuple[bool, str]:
        if not _attachment_is_image(att):
            return (False, "")

        url = att.get("content") or ""
        if not url:
            return (False, "")

        try:
            content = jira.download_url_bytes(str(url))
        except Exception as exc:
            if _ocr_debug_enabled():
                print(f"[WARN] precheck: attachment download failed: {att.get('filename')}: {exc}")
            return (False, "")

        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(att.get("filename") or "attachment"))
        dump_base = None
        if _ocr_debug_enabled():
            key = getattr(getattr(error_event, "ticket", None), "key", "") or "UNKNOWN"
            dump_base = f"{key}_{safe_name}"

        txt = _ocr_image_bytes(content, dump_basename=dump_base)
        if txt and text_has_target_line(txt):
            return (True, safe_name)
        return (False, "")

    matched_attachment = None
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_scan_one, a) for a in atts]
        for fut in as_completed(futures):
            ok, name = fut.result()
            if ok:
                matched_attachment = name or "attachment"
                break

    if matched_attachment:
        setattr(error_event, "precheck_phrase_found", True)
        setattr(error_event, "precheck_phrase_source", f"attachment:{matched_attachment}")
    else:
        setattr(error_event, "precheck_phrase_found", False)
        setattr(error_event, "precheck_phrase_source", None)

