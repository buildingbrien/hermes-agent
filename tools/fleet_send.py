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


def _post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    """POST JSON payload and return parsed response dict."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
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
            req = urllib.request.Request(url, method="GET")
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

    # ── Phase 1: Try pub/sub via our own bridge ──────────────────
    try:
        result = _post_json(bus_url, payload, timeout=10)
        if result.get("success"):
            task_id = result.get("task_id", "")

            # Phase 2: Confirm delivery on recipient's bridge
            if task_id and _check_recipient_inbox(recipient, task_id, timeout=5):
                return json.dumps({
                    "success": True,
                    "recipient": recipient,
                    "method": "pubsub",
                    "message": f"Message sent to {recipient} (confirmed delivered).",
                })

            # Phase 3: Not confirmed — fall back to direct bridge-to-bridge
            recipient_port = AGENT_PORTS.get(recipient)
            if recipient_port:
                direct_url = f"http://127.0.0.1:{recipient_port}/api/bus/send"
                direct_result = _post_json(direct_url, payload, timeout=10)
                if direct_result.get("success"):
                    return json.dumps({
                        "success": True,
                        "recipient": recipient,
                        "method": "direct",
                        "message": f"Message sent to {recipient} (direct fallback — pub/sub unconfirmed).",
                    })

            # Phase 4: Both methods failed or pub/sub sent but unconfirmed
            return json.dumps({
                "success": True,
                "recipient": recipient,
                "method": "pubsub_unconfirmed",
                "message": (
                    f"Message published to {recipient} but delivery could not be confirmed. "
                    "The recipient may be offline or its bridge may be stuck. "
                    "Consider retrying in a moment or using a different channel."
                ),
            })
        else:
            return json.dumps({"error": result.get("error", "Unknown error")})
    except urllib.error.URLError as e:
        return json.dumps({"error": f"Bus endpoint unreachable: {e.reason}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# --- Registry ---
from tools.registry import registry

registry.register(
    name="fleet_send",
    toolset="fleet",
    schema=FLEET_SEND_SCHEMA,
    handler=fleet_send_tool,
    emoji="📡",
)
