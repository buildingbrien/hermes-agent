"""tools/bridge_auth (HA3): addon bearer reads go file first, then env — the
runtime half of the B1 pair. Either half may ship first: with no file the env
still works; with the env stripped the file still works.

The file is the one hermes-bridge/server.py publishes (B1, merged on lucaryin-ai
prod-hardening/base): ``_bridge_bearer_file_path()`` below is a VERBATIM copy of
the bridge function, and ``test_reader_path_is_the_bridge_writer_path`` holds the
reader to it under every env shape. The builder's first cut read
``$LUCARYIN_AUTH_DIR/bridge-bearer`` — a file nobody writes — and only kept
working because the env fallback hid it (review, 2026-09-23). When a lucaryin-ai
checkout sits next to this repo (or ``LUCARYIN_AI_DIR`` names one),
``test_pinned_copy_matches_the_live_bridge`` also proves the copy has not drifted.
"""

import ast
import os
import re
import textwrap
from pathlib import Path

import pytest

from tools import bridge_auth
from tools import fleet_send

# The addon SOURCES: tests run from build/runtime (CI: ``working-directory:
# build/runtime``), whose parent's parent holds runtime-addons/. The fold patches
# (upstream files) have their own tests; browser_tool.py (0025) is checked below.
_RUNTIME = Path(__file__).resolve().parents[2]
ADDONS = _RUNTIME.parents[1] / "runtime-addons"
ADDON_FILES = ("dial_meeting.py", "fleet_send.py", "delegate_neith.py", "board_tool.py", "meeting_notes.py",
               "ask_agent_tool.py", "gbrain_tool.py")

# hermes-bridge/server.py @ lucaryin-ai 1ef2e65, copied verbatim (docstring included).
BRIDGE_WRITER_SOURCE = textwrap.dedent('''
    def _bridge_bearer_file_path() -> str:
        """Where the bridge publishes its bearer for readers that no longer see it
        in their environment: LUCARYIN_AUTH_DIR/bridge.token (the directory the
        connector tokens already live in, default ~/.lucaryin/auth)."""
        d = os.environ.get("LUCARYIN_AUTH_DIR") or os.path.join(
            os.path.expanduser("~"), ".lucaryin", "auth")
        return os.path.join(d, "bridge.token")
''')


def _bridge_writer_path() -> str:
    ns = {"os": os}
    exec(compile(BRIDGE_WRITER_SOURCE, "server.py", "exec"), ns)
    return ns["_bridge_bearer_file_path"]()


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for name in (bridge_auth.BEARER_ENV, bridge_auth.AUTH_DIR_ENV, "LUCARYIN_BRIDGE_BEARER_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _bearer_file(root: Path) -> Path:
    f = root / ".lucaryin" / "auth" / "bridge.token"
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


# ── the pair contract ────────────────────────────────────────────────────────

def test_the_file_name_is_the_bridges():
    assert bridge_auth.BEARER_FILE_NAME == "bridge.token"


@pytest.mark.parametrize("auth_dir", [None, "", "{tmp}/alt", "~/elsewhere"])
def test_reader_path_is_the_bridge_writer_path(clean, monkeypatch, auth_dir):
    """Same directory rule as the writer, including the edge cases: unset and
    empty both mean ~/.lucaryin/auth, and a value is used verbatim."""
    if auth_dir is not None:
        monkeypatch.setenv(bridge_auth.AUTH_DIR_ENV, auth_dir.format(tmp=clean))
    assert bridge_auth.bearer_file_path() == _bridge_writer_path()


def test_default_file_lives_under_the_auth_dir(clean):
    assert bridge_auth.bearer_file_path() == str(clean / ".lucaryin" / "auth" / "bridge.token")


def test_no_private_override_env(clean, monkeypatch):
    """A reader-only override (the old LUCARYIN_BRIDGE_BEARER_FILE) points at a
    file the bridge never writes; it must not exist."""
    (clean / "x").write_text("override-token")
    monkeypatch.setenv("LUCARYIN_BRIDGE_BEARER_FILE", str(clean / "x"))
    monkeypatch.setenv(bridge_auth.BEARER_ENV, "env-token")
    assert bridge_auth.bearer_file_path() == _bridge_writer_path()
    assert bridge_auth.bridge_bearer() == "env-token"


def _live_bridge_server() -> Path | None:
    candidates = []
    if os.environ.get("LUCARYIN_AI_DIR"):
        candidates.append(Path(os.environ["LUCARYIN_AI_DIR"]))
    candidates.append(_RUNTIME.parents[2] / "lucaryin-ai")  # sibling checkout on a dev Mac
    for root in candidates:
        server = root / "hermes-bridge" / "server.py"
        if server.is_file():
            return server
    return None


def test_pinned_copy_matches_the_live_bridge():
    server = _live_bridge_server()
    if server is None:
        pytest.skip("no lucaryin-ai checkout next to this repo (set LUCARYIN_AI_DIR)")
    tree = ast.parse(server.read_text(encoding="utf-8"))
    live = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_bridge_bearer_file_path"), None)
    assert live is not None, f"_bridge_bearer_file_path missing from {server}"
    pinned = ast.parse(BRIDGE_WRITER_SOURCE).body[0]
    assert ast.dump(live) == ast.dump(pinned), (
        "hermes-bridge/server.py _bridge_bearer_file_path changed — update tools/bridge_auth.py "
        "and BRIDGE_WRITER_SOURCE together (rule E pair B1 + HA3)")


# ── behaviour ────────────────────────────────────────────────────────────────

def test_file_wins_over_env(clean, monkeypatch):
    _bearer_file(clean).write_text("file-token")  # the bridge writes no trailing newline
    monkeypatch.setenv(bridge_auth.BEARER_ENV, "env-token")
    assert bridge_auth.bridge_bearer() == "file-token"
    assert bridge_auth.bridge_auth_headers({"X": "1"}) == {"X": "1", "Authorization": "Bearer file-token"}


def test_surrounding_whitespace_is_ignored(clean):
    _bearer_file(clean).write_text("  file-token\n")
    assert bridge_auth.bridge_bearer() == "file-token"


def test_env_when_the_file_is_absent_or_empty(clean, monkeypatch):
    monkeypatch.setenv(bridge_auth.BEARER_ENV, " env-token ")
    assert bridge_auth.bridge_bearer() == "env-token"
    _bearer_file(clean).write_text("\n")
    assert bridge_auth.bridge_bearer() == "env-token"


def test_nothing_gives_empty_and_no_header(clean):
    assert bridge_auth.bridge_bearer() == ""
    assert bridge_auth.bridge_auth_headers() == {}


def test_auth_dir_override(clean, monkeypatch):
    (clean / "alt").mkdir()
    (clean / "alt" / "bridge.token").write_text("dir-token")
    monkeypatch.setenv(bridge_auth.AUTH_DIR_ENV, str(clean / "alt"))
    assert bridge_auth.bridge_bearer() == "dir-token"


def test_unreadable_file_falls_through(clean, monkeypatch):
    _bearer_file(clean).parent.mkdir(parents=True, exist_ok=True)
    (clean / ".lucaryin" / "auth" / "bridge.token").mkdir()  # a directory, not a file
    monkeypatch.setenv(bridge_auth.BEARER_ENV, "env-token")
    assert bridge_auth.bridge_bearer() == "env-token"


def test_the_file_tool_deny_does_not_stop_the_addon(clean, monkeypatch):
    """0008 refuses ~/.lucaryin/auth to the MODEL's file tools; the addon reads it
    with plain open() and must still get the token."""
    from agent.file_safety import get_read_block_error
    f = _bearer_file(clean)
    f.write_text("file-token")
    assert get_read_block_error(str(f))
    assert bridge_auth.bridge_bearer() == "file-token"


def test_fleet_send_headers_use_the_file(clean, monkeypatch):
    _bearer_file(clean).write_text("file-token")
    monkeypatch.setenv(bridge_auth.BEARER_ENV, "stale-env-token")
    assert fleet_send._auth_headers().get("Authorization") == "Bearer file-token"


# ── the two cron hooks (scheduler-side callers; review M13 survived without these) ─

@pytest.mark.parametrize("module", ["cron.meeting_join", "cron.p7_task_hook"])
def test_cron_hook_bearer_is_the_file_when_the_env_is_stripped(clean, module):
    """0025 strips BRIDGE_AUTH_TOKEN from children; a cron hook must still
    authenticate from bridge.token. Reverting either hook to an env-only read
    fails here."""
    import importlib
    hook = importlib.import_module(module)
    _bearer_file(clean).write_text("file-token")
    assert os.environ.get(bridge_auth.BEARER_ENV) is None
    assert hook._bridge_bearer() == "file-token"


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_meeting_join_dial_request_carries_the_file_bearer(clean, monkeypatch):
    from cron import meeting_join
    _bearer_file(clean).write_text("file-token")
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.get_header("Authorization"))
        return _Resp(b'{"success": true}')

    monkeypatch.setattr(meeting_join.urllib.request, "urlopen", fake_urlopen)
    meeting_join._post_dial({"dial_number": "+15555550100"})
    assert seen == ["Bearer file-token"], seen


def test_task_hook_request_carries_the_file_bearer(clean, monkeypatch):
    from cron import p7_task_hook
    _bearer_file(clean).write_text("file-token")
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.get_header("Authorization"))
        return _Resp(b'{"success": true}')

    monkeypatch.setattr(p7_task_hook.urllib.request, "urlopen", fake_urlopen)
    assert p7_task_hook._post("/api/tasks/start", {"task_id": "t-1"}) is True
    assert seen == ["Bearer file-token"], seen


def test_the_in_process_browser_tool_reads_the_file_first(clean, monkeypatch):
    """tools/browser_tool.py (open-browser and request_signin, patch 0010's
    surface) goes through the same reader (patch 0025)."""
    import inspect
    from tools import browser_tool
    _bearer_file(clean).write_text("file-token")
    monkeypatch.setenv(bridge_auth.BEARER_ENV, "stale-env-token")
    assert browser_tool._lucaryin_bridge_bearer() == "file-token"
    src = inspect.getsource(browser_tool)
    assert 'os.environ.get("BRIDGE_AUTH_TOKEN"' not in src.replace(
        inspect.getsource(browser_tool._lucaryin_bridge_bearer), "")


def test_no_addon_reads_the_env_directly_any_more():
    """Contract: the only ``BRIDGE_AUTH_TOKEN`` reads outside bridge_auth are the
    cron hooks' import-failure fallbacks."""
    offenders = []
    if ADDONS.is_dir():
        files = list((ADDONS / "tools").glob("*.py")) + list((ADDONS / "cron").glob("*.py"))
    else:  # a bare materialized tree: the known addon files only
        files = [_RUNTIME / "tools" / n for n in ADDON_FILES] + list((_RUNTIME / "cron").glob("*_hook.py"))
    assert files, "no addon sources found"
    for path in files:
        if path.name == "bridge_auth.py" or not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'os\.environ\.get\("BRIDGE_AUTH_TOKEN"', text):
            before = text[max(0, m.start() - 200):m.start()]
            if "from tools.bridge_auth import bridge_bearer" not in before:
                offenders.append(path.name)
    assert offenders == [], offenders
