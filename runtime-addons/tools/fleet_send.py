#!/usr/bin/env python3
"""Fleet Send Tool — send a message to another Lucaryin fleet agent via the bus.

Now with delivery confirmation: after publishing via Supabase, the tool polls
the recipient's bridge to confirm delivery (draining its pub/sub buffer with a
POST — see _drain_recipient_inbox). If the message isn't confirmed within 5
seconds, it falls back to a direct HTTP POST to the recipient's bridge
/api/bus/send, bypassing the pub/sub layer entirely.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import uuid
from tools.bridge_auth import bridge_bearer

FLEET_SEND_SCHEMA = {
    "name": "fleet_send",
    "description": (
        "Send a message to another agent in the Lucaryin fleet. "
        "Automatically confirms delivery and falls back to direct bridge "
        "routing if the pub/sub bus is unavailable. "
        "If another agent handed you the task you are working on, do not use "
        "this to reply to it: your reply in this turn goes back to that agent "
        "automatically, and a send to it is refused as a loop (status "
        "'not_delivered'). Never tell the user a message was delivered unless "
        "the status says 'delivered'. "
        "Valid recipients: thoth, neith, ptah, set."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "enum": ["neith", "ptah", "set", "thoth"],
                "description": (
                    "The agent to send to, by agent id: 'neith', 'ptah', 'set' or 'thoth'. "
                    "This field alone decides who gets the message, never a display name; "
                    "the fleet roster pairs each teammate's name with its id."
                ),
            },
            "message": {
                "type": "string",
                "description": "The message content to deliver."
            },
        },
        "required": ["recipient", "message"],
    },
}

# Well-known bridge ports for direct fallback routing
AGENT_PORTS = {
    "thoth": 9001,
    "neith": 9007,  # Hermes bridge — migrated off the OpenClaw bridge (9003)
    "ptah": 9005,
    "set": 9006,
}


def _auth_headers(extra: "dict | None" = None) -> dict:
    """Headers for bridge calls — includes the bearer token when present (P4)."""
    h = {"Content-Type": "application/json"}
    if extra:
        h.update(extra)
    token = bridge_bearer()  # file-then-env (tools/bridge_auth.py, HA3)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── WS2: Fleet delegation budget propagation ─────────────────────────────
# When this process is itself running a delegated task, the bridge worker
# exports FLEET_DELEGATION_* env vars. Attach the next-hop budget to every
# outbound fleet message so the receiving bridge seeds its worker's depth
# and refuses runaway cascades.
# The budget lives in tools/fleet_budget.py, shared with delegate_to_neith
# (R2-2-23: three copies had drifted; the bridge worker's default of 3 is the
# fleet's). This name is kept for callers and tests.
from tools.fleet_budget import next_hop_fields as _delegation_budget_fields  # noqa: E402
from tools.fleet_budget import budget_from_env as _budget_from_env  # noqa: E402
from tools.fleet_budget import refusal_reason as _refusal_reason  # noqa: E402


# Turn sources with no person in a chat to post a late result to.
_AUTONOMOUS_SOURCES = frozenset({"cron", "heartbeat", "system", "notetaker"})


def turn_origin() -> dict:
    """The chat the current turn runs in and its surface, sent with a
    delegation (fleet_send, ask_agent, delegate_to_neith) so the bridge writes
    its late result into THIS chat rather than whichever chat is newest when
    it lands, and never relays a scheduled run's into the user's chat
    (Lucaryin local-convo review F19/F31). A cron run (the runtime's own
    HERMES_CRON_SESSION marker) is "cron" and names no chat; otherwise the
    bridge worker's LUCARYIN_TURN_SESSION_ID / HERMES_TURN_SOURCE. The id
    goes to this machine's bridges only, never onto the fleet bus. Never
    raises."""
    try:
        from tools.approval_context import _is_cron_approval_context
        if _is_cron_approval_context():
            return {"session_id": "", "source": "cron"}
    except Exception:  # noqa: BLE001
        pass
    sid = (os.environ.get("LUCARYIN_TURN_SESSION_ID") or "").strip()[:80]
    src = (os.environ.get("HERMES_TURN_SOURCE") or "").strip().lower()[:32]
    return {"session_id": sid, "source": src}


def late_result_where(source: "str | None" = None) -> str:
    """Where a delegation's late result will land, said truthfully for this
    turn's surface: the bridge adds it to the asking chat as a relayed row,
    which the desktop app shows and the phone app does not; a scheduled run
    has no chat to add it to."""
    src = (turn_origin()["source"] if source is None else str(source or "")).strip().lower()
    if src in _AUTONOMOUS_SOURCES:
        return ("Its result will not be posted to the user (this is a scheduled "
                "run); it shows in Team Chat when it lands.")
    if src == "mobile":
        return ("When it lands, the result is added to this conversation, but the "
                "phone app does not show it by itself: tell the user to ask you for "
                "it (or look in the desktop app).")
    if src == "voice":
        return ("When it lands, the result is added to your newest chat with the "
                "user in the desktop app, not to this call: tell the user to ask "
                "you for it later.")
    return "Its result will be posted into this chat when it lands."


def _not_delivered(recipient: str, reason: str, guidance: str) -> str:
    """A send the recipient's loop guard would refuse — so it is NOT
    delivered, said plainly. It is not a transport failure either: nothing
    is dead-lettered and nothing should be retried."""
    return json.dumps({
        "success": False,
        "status": "not_delivered",
        "refused": True,
        "recipient": recipient,
        "method": "none",
        "error": reason,
        "message": f"NOT delivered to {recipient}: {reason}. {guidance}",
    })


def _preflight_refusal(recipient: str, sender: str):
    """Local-convo review F31 (Sep 25 04:12Z): Merlin, running a task Ptah had
    delegated to him, fleet_sent Ptah "Confirmed, I'm ready". This tool said
    "delivered"; Ptah's bridge then refused it as a loop, and Merlin told the
    owner it was confirmed. The recipient's refusal is deterministic (the same
    budget fields travel with the message), so run it here first."""
    depth, _origin, visited = _budget_from_env()
    reason = _refusal_reason(recipient, sender, depth, visited)
    if not reason:
        return None
    if visited and visited[-1] == recipient:
        guidance = (f"{recipient} handed you the task you are working on, so your "
                    f"reply in this turn goes back to {recipient} automatically "
                    f"when you finish. Do not send it; do not tell the user it was "
                    f"sent separately.")
    else:
        guidance = (f"Do the work with your own tools, and tell the user plainly "
                    f"that the message was not delivered. Do not retry it.")
    return _not_delivered(recipient, reason, guidance)


def _post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    """POST JSON payload and return parsed response dict."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=_auth_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# Statuses meaning "this bridge predates the POST drain" (no such route /
# method): only these earn the one legacy-GET retry. Anything else (401/403
# auth, 5xx) would fail the same way on a GET, so it is not retried.
_DRAIN_GET_FALLBACK_CODES = frozenset({404, 405, 501})


def _drain_recipient_inbox(recipient_port: int) -> dict:
    """Drain the recipient bridge's pub/sub buffer once; return its JSON body.

    Draining is a write, so the bridge (lucaryin-ai#85) drains on POST, which
    goes through its Origin/bearer guard; a GET drains only with a valid
    bearer, and a token-less bridge answers the GET with 405. An older bridge
    on the same box (dev machines) has no POST drain yet, so on 404/405/501 —
    and only then — retry once with the legacy bearer GET. Every other error
    propagates to the caller's poll loop, exactly as before.

    The route literal and both requests stay in this one function: the
    bridge's test_pubsub_drain_runtime_contract.py locates drain callers by
    the function that names the route and needs a literal method on each.
    """
    url = f"http://127.0.0.1:{recipient_port}/api/pubsub/messages"
    req = urllib.request.Request(url, data=b"", headers=_auth_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code not in _DRAIN_GET_FALLBACK_CODES:
            raise
    legacy = urllib.request.Request(url, headers=_auth_headers(), method="GET")
    with urllib.request.urlopen(legacy, timeout=3) as resp:
        return json.loads(resp.read().decode())


def _check_recipient_inbox(recipient: str, task_id: str, timeout: int = 5) -> bool:
    """Poll recipient's bridge to confirm our message arrived."""
    recipient_port = AGENT_PORTS.get(recipient)
    if not recipient_port:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = _drain_recipient_inbox(recipient_port)
            messages = data.get("messages", [])
            for msg in messages:
                if msg.get("task_id") == task_id:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# Dead-letter log dir — every undeliverable message is recorded here so a
# failed send is never silently dropped (P6 ACK reliability).
DEAD_LETTER_DIR = os.path.expanduser("~/.lucaryin/fleet-dead-letters")


def _write_dead_letter(recipient: str, message: str, sender: str, reason: str) -> str:
    """Append an undeliverable message to today's dead-letter log.

    Returns the file path on success, or "" if the write itself failed.
    """
    try:
        os.makedirs(DEAD_LETTER_DIR, exist_ok=True)
        path = os.path.join(DEAD_LETTER_DIR, f"{time.strftime('%Y-%m-%d')}.md")
        entry = (
            f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} — {sender} → {recipient} (DEAD)\n"
            f"- reason: {reason}\n"
            f"- message: {message[:500]}\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        return path
    except Exception:
        return ""


def fleet_send_tool(args, **kw):
    """Handle fleet_send tool calls with delivery confirmation and fallback."""
    recipient = (args.get("recipient", "") or "").strip().lower()
    message = (args.get("message", "") or "").strip()

    if not recipient or not message:
        from tools.registry import tool_error
        return tool_error("Both 'recipient' and 'message' are required.")

    valid = {"thoth", "neith", "ptah", "set"}
    if recipient not in valid:
        from tools.registry import tool_error
        return tool_error(
            f"Unknown agent: '{recipient}'. 'recipient' is an agent id: "
            f"{', '.join(sorted(valid))}. The fleet roster pairs each teammate's "
            "name with its id; a name is not an id."
        )

    # Don't send to self
    sender = os.environ.get("BRIDGE_PROFILE", "thoth")
    if recipient == sender:
        from tools.registry import tool_error
        return tool_error(f"Cannot send to yourself ({sender}).")

    refused = _preflight_refusal(recipient, sender)
    if refused:
        return refused

    port = os.environ.get("HERMES_SERVER_PORT", "9001")
    bus_url = f"http://127.0.0.1:{port}/api/bus/send"

    # WS2: mint the task_id CLIENT-side so the pub/sub publish and the
    # direct-bridge fallback carry the same id — the receiving bridge dedups
    # on task_id, so double delivery collapses to one execution.
    payload = {
        "recipient": recipient,
        "message": message,
        "sender": sender,
        "task_id": str(uuid.uuid4()),
    }
    payload.update(_delegation_budget_fields(sender))
    # The chat this turn runs in, for this bridge's record of the delegation
    # (its late result lands there). Not part of what is published.
    _origin = turn_origin()
    if _origin["session_id"]:
        payload["origin_session_id"] = _origin["session_id"]
    if _origin["source"]:
        payload["origin_source"] = _origin["source"]

    # Receipt is tri-state: "delivered" (confirmed), "queued" (published but
    # unconfirmed — recipient may still pick it up), or "dead" (every transport
    # failed → logged to a dead-letter and surfaced to the user).
    # ── Phase 1: Try pub/sub via our own bridge ──────────────────
    try:
        result = _post_json(bus_url, payload, timeout=10)
        if result.get("success"):
            task_id = result.get("task_id", "")

            # Phase 2: Confirm delivery on recipient's bridge
            if task_id and _check_recipient_inbox(recipient, task_id, timeout=5):
                return json.dumps({
                    "success": True,
                    "status": "delivered",
                    "recipient": recipient,
                    "method": "pubsub",
                    "message": f"Message delivered to {recipient} (confirmed).",
                })

            # Phase 3: Not confirmed — fall back to direct bridge-to-bridge
            recipient_port = AGENT_PORTS.get(recipient)
            if recipient_port:
                try:
                    direct_url = f"http://127.0.0.1:{recipient_port}/api/bus/send"
                    direct_result = _post_json(direct_url, payload, timeout=10)
                    if direct_result.get("success"):
                        return json.dumps({
                            "success": True,
                            "status": "delivered",
                            "recipient": recipient,
                            "method": "direct",
                            "message": f"Message delivered to {recipient} (direct bridge fallback).",
                        })
                except Exception:
                    pass  # fall through to "queued"

            # Phase 4: Published but delivery unconfirmed — queued, not dead.
            return json.dumps({
                "success": True,
                "status": "queued",
                "recipient": recipient,
                "method": "pubsub_unconfirmed",
                "message": (
                    f"Message published to {recipient} but delivery could not be "
                    "confirmed (recipient may be offline or between sessions). It "
                    "is queued and may still be picked up — do NOT resend it; "
                    "tell the user delivery is unconfirmed."
                ),
            })

        # The bridge ran the recipient's loop guard and refused it (F31).
        if result.get("refused") or result.get("status") == "not_delivered":
            return _not_delivered(
                recipient, str(result.get("error") or "the recipient would refuse it"),
                str(result.get("guidance") or "Do not retry it; tell the user it was not delivered."))
        # Bus accepted the request but reported failure → dead.
        reason = result.get("error", "bus returned success=false")
    except urllib.error.URLError as e:
        reason = f"bus endpoint unreachable: {getattr(e, 'reason', e)}"
        # Last resort: try the recipient's bridge directly before giving up.
        recipient_port = AGENT_PORTS.get(recipient)
        if recipient_port:
            try:
                direct_url = f"http://127.0.0.1:{recipient_port}/api/bus/send"
                direct_result = _post_json(direct_url, payload, timeout=10)
                if direct_result.get("success"):
                    return json.dumps({
                        "success": True,
                        "status": "delivered",
                        "recipient": recipient,
                        "method": "direct",
                        "message": f"Message delivered to {recipient} (direct bridge; local bus was unreachable).",
                    })
            except Exception:
                pass
    except Exception as e:
        reason = str(e)

    # ── Dead: every transport failed. Never silently drop — log + surface. ──
    dl_path = _write_dead_letter(recipient, message, sender, reason)
    return json.dumps({
        "success": False,
        "status": "dead",
        "recipient": recipient,
        "method": "none",
        "error": reason,
        "dead_letter": dl_path,
        "message": (
            f"Delivery to {recipient} FAILED ({reason}). Logged to the dead-letter "
            "file. Do NOT silently drop this — tell the user what you tried, to "
            "whom, and that it failed, and offer to retry."
        ),
    })


# --- Registry ---
from tools.registry import registry

registry.register(
    name="fleet_send",
    toolset="fleet",
    schema=FLEET_SEND_SCHEMA,
    handler=fleet_send_tool,
    emoji="📡",
)
