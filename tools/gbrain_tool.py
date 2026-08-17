#!/usr/bin/env python3
"""
GBrain Recall — read the compiled memory the dream cycle writes every night.

Until v4.6.38 gbrain was WRITE-ONLY from an agent's point of view: the nightly
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

WHY A DRIVER AND NOT psql: the first version shelled out to the `psql` binary
and gated availability on a PATH lookup of that binary. That check passes in every
terminal (where Homebrew's /opt/homebrew/bin is on PATH) and fails inside the
actual product: a Finder-launched app inherits launchd's PATH, which has no
Homebrew, so every worker the bridge spawned reported "gbrain is not reachable"
against a perfectly healthy database — for the entire release that shipped the
feature. The founder found it by asking an agent a question the brain could
answer and watching it come back empty. A Python driver has no PATH dependency
and works the same from a terminal, from Finder, and on a customer Mac that
never had Homebrew. (psycopg ships via hermes-bridge/requirements.txt, so the
same provisioning pass that delivers this file delivers the driver.)

Degrades quietly either way: a machine without gbrain (or with the driver
missing) reports that recall is unavailable rather than failing the turn.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GBRAIN_URL = os.environ.get("GBRAIN_URL", "postgres://localhost:5432/gbrain")
_MAX_CHARS = 6000          # one page's truth, trimmed for a context window
_SNIPPET = 320
_CONNECT_TIMEOUT = 6.0     # local postgres; anything slower is "down"


def _query(sql: str, params: tuple = ()) -> Optional[List[List[str]]]:
    """Run one read-only, parameterized query. Returns rows as strings, or
    None when gbrain is simply not reachable on this machine.

    Parameterization replaces the old hand-rolled quote-escaping that the
    psql shell-out required — the driver does it properly.
    """
    try:
        import psycopg
    except ImportError:
        logger.warning("gbrain recall unavailable: psycopg driver not installed")
        return None
    try:
        with psycopg.connect(GBRAIN_URL, connect_timeout=_CONNECT_TIMEOUT) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [["" if v is None else str(v) for v in row] for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning("gbrain query failed: %s", e)
        return None


def gbrain_search(query: str, limit: int = 5) -> str:
    """Find compiled pages about a topic, person or company."""
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query is required"})
    like = f"%{q}%"
    n = max(1, min(int(limit or 5), 12))
    # Title/slug hits first — an entity page named for the thing you asked
    # about is a better answer than a passing mention inside another page.
    rows = _query(
        "SELECT slug, coalesce(title,''), "
        "left(regexp_replace(coalesce(compiled_truth,''), '\\s+', ' ', 'g'), %s), "
        "length(coalesce(compiled_truth,'')), "
        "coalesce(to_char(updated_at,'YYYY-MM-DD'),'') "
        "FROM pages "
        "WHERE (title ILIKE %s OR slug ILIKE %s OR compiled_truth ILIKE %s) "
        "AND coalesce(compiled_truth,'') <> '' "
        "ORDER BY (title ILIKE %s OR slug ILIKE %s) DESC, "
        "updated_at DESC NULLS LAST, length(compiled_truth) DESC "
        "LIMIT %s;",
        (_SNIPPET, like, like, like, like, like, n),
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
    rows = _query(
        "SELECT slug, coalesce(title,''), coalesce(compiled_truth,''), "
        "coalesce(to_char(updated_at,'YYYY-MM-DD'),'') "
        "FROM pages WHERE slug = %s LIMIT 1;",
        (s,),
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
    """Available only where the brain actually is.

    The reasons are deliberately distinct: "driver not installed" means the
    venv needs a refresh (a provisioning problem on OUR side), while
    "database not reachable" means this machine simply has no gbrain (normal
    for a plain hermes-agent host). The first one should never be silently
    read as the second.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return {"available": False,
                "reason": "psycopg driver not installed (venv needs refresh)"}
    if _query("SELECT 1;") is None:
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
