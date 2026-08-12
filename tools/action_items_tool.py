"""action_items — the durable store behind "I'll note that" and "consider it closed".

Exists because of a pattern proven in transcripts on 2026-08-11: in one voice
call an agent said "I'll mark that one closed everywhere", "noting it for the
Thursday call", and "noting it as fully closed everywhere now" — six
promise-shaped statements, zero tool calls behind any of them. Nothing was
written anywhere; a completed item kept resurfacing from stale meeting notes
call after call, and the user had to correct the agent three times.

The rule this tool enforces by existing: a commitment is a ROW, not a
sentence. If you say you'll track, note, close, or follow up on something,
call this tool in the same turn — otherwise say you can't.

Storage is one shared JSON file under HERMES_HOME's parent scope so every
agent reads the same list: an item closed in a voice call with Ptah stays
closed when Thoth briefs tomorrow's meeting.
"""

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

STORE_ENV = "LUCARYIN_ACTION_ITEMS_PATH"


def _store_path() -> str:
    explicit = os.environ.get(STORE_ENV)
    if explicit:
        return os.path.expanduser(explicit)
    # ~/.lucaryin is machine-scoped (shared across agent profiles), which is
    # the point: one list, every agent.
    return os.path.expanduser("~/.lucaryin/action_items.json")


def _load() -> List[Dict[str, Any]]:
    try:
        with open(_store_path()) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: List[Dict[str, Any]]) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, path)


def _match(items: List[Dict[str, Any]], needle: str) -> Optional[Dict[str, Any]]:
    """Find by id first, then by case-insensitive substring of the title."""
    needle = (needle or "").strip()
    if not needle:
        return None
    for it in items:
        if it.get("id") == needle:
            return it
    low = needle.lower()
    hits = [it for it in items if low in (it.get("title") or "").lower()]
    return hits[0] if len(hits) == 1 else None


def action_items_tool(args: Dict[str, Any], **_kw) -> Dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    op = (args.get("op") or "list").strip().lower()
    items = _load()

    if op == "add":
        title = (args.get("title") or "").strip()
        if not title:
            return {"error": "An action item needs a title."}
        # Never store the same open commitment twice — re-adding an existing
        # open item just returns it, so repeated calls are idempotent.
        low = title.lower()
        for it in items:
            if it.get("status") == "open" and (it.get("title") or "").lower() == low:
                return {"item": it, "note": "already tracked (open)"}
        item = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "status": "open",
            "owner": (args.get("owner") or "").strip(),
            "due": (args.get("due") or "").strip(),
            "source": (args.get("source") or "").strip(),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        items.append(item)
        _save(items)
        return {"item": item, "open_count": sum(1 for i in items if i["status"] == "open")}

    if op in ("close", "reopen"):
        target = _match(items, args.get("id") or args.get("title") or "")
        if not target:
            return {"error": "No unique match — pass the item id, or a title "
                             "substring that matches exactly one item.",
                    "open_items": [{"id": i["id"], "title": i["title"]}
                                   for i in items if i["status"] == "open"][:20]}
        target["status"] = "closed" if op == "close" else "open"
        target["updated_at"] = time.time()
        if op == "close" and args.get("resolution"):
            target["resolution"] = str(args["resolution"])[:300]
        _save(items)
        return {"item": target}

    if op == "list":
        status = (args.get("status") or "open").strip().lower()
        if status == "all":
            out = items
        else:
            out = [i for i in items if i.get("status") == status]
        out = sorted(out, key=lambda i: i.get("updated_at", 0), reverse=True)
        return {"items": out[:50], "count": len(out)}

    return {"error": f"Unknown op '{op}'. Use add, close, reopen, or list."}


ACTION_ITEMS_SCHEMA = {
    "name": "action_items",
    "description": (
        "The durable, cross-agent store for commitments and follow-ups. "
        "RULE: if you tell the user you'll track, note, close, or follow up "
        "on something, call this tool in the SAME turn — a commitment without "
        "a row here does not exist, and another agent will re-raise it. "
        "Before presenting action items from meeting notes or transcripts, "
        "check `list` first: items closed here are DONE even if old notes "
        "still mention them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add", "close", "reopen", "list"],
                   "description": "What to do. Default: list."},
            "title": {"type": "string",
                      "description": "The commitment, stated so a colleague could act on it."},
            "id": {"type": "string", "description": "Item id (for close/reopen)."},
            "owner": {"type": "string", "description": "Who owns it (person or agent)."},
            "due": {"type": "string", "description": "When it's due, if said (free text)."},
            "source": {"type": "string",
                       "description": "Where it came from, e.g. 'voice call 2026-08-11'."},
            "status": {"type": "string", "enum": ["open", "closed", "all"],
                       "description": "For list: which items. Default open."},
            "resolution": {"type": "string",
                           "description": "For close: how it was resolved."},
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="action_items",
    toolset="memory",
    schema=ACTION_ITEMS_SCHEMA,
    handler=action_items_tool,
    emoji="📌",
)
