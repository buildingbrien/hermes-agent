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

WHY THE :9050 BRIDGE AND NOT A DB DRIVER: the previous version connected
psycopg directly to a hardcoded postgres://localhost:5432/gbrain. That works on
the founder's box (hand-built Postgres) and NOWHERE ELSE: the app provisions a
PGLite brain on every customer machine, which has no :5432 server, so recall
connected to a dead port, quietly returned "not reachable", and the registry
dropped both tools against a perfectly healthy PGLite brain — for every customer
(found 2026-08-24, the same class as the earlier psql-PATH scar). Routing
through the local gbrain bridge (:9050, already a supervised bridge-manager
child) fixes it for good: the bridge shells to the `gbrain` CLI, which reads
~/.gbrain/config.json and so speaks PGLite and Postgres identically. Loopback
HTTP has no PATH dependency and no engine assumption, and needs no psycopg.

Degrades quietly either way: a machine without gbrain (or with the bridge down)
reports that recall is unavailable rather than failing the turn.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The local gbrain bridge — engine-agnostic recall over loopback (see the
# module docstring). Overridable for a non-default port, but every bridge in the
# fleet runs it on :9050.
GBRAIN_BRIDGE = os.environ.get("GBRAIN_BRIDGE_URL", "http://127.0.0.1:9050")
_MAX_CHARS = 6000          # one page's truth, trimmed for a context window
_SNIPPET = 320
_TIMEOUT = 8.0             # loopback; anything slower is "down"

# Sentinel: the bridge responded with an HTTP error (it is UP, so this is a real
# answer like 404-no-page), distinct from None (bridge unreachable = no recall).
_HTTP_ERROR = "__http_error__"


def _bridge_get(path: str, params: Dict[str, str]) -> Optional[Any]:
    """GET a gbrain-bridge endpoint on :9050.

    Returns the decoded JSON body on success; a {"__http_error__": code} marker
    when the bridge answered with an HTTP error (it is reachable — e.g. a 404
    for an unknown slug); or None when the bridge itself is unreachable (no
    gbrain on this machine, or the bridge is down), which the caller renders as
    'recall unavailable' rather than failing the turn.
    """
    url = f"{GBRAIN_BRIDGE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("gbrain bridge %s → HTTP %s", path, e.code)
        return {_HTTP_ERROR: e.code}
    except Exception as e:  # noqa: BLE001 — any transport failure = unavailable
        logger.warning("gbrain bridge %s unreachable: %s", path, e)
        return None


def gbrain_search(query: str, limit: int = 5) -> str:
    """Find compiled pages about a topic, person or company."""
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query is required"})
    n = max(1, min(int(limit or 5), 12))
    body = _bridge_get("/api/gbrain/search", {"q": q})
    if body is None or (isinstance(body, dict) and _HTTP_ERROR in body):
        return json.dumps({
            "error": "gbrain is not reachable on this machine",
            "recall_available": False,
        })
    data = body.get("results") if isinstance(body, dict) else None
    results = _parse_search_results(data, n)
    if not results:
        return json.dumps({"query": q, "results": [],
                           "note": "nothing compiled about this yet"})
    return json.dumps({"query": q, "results": results}, indent=2)


def _parse_search_results(data: Any, n: int) -> List[Dict[str, Any]]:
    """Normalise the bridge's `results` into a list of {slug,snippet,score,...}.

    The gbrain CLI's `search --json` emits `data` as a formatted TEXT block —
    one hit per record, "[score] slug -- snippet" (snippets can wrap lines) —
    not structured rows, so we parse that. A future CLI that returns a real list
    of dicts is also handled (the isinstance(list) branch), so this tool doesn't
    break either way.
    """
    results: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data[:n]:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("id") or "").strip()
            snippet = (item.get("snippet") or item.get("summary")
                       or str(item.get("content") or ""))[:_SNIPPET]
            results.append({
                "slug": slug,
                "title": item.get("title", ""),
                "snippet": snippet,
                "read_with": f"gbrain_read(slug='{slug}')" if slug else None,
            })
        return results
    if isinstance(data, str) and data.strip():
        import re
        # Each hit starts with "[<score>]"; snippet runs to the next "[score]"
        # marker or end-of-string, so multi-line snippets stay intact.
        pat = re.compile(
            r"\[(?P<score>[0-9.]+)\]\s+(?P<slug>\S+)"
            r"(?:\s+--\s+(?P<snippet>.*?))?(?=\n\[[0-9.]+\]|\Z)",
            re.S,
        )
        for m in pat.finditer(data):
            slug = m.group("slug").strip()
            snippet = (m.group("snippet") or "").strip()
            # collapse internal whitespace so the snippet reads on one line
            snippet = re.sub(r"\s+", " ", snippet)[:_SNIPPET]
            results.append({
                "slug": slug,
                "snippet": snippet,
                "score": float(m.group("score")),
                "read_with": f"gbrain_read(slug='{slug}')",
            })
            if len(results) >= n:
                break
    return results


def gbrain_read(slug: str) -> str:
    """Read one compiled page in full — the settled version of what is known."""
    s = (slug or "").strip()
    if not s:
        return json.dumps({"error": "slug is required"})
    body = _bridge_get("/api/gbrain/page", {"slug": s})
    if body is None:
        return json.dumps({"error": "gbrain is not reachable on this machine",
                           "recall_available": False})
    if isinstance(body, dict) and _HTTP_ERROR in body:
        # The bridge answered — a 404 means there is simply no such page.
        return json.dumps({"error": f"no compiled page at slug '{s}'",
                           "hint": "use gbrain_search first"})
    content = str(body.get("content", "")) if isinstance(body, dict) else ""
    if not content:
        return json.dumps({"error": f"no compiled page at slug '{s}'",
                           "hint": "use gbrain_search first"})
    return json.dumps({
        "slug": s,
        "truncated": len(content) > _MAX_CHARS,
        "compiled_truth": content[:_MAX_CHARS],
    }, indent=2)


def check_gbrain_requirements() -> Dict[str, Any]:
    """Available only where the brain actually is.

    Probes the gbrain bridge's /health. A reachable, healthy bridge means recall
    works (on either engine); an unreachable bridge means this machine simply
    has no gbrain (normal for a plain hermes-agent host) — reported as
    unavailable so the registry hides the tools rather than offering ones that
    would only ever answer "not reachable".
    """
    body = _bridge_get("/health", {})
    if body is None:
        return {"available": False, "reason": "gbrain bridge not reachable (:9050)"}
    if isinstance(body, dict) and _HTTP_ERROR in body:
        return {"available": False,
                "reason": f"gbrain bridge unhealthy (HTTP {body[_HTTP_ERROR]})"}
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
