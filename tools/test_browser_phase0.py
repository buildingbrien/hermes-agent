"""Phase 0 browser tools: get_box, tab, dialog-warning surfacing, sticky-flag
reset. Mocks the CLI layer (_run_browser_command) — asserts wiring, not Chrome."""
import json
import importlib
import unittest.mock as m
import pytest

bt = importlib.import_module("tools.browser_tool")


def test_get_box_maps_ref_and_parses_box():
    with m.patch.object(bt, "_run_browser_command",
                        return_value={"success": True, "data": {"box": {"x": 10, "y": 20, "width": 100, "height": 30}}}) as run:
        out = json.loads(bt.browser_get_box("e5"))
    run.assert_called_once()
    args = run.call_args[0]
    assert args[1] == "get" and args[2] == ["box", "@e5"]  # @ prefix added
    assert out["success"] and out["box"]["width"] == 100


def test_get_box_failure():
    with m.patch.object(bt, "_run_browser_command", return_value={"success": False, "error": "no such ref"}):
        out = json.loads(bt.browser_get_box("@e9"))
    assert out["success"] is False and "no such ref" in out["error"]


def test_tab_list_new_close_switch():
    with m.patch.object(bt, "_run_browser_command", return_value={"success": True, "data": {"tabs": []}}) as run:
        json.loads(bt.browser_tab("list"))
        assert run.call_args[0][2] == ["list"]
        json.loads(bt.browser_tab("new"))
        assert run.call_args[0][2] == ["new"]
        json.loads(bt.browser_tab("switch", index=2))
        assert run.call_args[0][2] == ["2"]


def test_tab_switch_without_index_errors():
    out = json.loads(bt.browser_tab("switch"))
    assert out["success"] is False and "index" in out["error"]


def test_tab_unknown_action():
    out = json.loads(bt.browser_tab("frobnicate"))
    assert out["success"] is False


def test_click_surfaces_dialog_warning():
    with m.patch.object(bt, "_run_browser_command",
                        return_value={"success": True, "warning": "confirm() dialog is blocking the page"}), \
         m.patch.object(bt, "_is_camofox_mode", return_value=False):
        out = json.loads(bt.browser_click("@e2"))
    assert out["success"] and "confirm()" in out["dialog_warning"]


def test_click_no_warning_no_field():
    with m.patch.object(bt, "_run_browser_command", return_value={"success": True}), \
         m.patch.object(bt, "_is_camofox_mode", return_value=False):
        out = json.loads(bt.browser_click("@e2"))
    assert "dialog_warning" not in out


def test_sticky_unreachable_flag_resets_on_reachable():
    bt._SIGNED_IN_BROWSER_UNREACHABLE = True
    bt._mark_signed_in_reachable()
    assert bt._SIGNED_IN_BROWSER_UNREACHABLE is False


def test_new_tools_registered():
    from tools.registry import registry
    names = {t for t in registry.list_tools()} if hasattr(registry, "list_tools") else set()
    # fall back to schema map presence
    assert "browser_get_box" in bt._BROWSER_SCHEMA_MAP
    assert "browser_tab" in bt._BROWSER_SCHEMA_MAP


# ── Phase 1: visible cursor ──────────────────────────────────────────
from tools import browser_cursor as bc


def test_box_center():
    assert bc.box_center({"x": 100, "y": 50, "width": 40, "height": 20}) == (120.0, 60.0)
    assert bc.box_center({"nope": 1}) is None
    assert bc.box_center("not a dict") is None


def test_cursor_js_substitution_is_numeric_only():
    js = bc.cursor_move_js(300.7, 220.2, pulse=True)
    assert "{X}" not in js and "{Y}" not in js and "{PULSE}" not in js
    assert "300" in js and "220" in js and "true" in js
    # no caller string can reach the script — it's a fixed constant
    js2 = bc.cursor_move_js(1, 2, pulse=False)
    assert "false" in js2


def test_show_cursor_resolves_box_then_evals():
    calls = []
    def fake_run(task_id, cmd, args, timeout=None):
        calls.append((cmd, args))
        if cmd == "get":
            return {"success": True, "data": {"box": {"x": 10, "y": 20, "width": 100, "height": 40}}}
        return {"success": True}
    ok = bc.show_cursor_at(fake_run, "t1", "e5", pulse=True)
    assert ok is True
    assert calls[0] == ("get", ["box", "@e5"])
    assert calls[1][0] == "eval"
    # center of the box = (60, 40) must appear in the eval'd JS
    assert "60" in calls[1][1][0] and "40" in calls[1][1][0]


def test_show_cursor_never_raises_on_bad_box():
    ok = bc.show_cursor_at(lambda *a, **k: {"success": False}, "t1", "@e9")
    assert ok is False


def test_cursor_gate_off_by_env(monkeypatch):
    monkeypatch.setenv("LUCARYIN_BROWSER_CURSOR", "0")
    assert bt._cursor_enabled("t1") is False


# ── Phase 2: supervisor + dialog integration ─────────────────────────
from tools import browser_supervisor as bsup
from tools import browser_dialog_tool as bdlg


def test_pending_dialog_to_dict():
    d = bsup.PendingDialog(id="d1", type="confirm", message="Sure?",
                           default_prompt="", opened_at=0.0, cdp_session_id="s1")
    j = d.to_dict()
    assert j["id"] == "d1" and j["type"] == "confirm" and j["message"] == "Sure?"


def test_browser_dialog_no_supervisor_is_clear_error():
    with m.patch.object(bdlg.SUPERVISOR_REGISTRY, "get", return_value=None):
        out = json.loads(bdlg.browser_dialog("accept", task_id="t1"))
    assert out["success"] is False and "supervisor" in out["error"].lower()


def test_browser_dialog_routes_to_supervisor():
    fake = m.MagicMock()
    fake.respond_to_dialog.return_value = {"ok": True, "dialog": {"id": "d1"}}
    with m.patch.object(bdlg.SUPERVISOR_REGISTRY, "get", return_value=fake):
        out = json.loads(bdlg.browser_dialog("dismiss", task_id="t1"))
    assert out["success"] is True and out["action"] == "dismiss"
    fake.respond_to_dialog.assert_called_once()


def test_snapshot_merge_surfaces_pending_dialog():
    fake_sup = m.MagicMock()
    snap = m.MagicMock()
    pd = bsup.PendingDialog(id="d9", type="prompt", message="name?",
                            default_prompt="", opened_at=0.0, cdp_session_id="s1")
    snap.pending_dialogs = [pd]
    fake_sup.snapshot.return_value = snap
    resp = {"success": True}
    with m.patch.object(bt, "_merge_supervisor_state", bt._merge_supervisor_state), \
         m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = fake_sup
        bt._merge_supervisor_state("t1", resp)
    assert "pending_dialogs" in resp and resp["pending_dialogs"][0]["id"] == "d9"
    assert "dialog_hint" in resp


def test_merge_no_supervisor_is_noop():
    resp = {"success": True}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = None
        bt._merge_supervisor_state("t1", resp)
    assert "pending_dialogs" not in resp


def test_cleanup_stops_supervisor_best_effort():
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg, \
         m.patch.object(bt, "_is_camofox_mode", return_value=False), \
         m.patch.object(bt, "_run_browser_command", return_value={"success": True}):
        bt.cleanup_browser("t-cleanup")
        reg.stop.assert_called_with("t-cleanup")


def test_redact_cdp_url_masks_token():
    from agent.redact import redact_cdp_url
    assert "***" in redact_cdp_url("ws://127.0.0.1:9222/devtools/browser/SECRET-GUID")
    assert "SECRET-GUID" not in redact_cdp_url("ws://127.0.0.1:9222/devtools/browser/SECRET-GUID")
