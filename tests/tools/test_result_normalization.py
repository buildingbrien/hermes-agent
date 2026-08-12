"""Tool results are strings everywhere downstream — enforce it at the seams.

The failure this guards (2026-08-12, live): email_send returns a dict; the
display layer's failure sniffer did `result[:500]` on it; Python 3.12+ made
slices hashable so that raises KeyError(slice(None, 500, None)) instead of
TypeError; the exception escaped a `finally`, killed the whole turn — AFTER
the approved email had already sent — and the model, believing the send
failed, started composing a duplicate via the terminal.

Three layers, all tested here:
  1. registry.dispatch stringifies non-str handler results (root fix)
  2. _detect_tool_failure survives raw non-str input (belt)
  3. get_cute_tool_message NEVER raises (suspenders — a status line must
     never kill the turn it reports on)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.registry import registry
import agent.display as display
from agent.display import _detect_tool_failure, get_cute_tool_message


EMAIL_RESULT = {
    "sent": True,
    "to": ["someone@example.com"],
    "subject": "Test",
    "summary": "Sent — 1 recipient.",
}


# ── Layer 1: dispatch normalization ─────────────────────────────────────────

@pytest.fixture
def dict_tool():
    name = "_test_dict_result_tool"
    registry.register(
        name=name,
        toolset="rl",
        schema={"name": name, "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: dict(EMAIL_RESULT),
    )
    yield name
    registry._tools.pop(name, None)


def test_dispatch_stringifies_dict_results(dict_tool):
    result = registry.dispatch(dict_tool, {})
    assert isinstance(result, str)
    assert json.loads(result)["sent"] is True


def test_dispatch_stringifies_unjsonable_results():
    name = "_test_weird_result_tool"
    registry.register(
        name=name,
        toolset="rl",
        schema={"name": name, "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: {"path": Path("/tmp/x")},  # Path isn't JSON
    )
    try:
        result = registry.dispatch(name, {})
        assert isinstance(result, str)
        assert "/tmp/x" in result  # default=str carried it
    finally:
        registry._tools.pop(name, None)


def test_dispatch_leaves_string_results_alone(dict_tool):
    name = "_test_str_result_tool"
    registry.register(
        name=name,
        toolset="rl",
        schema={"name": name, "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: '{"already": "json"}',
    )
    try:
        assert registry.dispatch(name, {}) == '{"already": "json"}'
    finally:
        registry._tools.pop(name, None)


# ── Layer 2: the failure sniffer survives raw structures ────────────────────

def test_detect_failure_accepts_dict_success():
    is_failure, suffix = _detect_tool_failure("email_send", dict(EMAIL_RESULT))
    assert is_failure is False
    assert suffix == ""


def test_detect_failure_flags_dict_error():
    is_failure, suffix = _detect_tool_failure("email_send", {"error": "SMTP down"})
    assert is_failure is True


def test_detect_failure_none_is_fine():
    assert _detect_tool_failure("email_send", None) == (False, "")


# ── Layer 3: the status line can never kill the turn ────────────────────────

def test_cute_message_with_dict_result_returns_string():
    msg = get_cute_tool_message("email_send", {}, 1.5, result=dict(EMAIL_RESULT))
    assert isinstance(msg, str) and msg


def test_cute_message_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise KeyError(slice(None, 500, None))  # the exact live failure

    monkeypatch.setattr(display, "_detect_tool_failure", boom)
    msg = get_cute_tool_message("email_send", {}, 2.0, result="whatever")
    assert isinstance(msg, str)
    assert "email_send" in msg
