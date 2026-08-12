"""read_document — text extraction from binary documents (PDF first).

read_file hands back raw bytes-as-text, which for a PDF is garbage — cron
jobs literally reported "I can't read PDFs" while inbox attachments piled
up (2026-08-11). This tool is the sanctioned path: give it a path, get the
text, with page selection so a 300-page lease doesn't flood the context.

Deliberately extraction-only: no OCR (a scanned PDF with no text layer says
so honestly instead of returning empty pages), no writes, no network.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

MAX_CHARS_DEFAULT = 40_000
MAX_CHARS_CAP = 120_000


def _parse_pages(spec: Any, total: int) -> Optional[List[int]]:
    """'1-5', '3', '2,7,9-11' → zero-based page indexes. None = all pages."""
    if spec is None or str(spec).strip() == "":
        return None
    indexes: List[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(part)
        if lo < 1 or hi < lo:
            raise ValueError(f"Bad page range: {part!r} (pages are 1-based)")
        for p in range(lo, min(hi, total) + 1):
            indexes.append(p - 1)
    return indexes or None


def _extract_pdf(path: str, pages_spec: Any) -> Tuple[str, int, List[int], bool]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    total = len(reader.pages)
    indexes = _parse_pages(pages_spec, total)
    chosen = indexes if indexes is not None else list(range(total))

    chunks: List[str] = []
    any_text = False
    for i in chosen:
        text = (reader.pages[i].extract_text() or "").strip()
        if text:
            any_text = True
            chunks.append(f"--- page {i + 1} ---\n{text}")
        else:
            chunks.append(f"--- page {i + 1} --- (no extractable text)")
    return "\n\n".join(chunks), total, [i + 1 for i in chosen], any_text


def read_document_tool(args: Dict[str, Any], **_kw) -> Dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    path = os.path.expanduser(str(args.get("path") or "").strip())
    if not path:
        return {"error": "No path. Pass 'path' to the document."}
    if not os.path.isfile(path):
        return {"error": f"No such file: {path}"}

    try:
        max_chars = int(args.get("max_chars") or MAX_CHARS_DEFAULT)
    except (TypeError, ValueError):
        max_chars = MAX_CHARS_DEFAULT
    max_chars = max(1_000, min(max_chars, MAX_CHARS_CAP))

    ext = os.path.splitext(path)[1].lower()
    if ext != ".pdf":
        return {
            "error": f"read_document handles PDFs; {ext or 'this file'} is not one. "
                     f"For text-based files use read_file."
        }

    try:
        text, total_pages, pages_read, any_text = _extract_pdf(path, args.get("pages"))
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — a broken PDF should say why
        return {"error": f"Could not read PDF: {e}"}

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    if not any_text:
        return {
            "path": path,
            "total_pages": total_pages,
            "pages_read": pages_read,
            "text": "",
            "note": (
                "No extractable text on these pages — this is likely a scanned "
                "PDF (images only, no text layer). OCR is not available here; "
                "tell the user which file needs it rather than guessing at the "
                "contents."
            ),
        }

    result: Dict[str, Any] = {
        "path": path,
        "total_pages": total_pages,
        "pages_read": pages_read,
        "text": text,
    }
    if truncated:
        result["note"] = (
            f"Truncated at {max_chars} characters. Narrow with pages='N-M' "
            f"(document has {total_pages} pages) or raise max_chars."
        )
    return result


READ_DOCUMENT_SCHEMA = {
    "name": "read_document",
    "description": (
        "Extract the text of a PDF document. Use this instead of read_file "
        "for .pdf paths (read_file returns unreadable raw bytes for PDFs). "
        "Supports page selection for long documents. Scanned PDFs without a "
        "text layer are reported as such — no OCR."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the PDF file."},
            "pages": {
                "type": "string",
                "description": "Optional 1-based page selection, e.g. '1-5', '3', '2,7,9-11'. Default: all pages.",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Cap on returned text (default {MAX_CHARS_DEFAULT}).",
            },
        },
        "required": ["path"],
    },
}


def check_read_document_requirements() -> tuple:
    try:
        import pypdf  # noqa: F401
        return True, ""
    except ImportError:
        return False, "pypdf is not installed in this environment"


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="read_document",
    toolset="file",
    schema=READ_DOCUMENT_SCHEMA,
    handler=read_document_tool,
    check_fn=check_read_document_requirements,
    emoji="📄",
)
