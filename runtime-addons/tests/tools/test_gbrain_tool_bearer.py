"""gbrain_search / gbrain_read sign their :9050 calls with the bridge bearer
(addon runtime-addons/tools/gbrain_tool.py).

lucaryin-ai B17 (#109, bug hunt round 2 R2-1-23) closed the gbrain memory
bridge on 127.0.0.1:9050: every route but GET /health now needs
``Authorization: Bearer <bridge token>``, because any website could read and
overwrite the memory brain through it. This addon's _bridge_get sent no
Authorization, so behind that bridge every gbrain_search / gbrain_read call got
a 401 and answered "gbrain is not reachable", while check_gbrain_requirements
(which probes the open /health) kept both tools advertised. It now signs
through tools/bridge_auth: file first, env second (HA3), like every other
addon that calls a local bridge.

Bare tier: a real loopback HTTP server stands in for the gbrain bridge and
answers like B17's: 401 without the right bearer on every route but /health.
The request is asserted as it hits the wire.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools import bridge_auth
from tools import gbrain_tool

TOKEN = "gbrain-bridge-token"
PAGE = "# Acme\nThe founder signed the Acme contract on Sep 3."


class FakeGbrainBridge:
    """Records the Authorization of every request; enforces the bearer like B17."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, "str | None"]] = []
        self.port = 0


def _handler_for(bridge: FakeGbrainBridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # keep pytest output clean
            pass

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            auth = self.headers.get("Authorization")
            bridge.seen.append((parsed.path, auth))
            if parsed.path == "/health":
                self._reply(200, {"status": "ok", "pages": 1})
                return
            if auth != f"Bearer {TOKEN}":
                self._reply(401, {"error": "Unauthorized"})
                return
            if parsed.path == "/api/gbrain/search":
                q = params.get("q", [""])[0]
                self._reply(200, {"query": q,
                                  "results": "[0.91] companies/acme -- signed the Acme contract"})
                return
            if parsed.path == "/api/gbrain/page":
                slug = params.get("slug", [""])[0]
                if slug == "companies/acme":
                    self._reply(200, {"slug": slug, "content": PAGE})
                else:
                    self._reply(404, {"error": "not found"})
                return
            self._reply(404, {"error": "not found"})

    return Handler


@pytest.fixture
def bridge(monkeypatch):
    fake = FakeGbrainBridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    monkeypatch.setattr(gbrain_tool, "GBRAIN_BRIDGE", f"http://127.0.0.1:{fake.port}")
    monkeypatch.delenv(bridge_auth.BEARER_ENV, raising=False)
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _publish_bearer(token: str = TOKEN) -> None:
    """Write the bearer where hermes-bridge publishes it. conftest.py points
    LUCARYIN_AUTH_DIR at a fresh tmp dir for every addon test."""
    path = Path(bridge_auth.bearer_file_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def _auth_for(bridge: FakeGbrainBridge, route: str) -> list:
    return [auth for path, auth in bridge.seen if path == route]


# ── the fix: both verbs reach the brain through a bearer-guarded bridge ─────

def test_search_signs_with_the_published_bearer(bridge):
    _publish_bearer()
    out = json.loads(gbrain_tool.gbrain_search("acme contract"))
    assert "error" not in out, out
    assert out["results"][0]["slug"] == "companies/acme"
    assert _auth_for(bridge, "/api/gbrain/search") == [f"Bearer {TOKEN}"]


def test_read_signs_with_the_published_bearer(bridge):
    _publish_bearer()
    out = json.loads(gbrain_tool.gbrain_read("companies/acme"))
    assert "error" not in out, out
    assert out["compiled_truth"] == PAGE
    assert _auth_for(bridge, "/api/gbrain/page") == [f"Bearer {TOKEN}"]


def test_env_bearer_when_no_file_is_published(bridge, monkeypatch):
    # The in-process worker still carries BRIDGE_AUTH_TOKEN (B1 keeps it there).
    monkeypatch.setenv(bridge_auth.BEARER_ENV, TOKEN)
    assert not Path(bridge_auth.bearer_file_path()).exists()
    assert json.loads(gbrain_tool.gbrain_search("acme"))["results"]
    assert "compiled_truth" in json.loads(gbrain_tool.gbrain_read("companies/acme"))


def test_the_published_file_wins_over_a_stale_env(bridge, monkeypatch):
    _publish_bearer()
    monkeypatch.setenv(bridge_auth.BEARER_ENV, "stale-token")
    assert json.loads(gbrain_tool.gbrain_search("acme"))["results"]
    assert _auth_for(bridge, "/api/gbrain/search") == [f"Bearer {TOKEN}"]


# ── degradation stays honest ────────────────────────────────────────────────

def test_an_unknown_slug_is_still_no_page(bridge):
    _publish_bearer()
    out = json.loads(gbrain_tool.gbrain_read("companies/nobody"))
    assert out["error"] == "no compiled page at slug 'companies/nobody'"


def test_a_refused_bearer_reads_as_recall_unavailable_not_no_page(bridge):
    # No bearer anywhere (or a stale one): the bridge is UP and says 401. That
    # is recall unavailable, never "no compiled page" (which tells the model
    # the brain holds nothing about the topic).
    read = json.loads(gbrain_tool.gbrain_read("companies/acme"))
    search = json.loads(gbrain_tool.gbrain_search("acme"))
    for out in (read, search):
        assert out["recall_available"] is False
        assert "refused" in out["error"]
    assert _auth_for(bridge, "/api/gbrain/page") == [None]
    _publish_bearer("stale-token")
    assert json.loads(gbrain_tool.gbrain_read("companies/acme"))["recall_available"] is False


def test_the_health_probe_needs_no_bearer(bridge):
    assert gbrain_tool.check_gbrain_requirements() == {"available": True}


# ── the contract that would have caught this ────────────────────────────────

def _addon_sources() -> "Path | None":
    """runtime-addons/ from a source-path run or from build/runtime (CI)."""
    here = Path(__file__).resolve()
    # runtime-addons/tests/tools/<this>, or build/runtime/tests/tools/<this>
    # with runtime-addons/ two levels above build/runtime.
    for root in (here.parents[2], here.parents[2].parent.parent / "runtime-addons"):
        if root.name == "runtime-addons" and (root / "tools" / "gbrain_tool.py").is_file():
            return root
    return None


def test_every_addon_that_calls_a_local_bridge_signs():
    """An addon that names a loopback address talks to a Lucaryin bridge, and
    every bridge now needs the bearer: it must get it from tools/bridge_auth."""
    addons = _addon_sources()
    if addons is None:
        pytest.skip("runtime-addons/ sources not next to this test")
    offenders = []
    checked = 0
    for path in sorted(list((addons / "tools").glob("*.py")) + list((addons / "cron").glob("*.py"))):
        if path.name == "bridge_auth.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"127\.0\.0\.1|localhost", text):
            continue
        checked += 1
        if "tools.bridge_auth" not in text:
            offenders.append(path.name)
    assert checked >= 5, "loopback-calling addons not found; is the scan pointed at runtime-addons/?"
    assert offenders == [], offenders
