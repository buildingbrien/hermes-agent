"""Duplicate tool names within one toolset must be LOUD.

The shadow check only compared toolsets, so two modules claiming the same name
in the same toolset silently overwrote each other and import order picked the
winner. Real incident 2026-08-05: 'meeting_notes' was registered by both
tools/meeting_notes.py and tools/dial_meeting.py; the later module won,
shadowing the copy that carried a bug fix — the fix was inert in production and
nothing was logged.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.registry import ToolRegistry


def _handler_in(module_name):
    def h(args, **kw):
        return "{}"
    h.__module__ = module_name
    return h


SCHEMA = {"name": "x", "description": "d", "parameters": {}}


def test_duplicate_from_different_modules_is_logged(caplog):
    reg = ToolRegistry()
    reg.register(name="dup", toolset="voice", schema=SCHEMA, handler=_handler_in("tools.a"))
    with caplog.at_level(logging.ERROR):
        reg.register(name="dup", toolset="voice", schema=SCHEMA, handler=_handler_in("tools.b"))
    assert any("DUPLICATE tool registration" in r.message for r in caplog.records), \
        "a second module claiming the same name must be logged loudly"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "tools.a" in joined and "tools.b" in joined, "both modules must be named"


def test_same_module_reregistration_is_quiet(caplog):
    """Module re-import is benign — must not cry wolf."""
    reg = ToolRegistry()
    reg.register(name="dup", toolset="voice", schema=SCHEMA, handler=_handler_in("tools.a"))
    with caplog.at_level(logging.ERROR):
        reg.register(name="dup", toolset="voice", schema=SCHEMA, handler=_handler_in("tools.a"))
    assert not any("DUPLICATE" in r.message for r in caplog.records)


def test_last_registration_still_wins(caplog):
    """Behavior unchanged — this guard only makes the collision visible."""
    reg = ToolRegistry()
    reg.register(name="dup", toolset="voice", schema=SCHEMA, handler=_handler_in("tools.a"))
    reg.register(name="dup", toolset="voice", schema=SCHEMA, handler=_handler_in("tools.b"))
    entry = reg.get("dup") if hasattr(reg, "get") else reg._tools["dup"]
    assert getattr(entry.handler, "__module__") == "tools.b"
