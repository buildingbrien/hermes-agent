#!/usr/bin/env python3
"""Fleet Send Tool — send a message to another Lucaryin fleet agent via the bus.

Now with delivery confirmation: after publishing via Supabase, the tool polls
the recipient's bridge to confirm delivery. If the message isn't confirmed
within 5 seconds, it falls back to a direct HTTP POST to the recipient's
bridge /api/bus/send, bypassing the pub/sub layer entirely.
"""

import json
import os
import time
import urllib.request
import urllib.error

FLEET_SEND_SCHEMA = {
    "name": "fleet_send",
    "description": (
        "Send a message to another agent in the Lucaryin fleet. "
        "Automatically confirms delivery and falls back to direct bridge "
        "routing if the pub/sub bus is unavailable. "
        "Valid recipients: thoth, neith, ptah, set."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "The agent to send to: 'neith', 'ptah', 'set', or 'thoth'."
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


def _auth_headers(extra: dict | None = None) -> dict:
    """Headers for bridge calls — includes the bearer token when present (P4)."""
    h = {"Content-Type": "application/json"}
    if extra:
        h.update(extra)
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


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


def _check_recipient_inbox(recipient: str, task_id: str, timeout: int = 5) -> bool:
    """Poll recipient's bridge to confirm our message arrived."""
    recipient_port = AGENT_PORTS.get(recipient)
    if not recipient_port:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = f"http://127.0.0.1:{recipient_port}/api/pubsub/messages"
            req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
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
            f"Unknown agent: '{recipient}'. Valid: {', '.join(sorted(valid))}"
        )

    # Don't send to self
    sender = os.environ.get("BRIDGE_PROFILE", "thoth")
    if recipient == sender:
        from tools.registry import tool_error
        return tool_error(f"Cannot send to yourself ({sender}).")

    port = os.environ.get("HERMES_SERVER_PORT", "9001")
    bus_url = f"http://127.0.0.1:{port}/api/bus/send"

    payload = {
        "recipient": recipient,
        "message": message,
        "sender": sender,
    }

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
                    "is queued — consider retrying, or tell the user it is unconfirmed."
                ),
            })

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
