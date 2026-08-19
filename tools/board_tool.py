"""board — the office's shared working table, from the agent's side.

The bulletin at the center of the workspace is the user's one glance at
what their team is producing (strategy doc: workspace-scene-strategy
2026-08-18). Agents keep it MEANINGFUL, and these tools are the only way
they touch it:

  board_view          read the whole board + counters (free)
  counter_create      register a named metric to accrue (free)
  counter_increment   count COMPLETED work — audited, never speculative
  board_update        refresh CONTENT of an accepted widget (free)
  board_propose       suggest a NEW widget — ghost + approval card

The line that never moves: increment a counter only for work that actually
finished. A bulletin that inflates its numbers is worse than no bulletin.
"""

import json
import os
import urllib.error
import urllib.request

from tools.registry import tool_error, registry

AGENT_PORTS = {"thoth": 9001, "neith": 9007, "ptah": 9005, "set": 9006}


def _bridge_url(path: str) -> str:
    port = os.environ.get("HERMES_SERVER_PORT", "").strip()
    if not port:
        profile = (os.environ.get("BRIDGE_PROFILE", "") or "thoth").strip().lower()
        port = str(AGENT_PORTS.get(profile, 9001))
    return f"http://127.0.0.1:{port}{path}"


def _agent() -> str:
    return (os.environ.get("BRIDGE_PROFILE", "") or "thoth").strip().lower()


def _post(body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        _bridge_url("/api/board/agent"),
        data=json.dumps(body).encode(),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _call(body: dict):
    try:
        result = _post(body)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            detail = ""
        return tool_error(detail or f"board request failed (HTTP {e.code})")
    except Exception as e:  # noqa: BLE001
        return tool_error(f"could not reach the board: {e}")
    if not result.get("success"):
        return tool_error(result.get("error", "board request failed"))
    return json.dumps(result)


def board_view_tool(args, **kwargs):
    return _call({"op": "view", "agent": _agent()})


def board_propose_tool(args, **kwargs):
    widget = args.get("widget") if isinstance(args.get("widget"), dict) else {}
    if not widget:
        return tool_error("pass the widget object: {type, title, config, layout?}")
    return _call({"op": "propose", "agent": _agent(), "widget": widget})


def board_update_tool(args, **kwargs):
    wid = (args.get("widget_id") or "").strip()
    patch = args.get("patch") if isinstance(args.get("patch"), dict) else {}
    if not wid or not patch:
        return tool_error("pass widget_id and a patch ({config: {...}} and/or {title})")
    return _call({"op": "update", "agent": _agent(),
                  "widget_id": wid, "patch": patch})


def counter_create_tool(args, **kwargs):
    name = (args.get("name") or "").strip()
    if not name:
        return tool_error("pass name (slug like 'emails_handled_google')")
    return _call({"op": "counter_create", "agent": _agent(), "name": name,
                  "label": args.get("label") or "", "unit": args.get("unit") or ""})


def counter_increment_tool(args, **kwargs):
    name = (args.get("name") or "").strip()
    if not name:
        return tool_error("pass the counter name")
    return _call({"op": "counter_increment", "agent": _agent(), "name": name,
                  "by": args.get("by", 1), "note": args.get("note") or ""})


_WIDGET_SHAPE = (
    "Widget shape: {type: metric|counter|sparkline|list|note|status_lamp|"
    "feed_card|control, title, config: {...}, layout?: {col,row,w,h}}. "
    "metric/counter/sparkline reference a counter via config.counter."
)

BOARD_VIEW_SCHEMA = {
    "name": "board_view",
    "description": (
        "Read the office's shared bulletin board: every widget (active and "
        "proposed) plus all named counters with daily history. Free — check "
        "the board before proposing or updating anything."
    ),
    "parameters": {"type": "object", "properties": {}},
}

BOARD_PROPOSE_SCHEMA = {
    "name": "board_propose",
    "description": (
        "Propose a NEW widget for the shared board. It appears dimmed on the "
        "board and files an approval card — the USER's decision activates or "
        "removes it; never claim it is on the board until accepted. Max 3 "
        "undecided proposals per agent. " + _WIDGET_SHAPE
    ),
    "parameters": {
        "type": "object",
        "properties": {"widget": {"type": "object",
                                  "description": _WIDGET_SHAPE}},
        "required": ["widget"],
    },
}

BOARD_UPDATE_SCHEMA = {
    "name": "board_update",
    "description": (
        "Update the CONTENT of an accepted widget you maintain (metric value, "
        "note text, list items…). Structural changes (type, layout, controls) "
        "are rejected — use board_propose for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "widget_id": {"type": "string"},
            "patch": {"type": "object",
                      "description": "{config: {...}} and/or {title}"},
        },
        "required": ["widget_id", "patch"],
    },
}

COUNTER_CREATE_SCHEMA = {
    "name": "counter_create",
    "description": (
        "Register a named counter for work you will track on the board, e.g. "
        "'emails_handled_google'. Idempotent. Counters keep daily history so "
        "sparklines work from day one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "slug: lowercase, digits, underscores"},
            "label": {"type": "string"},
            "unit": {"type": "string", "description": "e.g. 'emails', 'calls'"},
        },
        "required": ["name"],
    },
}

COUNTER_INCREMENT_SCHEMA = {
    "name": "counter_increment",
    "description": (
        "Count COMPLETED work on a board counter. HARD RULE: increment only "
        "for work that actually finished — never for attempts, plans, or "
        "work you merely described. Every increment is audited with your "
        "name. Use note to say what the work was."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "by": {"type": "integer", "description": "default 1"},
            "note": {"type": "string", "description": "what completed, briefly"},
        },
        "required": ["name"],
    },
}


# Registration calls stay TOP-LEVEL and explicit: discover_builtin_tools
# recognizes a tool module by scanning tree.body for registry.register(...)
# statements — a loop would make this module invisible to discovery.
registry.register(name="board_view", toolset="board",
                  schema=BOARD_VIEW_SCHEMA, handler=board_view_tool, emoji="📋")
registry.register(name="board_propose", toolset="board",
                  schema=BOARD_PROPOSE_SCHEMA, handler=board_propose_tool, emoji="📌")
registry.register(name="board_update", toolset="board",
                  schema=BOARD_UPDATE_SCHEMA, handler=board_update_tool, emoji="📋")
registry.register(name="counter_create", toolset="board",
                  schema=COUNTER_CREATE_SCHEMA, handler=counter_create_tool, emoji="🔢")
registry.register(name="counter_increment", toolset="board",
                  schema=COUNTER_INCREMENT_SCHEMA, handler=counter_increment_tool, emoji="🔢")
