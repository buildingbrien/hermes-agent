"""fleet_send's delivery confirmation drains the recipient's pub/sub buffer
with a POST (addon runtime-addons/tools/fleet_send.py).

The Lucaryin bridge (lucaryin-ai#85) moved the /api/pubsub/messages drain to
POST: draining is a write, and a GET is what a cross-site <img>/no-cors fetch
sends with no Origin. A GET still drains only with a valid bearer, and a
token-less bridge answers it with 405 — so a GET-only confirmation poll sat out
its whole 5 s window on such a box and then double-published via the direct
fallback. The drain now POSTs (empty JSON body, same bearer), and retries ONCE
with the legacy bearer GET only when the POST says the route/method does not
exist (404/405/501) — an older bridge on a dev box.

Bare tier: a real loopback HTTP server stands in for the recipient's bridge
(and, end to end, for our own bus), so the real urllib request path is what is
asserted — method, headers and body as they hit the wire.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools import fleet_send

ROUTE = "/api/pubsub/messages"
TOKEN = "test-bridge-token"


@dataclass
class Seen:
    method: str
    path: str
    authorization: "str | None"
    content_type: "str | None"
    content_length: "str | None"
    body: bytes


class FakeBridge:
    """Records every request; serves a drain buffer like the real bridge."""

    def __init__(self) -> None:
        self.requests: list[Seen] = []
        self.buffer: list[dict] = []
        self.post_status = 200  # status for POST /api/pubsub/messages
        self.get_status = 200   # status for GET  /api/pubsub/messages
        self.port = 0

    def methods(self, path: str = ROUTE) -> list[str]:
        return [r.method for r in self.requests if r.path == path]

    def drain(self) -> dict:
        msgs, self.buffer = self.buffer, []
        return {"messages": msgs, "count": len(msgs)}


def _handler_for(bridge: FakeBridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # keep pytest output clean
            pass

        def _record(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            bridge.requests.append(Seen(
                method=self.command,
                path=self.path,
                authorization=self.headers.get("Authorization"),
                content_type=self.headers.get("Content-Type"),
                content_length=self.headers.get("Content-Length"),
                body=body,
            ))
            return body

        def _reply(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _drain_reply(self, status: int) -> None:
            if status == 200:
                self._reply(200, bridge.drain())
            else:
                self._reply(status, {"error": f"status {status}"})

        def do_POST(self) -> None:
            body = self._record()
            if self.path == "/api/bus/send":
                # Our own bridge's bus: "publish" straight into the recipient
                # buffer, echoing the client-minted task_id like the real one.
                payload = json.loads(body)
                bridge.buffer.append({"task_id": payload["task_id"],
                                      "message": payload["message"]})
                self._reply(200, {"success": True, "task_id": payload["task_id"]})
            elif self.path == ROUTE:
                self._drain_reply(bridge.post_status)
            else:
                self._reply(404, {"error": "not found"})

        def do_GET(self) -> None:
            self._record()
            if self.path == ROUTE:
                self._drain_reply(bridge.get_status)
            else:
                self._reply(404, {"error": "not found"})

    return Handler


@pytest.fixture
def bridge(monkeypatch):
    b = FakeBridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(b))
    b.port = server.server_address[1]
    # Short poll interval: shutdown() waits out one poll per test.
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    # Loopback only: never let a developer's proxy env route these requests.
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setitem(fleet_send.AGENT_PORTS, "neith", b.port)
    monkeypatch.setenv("BRIDGE_AUTH_TOKEN", TOKEN)
    try:
        yield b
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ── the drain request itself ──────────────────────────────────────────────


def test_drain_posts_an_empty_json_body_with_the_bearer(bridge):
    bridge.buffer = [{"task_id": "t-1"}]

    data = fleet_send._drain_recipient_inbox(bridge.port)

    assert data["messages"] == [{"task_id": "t-1"}]
    assert len(bridge.requests) == 1
    req = bridge.requests[0]
    assert (req.method, req.path) == ("POST", ROUTE)
    assert req.authorization == f"Bearer {TOKEN}"
    assert req.content_type == "application/json"
    assert req.body == b""
    assert req.content_length == "0"


def test_drain_without_a_token_still_posts_and_sends_no_bearer(bridge, monkeypatch):
    monkeypatch.delenv("BRIDGE_AUTH_TOKEN", raising=False)

    fleet_send._drain_recipient_inbox(bridge.port)

    assert bridge.methods() == ["POST"]
    assert bridge.requests[0].authorization is None


@pytest.mark.parametrize("status", [404, 405, 501])
def test_older_bridge_without_post_drain_falls_back_to_one_bearer_get(bridge, status):
    bridge.post_status = status
    bridge.buffer = [{"task_id": "t-legacy"}]

    data = fleet_send._drain_recipient_inbox(bridge.port)

    assert data["messages"] == [{"task_id": "t-legacy"}]
    assert bridge.methods() == ["POST", "GET"]
    get = bridge.requests[1]
    assert get.authorization == f"Bearer {TOKEN}"
    assert get.body == b""


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 502, 503])
def test_other_post_failures_never_fall_back_to_get(bridge, status):
    """Auth failures and server errors would fail the same way on a GET —
    and a GET is the weaker request — so they surface to the poll loop."""
    bridge.post_status = status
    bridge.buffer = [{"task_id": "t-1"}]

    with pytest.raises(urllib.error.HTTPError) as exc:
        fleet_send._drain_recipient_inbox(bridge.port)

    assert exc.value.code == status
    assert bridge.methods() == ["POST"]
    assert bridge.buffer == [{"task_id": "t-1"}]  # nothing drained


def test_fallback_is_a_single_retry(bridge):
    bridge.post_status = 404
    bridge.get_status = 404

    with pytest.raises(urllib.error.HTTPError) as exc:
        fleet_send._drain_recipient_inbox(bridge.port)

    assert exc.value.code == 404
    assert bridge.methods() == ["POST", "GET"]


# ── confirmation poll and the tool end to end ─────────────────────────────


def test_inbox_check_confirms_through_the_post_drain(bridge):
    bridge.buffer = [{"task_id": "other"}, {"task_id": "mine"}]

    assert fleet_send._check_recipient_inbox("neith", "mine", timeout=2) is True
    assert bridge.methods() == ["POST"]


def test_fleet_send_confirms_delivery_without_any_get(bridge, monkeypatch):
    # Our own bus and the recipient's bridge are both the fake server.
    monkeypatch.setenv("HERMES_SERVER_PORT", str(bridge.port))
    monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
    for var in ("FLEET_DELEGATION_DEPTH", "FLEET_DELEGATION_ORIGIN",
                "FLEET_DELEGATION_VISITED"):
        monkeypatch.delenv(var, raising=False)

    result = json.loads(fleet_send.fleet_send_tool(
        {"recipient": "neith", "message": "status check"}))

    assert result["status"] == "delivered", result
    assert result["method"] == "pubsub"
    assert [(r.method, r.path) for r in bridge.requests] == [
        ("POST", "/api/bus/send"),
        ("POST", ROUTE),
    ]
    assert all(r.authorization == f"Bearer {TOKEN}" for r in bridge.requests)
