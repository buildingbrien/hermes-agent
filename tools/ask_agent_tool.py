#!/usr/bin/env python3
"""ask_agent — ask any teammate a question and get their ANSWER back.

The fleet had two halves of this and neither was the whole thing:

  fleet_send        — works for any agent, but is fire-and-forget. It returns
                      a delivery receipt, not a reply.
  delegate_to_neith — returns the reply synchronously, but is hardcoded to
                      Neith (port 9007).

So an agent that needed to ASK a specific teammate something and use the
answer had no supported path. On 2026-08-16 Ptah tried to run a 25-image
focus group with Clara and Set as reviewers and spent most of its turn
hand-rolling the missing primitive: curling bridge endpoints to find a read
path, polluting another agent's bus with shell text, then scraping replies
out of session files on disk. It ran out of time before finishing the work
it was actually asked to do.

The plumbing was already there — /api/chat/sync, the delegation budget, the
hop-depth refusals — welded to one recipient. This exposes it for the fleet.

Multi-round conversations: each call is a fresh session on the far side, so
the teammate does NOT remember previous exchanges. Carry any context the
answer depends on in the question itself.
"""

import json
import os
import urllib.error
import urllib.request

# Same map the bridges and voice server use. Keep in sync.
AGENT_PORTS = {"thoth": 9001, "ptah": 9005, "set": 9006, "neith": 9007}

# Long enough for a real tool-bearing turn on the far side (research, file
# reads, vision), short enough that a wedged teammate cannot hang the caller
# for the whole turn budget.
_SYNC_TIMEOUT = 300.0


def _budget_fields(sender: str) -> dict:
    """Propagate the delegation budget so the far side can refuse onward hops
    and the A→B→A ping-pong guard keeps working."""
    try:
        from tools.fleet_send import _delegation_budget_fields
        return _delegation_budget_fields(sender) or {}
    except Exception:
        return {}


def ask_agent(agent: str, question: str, sender: str = "") -> str:
    target = (agent or "").strip().lower()
    q = (question or "").strip()
    if target not in AGENT_PORTS:
        return json.dumps({
            "success": False,
            "error": f"Unknown agent '{agent}'. Valid: {', '.join(sorted(AGENT_PORTS))}.",
        })
    if not q:
        return json.dumps({"success": False, "error": "question is required"})
    if target == (sender or "").strip().lower():
        return json.dumps({
            "success": False,
            "error": "That is you — answer it yourself rather than asking.",
        })

    payload = {"messages": [{"role": "user", "content": q}], "agent_id": target}
    payload.update(_budget_fields(sender))
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"http://127.0.0.1:{AGENT_PORTS[target]}/api/chat/sync"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as e:
        return json.dumps({
            "success": False, "agent": target,
            "error": f"{target}'s bridge is unreachable ({e}). Say so plainly "
                     f"rather than inventing their answer.",
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "agent": target, "error": str(e)})

    # A refusal is a real answer — surface it rather than burying it as failure.
    if data.get("refused"):
        return json.dumps({"success": False, "agent": target, "refused": True,
                           "error": data.get("error", "refused")})

    reply = (data.get("response") or data.get("content")
             or data.get("message") or "").strip()
    if not reply:
        return json.dumps({
            "success": False, "agent": target,
            "error": f"{target} returned nothing. Do NOT guess what they would "
                     f"have said — report that they did not answer.",
        })
    return json.dumps({"success": True, "agent": target, "answer": reply})


def check_ask_agent_requirements() -> dict:
    return {"available": True}


ASK_AGENT_SCHEMA = {
    "name": "ask_agent",
    "description": (
        "Ask a specific teammate a question and get their ANSWER back as the "
        "tool result. Use this whenever you need another agent's actual "
        "response — a review, an opinion, an analysis, a check — rather than "
        "just notifying them. This is the tool for running anything "
        "conversational across the fleet: panels, reviews, second opinions, "
        "multi-agent exercises. (fleet_send only DELIVERS a message and "
        "returns a receipt; it cannot bring a reply back.) "
        "The teammate does not see your conversation and does not remember "
        "earlier calls, so make each question self-contained — restate any "
        "role, persona or context the answer depends on. "
        "Valid agents: thoth, neith, ptah, set."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent": {"type": "string",
                      "description": "Teammate to ask: thoth, neith, ptah, or set."},
            "question": {"type": "string",
                         "description": "The self-contained question or task."},
        },
        "required": ["agent", "question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error  # noqa: E402

registry.register(
    name="ask_agent",
    toolset="fleet",
    schema=ASK_AGENT_SCHEMA,
    handler=lambda args, **kw: ask_agent(
        agent=args.get("agent") or "",
        question=args.get("question") or "",
        sender=(kw.get("agent_id") or kw.get("profile") or "")),
    check_fn=check_ask_agent_requirements,
    emoji="💬",
)
