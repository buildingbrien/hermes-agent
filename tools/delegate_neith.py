#!/usr/bin/env python3
"""delegate_to_neith — delegate a research/analysis task to the persistent Neith
agent (Hermes bridge, port 9007) and return her findings synchronously.

Primary path: POST the task to Neith's /api/chat/sync and return Neith's answer.
This is bus-independent — it does not rely on Supabase Realtime / pub/sub, so it
works even when the fleet bus is down.

Fallback: if Neith's bridge is unreachable or returns no usable result, spawn an
in-process research subagent via delegate_task(toolsets=['web']) so research is
never silently dropped. The result is tagged so the caller (and user) can see
which path produced it.
"""

import json
import os
import urllib.request
import urllib.error

# Neith runs on the Hermes bridge (profile neith), port 9007 — migrated off the
# deprecated OpenClaw bridge (9003).
NEITH_PORT = 9007
NEITH_SYNC_URL = f"http://127.0.0.1:{NEITH_PORT}/api/chat/sync"
# Neith's /api/chat/sync worker caps at 300s; give the HTTP call slight headroom
# so we receive the bridge's own 504 rather than a client-side timeout.
_SYNC_TIMEOUT = 320


DELEGATE_TO_NEITH_SCHEMA = {
    "name": "delegate_to_neith",
    "description": (
        "Delegate a research, web-search, data-gathering, or deep-analysis task "
        "to Neith — the fleet's dedicated research agent — and get her findings "
        "back as the tool result, which you then relay to the user. Use this "
        "whenever the user needs live web research or information beyond your "
        "knowledge cutoff. Neith does NOT see this conversation, so make the task "
        "specific and self-contained. If Neith's bridge is unavailable, the task "
        "is automatically handled by a research subagent instead, so it never "
        "silently fails."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The research/analysis task for Neith. Be specific and "
                    "self-contained — include any context she needs, since she "
                    "cannot see your conversation history."
                ),
            },
        },
        "required": ["task"],
    },
}


def _call_neith_sync(task: str) -> dict:
    """POST the task to Neith's /api/chat/sync and return the parsed response.

    Raises urllib.error.URLError (transport) or other exceptions on failure.
    """
    payload = {
        "messages": [{"role": "user", "content": task}],
        "agent_id": "neith",
    }
    headers = {"Content-Type": "application/json"}
    # Bridge auth (P4): attach the per-launch token when present so this keeps
    # working once the bridges require authentication. Harmless when unset.
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        NEITH_SYNC_URL,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fallback_subagent(task: str, parent_agent) -> str | None:
    """Run an in-process research subagent when Neith's bridge is unreachable.

    Returns the subagent's summary, or None if the fallback itself fails.
    """
    if parent_agent is None:
        return None
    try:
        from tools.delegate_tool import delegate_task
        result_json = delegate_task(
            goal=task,
            toolsets=["web"],
            parent_agent=parent_agent,
        )
        data = json.loads(result_json)
        results = data.get("results", [])
        if results and isinstance(results, list):
            summary = results[0].get("summary")
            if summary:
                return summary
    except Exception:
        return None
    return None


def delegate_to_neith_tool(args, **kw):
    from tools.registry import tool_error

    task = (args.get("task", "") or "").strip()
    if not task:
        return tool_error("A 'task' for Neith is required.")

    parent_agent = kw.get("parent_agent")

    # ── Primary: synchronous call to the persistent Neith on :9007 ──
    try:
        resp = _call_neith_sync(task)
        if resp.get("success") and resp.get("response"):
            return json.dumps(
                {"source": "neith", "result": resp["response"]},
                ensure_ascii=False,
            )
        # Bridge reachable but produced no usable answer — try the fallback.
        neith_err = resp.get("error", "Neith returned no result")
    except urllib.error.URLError as e:
        neith_err = f"Neith bridge unreachable: {getattr(e, 'reason', e)}"
    except Exception as e:
        neith_err = f"Neith call failed: {e}"

    # ── Fallback: in-process research subagent ──
    summary = _fallback_subagent(task, parent_agent)
    if summary:
        return json.dumps(
            {
                "source": "research_subagent_fallback",
                "note": (
                    f"Neith was unavailable ({neith_err}); a research subagent "
                    "handled this instead."
                ),
                "result": summary,
            },
            ensure_ascii=False,
        )

    return tool_error(
        f"Could not reach Neith ({neith_err}) and the research-subagent fallback "
        "also failed. Tell the user the research could not be completed, and why."
    )


# --- Registry ---
from tools.registry import registry

registry.register(
    name="delegate_to_neith",
    toolset="fleet",
    schema=DELEGATE_TO_NEITH_SCHEMA,
    handler=lambda args, **kw: delegate_to_neith_tool(args, **kw),
    emoji="🔬",
)
