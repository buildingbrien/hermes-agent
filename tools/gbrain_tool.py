#!/usr/bin/env python3
"""
GBrain Recall — read the compiled memory the dream cycle writes every night.

Until now gbrain was WRITE-ONLY from an agent's point of view: the nightly
dream cycle consolidated the day into 850+ compiled pages, and not one tool in
the runtime could read them back. Recall therefore meant grepping the
filesystem and re-deriving settled facts from raw markdown — six tool calls and
~2.5 minutes to answer "what did we decide about the logos", on 2026-08-16,
for a decision the brain already held as a compiled page.

It also explains the class of failure the founder had been reporting for
weeks: agents reminding him about a contract that was already signed, and
forgetting a project that has its own page. Not a memory-quality problem —
nothing could read the memory.

This exposes two verbs:
  gbrain_search(query)  — find compiled pages by title/slug/content
  gbrain_read(slug)     — read one page's compiled truth in full

Degrades quietly: a machine without gbrain (or with it stopped) reports that
recall is unavailable rather than failing the turn.
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GBRAIN_URL = os.environ.get("GBRAIN_URL", "postgres://localhost:5432/gbrain")
_MAX_CHARS = 6000          # one page's truth, trimmed for a context window
_SNIPPET = 320


def _psql(sql: str, timeout: float = 15.0) -> Optional[List[List[str]]]:
    """Run one read-only query. Returns rows split on the unit separator, or
    None when gbrain is simply not reachable on this machine."""
    if not shutil.which("psql"):
        return None
    try:
        out = subprocess.run(
            ["psql", GBRAIN_URL, "-tA", "-F", "\x1f", "-c", sql],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("gbrain query failed: %s", e)
        return None
    if out.returncode != 0:
        logger.warning("gbrain query error: %s", (out.stderr or "")[:200])
        return None
    return [ln.split("\x1f") for ln in out.stdout.splitlines() if ln.strip()]


def _q(text: str) -> str:
    """Single-quote escape for inline SQL literals."""
    return (text or "").replace("'", "''")


def gbrain_search(query: str, limit: int = 5) -> str:
    """Find compiled pages about a topic, person or company."""
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query is required"})
    like = f"%{_q(q)}%"
    # Title/slug hits first — an entity page named for the thing you asked
    # about is a better answer than a passing mention inside another page.
    rows = _psql(
        "SELECT slug, coalesce(title,''), "
        "left(regexp_replace(coalesce(compiled_truth,''), '\\s+', ' ', 'g'), "
        f"{_SNIPPET}), length(coalesce(compiled_truth,'')), "
        "coalesce(to_char(updated_at,'YYYY-MM-DD'),'') "
        "FROM pages "
        f"WHERE (title ILIKE '{like}' OR slug ILIKE '{like}' "
        f"       OR compiled_truth ILIKE '{like}') "
        "AND coalesce(compiled_truth,'') <> '' "
        f"ORDER BY (title ILIKE '{like}' OR slug ILIKE '{like}') DESC, "
        "updated_at DESC NULLS LAST, length(compiled_truth) DESC "
        f"LIMIT {max(1, min(int(limit or 5), 12))};"
    )
    if rows is None:
        return json.dumps({
            "error": "gbrain is not reachable on this machine",
            "recall_available": False,
        })
    if not rows:
        return json.dumps({"query": q, "results": [],
                           "note": "nothing compiled about this yet"})
    return json.dumps({
        "query": q,
        "results": [
            {"slug": r[0], "title": r[1], "snippet": r[2],
             "chars": int(r[3] or 0), "updated": r[4],
             "read_with": f"gbrain_read(slug='{r[0]}')"}
            for r in rows if len(r) >= 5
        ],
    }, indent=2)


def gbrain_read(slug: str) -> str:
    """Read one compiled page in full — the settled version of what is known."""
    s = (slug or "").strip()
    if not s:
        return json.dumps({"error": "slug is required"})
    rows = _psql(
        "SELECT slug, coalesce(title,''), coalesce(compiled_truth,''), "
        "coalesce(to_char(updated_at,'YYYY-MM-DD'),'') "
        f"FROM pages WHERE slug = '{_q(s)}' LIMIT 1;"
    )
    if rows is None:
        return json.dumps({"error": "gbrain is not reachable on this machine",
                           "recall_available": False})
    if not rows:
        return json.dumps({"error": f"no compiled page at slug '{s}'",
                           "hint": "use gbrain_search first"})
    r = rows[0]
    truth = r[2] if len(r) > 2 else ""
    return json.dumps({
        "slug": r[0], "title": r[1], "updated": r[3] if len(r) > 3 else "",
        "truncated": len(truth) > _MAX_CHARS,
        "compiled_truth": truth[:_MAX_CHARS],
    }, indent=2)


def check_gbrain_requirements() -> Dict[str, Any]:
    """Available only where the brain actually is."""
    if not shutil.which("psql"):
        return {"available": False, "reason": "psql not installed"}
    if _psql("SELECT 1;", timeout=6.0) is None:
        return {"available": False, "reason": "gbrain database not reachable"}
    return {"available": True}


GBRAIN_SEARCH_SCHEMA = {
    "name": "gbrain_search",
    "description": (
        "Search the user's long-term compiled memory (gbrain) for what is "
        "already KNOWN about a person, company, project or decision. Use this "
        "FIRST for any recall question — 'what did we decide', 'who is X', "
        "'where did we land on Y', 'what's the status of Z' — before searching "
        "files or past transcripts. It returns settled, consolidated facts, so "
        "it is far faster and more accurate than re-deriving them from raw "
        "documents."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Topic, person, company or decision to recall."},
            "limit": {"type": "integer",
                      "description": "Max pages to return (default 5)."},
        },
        "required": ["query"],
    },
}

GBRAIN_READ_SCHEMA = {
    "name": "gbrain_read",
    "description": (
        "Read one compiled memory page in full, by the slug returned from "
        "gbrain_search. Use when the search snippet is not enough detail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string",
                     "description": "Page slug, e.g. 'people/courtenay-rushing'."},
        },
        "required": ["slug"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error  # noqa: E402

registry.register(
    name="gbrain_search",
    toolset="memory",
    schema=GBRAIN_SEARCH_SCHEMA,
    handler=lambda args, **kw: gbrain_search(
        query=args.get("query") or "",
        limit=args.get("limit", 5)),
    check_fn=check_gbrain_requirements,
    emoji="🧠",
)

registry.register(
    name="gbrain_read",
    toolset="memory",
    schema=GBRAIN_READ_SCHEMA,
    handler=lambda args, **kw: gbrain_read(slug=args.get("slug") or ""),
    check_fn=check_gbrain_requirements,
    emoji="🧠",
)
