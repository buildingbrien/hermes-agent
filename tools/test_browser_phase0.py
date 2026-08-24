"""Phase 0 browser tools: get_box, tab, dialog-warning surfacing, sticky-flag
reset. Mocks the CLI layer (_run_browser_command) — asserts wiring, not Chrome."""
import json
import os
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


# ── Phase 2 increment 2: fast eval + frame tree ──────────────────────
def test_eval_via_supervisor_success():
    fake = m.MagicMock()
    fake.evaluate_runtime.return_value = {"ok": True, "result": '{"a":1}', "result_type": "string"}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = fake
        out = bt._eval_via_supervisor("t1", "({a:1})")
    assert out is not None
    d = json.loads(out)
    assert d["success"] and d["via"] == "supervisor" and d["result"] == {"a": 1}


def test_eval_via_supervisor_none_when_no_supervisor():
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = None
        assert bt._eval_via_supervisor("t1", "1+1") is None


def test_eval_via_supervisor_none_on_failure_falls_back():
    fake = m.MagicMock()
    fake.evaluate_runtime.return_value = {"ok": False, "error": "boom"}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = fake
        assert bt._eval_via_supervisor("t1", "1+1") is None  # → subprocess path


def test_browser_eval_prefers_supervisor():
    with m.patch.object(bt, "_is_camofox_mode", return_value=False), \
         m.patch.object(bt, "_eval_via_supervisor", return_value='{"success":true,"via":"supervisor"}') as fast, \
         m.patch.object(bt, "_run_browser_command") as sub:
        out = bt._browser_eval("document.title", "t1")
    fast.assert_called_once()
    sub.assert_not_called()  # never touched the subprocess CLI
    assert json.loads(out)["via"] == "supervisor"


def test_frame_tree_merged_only_when_children():
    fake_sup = m.MagicMock()
    snap = m.MagicMock()
    snap.pending_dialogs = []
    snap.frame_tree = {"top": {"id": "f0"}, "children": [{"id": "f1", "oopif": True}]}
    fake_sup.snapshot.return_value = snap
    resp = {"success": True}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = fake_sup
        bt._merge_supervisor_state("t1", resp)
    assert resp.get("frame_tree", {}).get("children")

    # no children → not attached
    snap.frame_tree = {"top": None, "children": []}
    resp2 = {"success": True}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = fake_sup
        bt._merge_supervisor_state("t1", resp2)
    assert "frame_tree" not in resp2


# ── Phase 3: snapshot / coordinate honesty ───────────────────────────
def test_count_visible_refs_boundary_safe():
    text = "button [ref=e5] link [ref=e12] input @e3"
    refs = {"e5": 1, "e12": 1, "e3": 1, "e50": 1}  # e50 is NOT in text
    n = bt._count_visible_refs(text, refs)
    assert n == 3  # e5, e12, e3 present; e50 absent (and e5 doesn't match e50)


def test_count_visible_refs_empty():
    assert bt._count_visible_refs("anything", {}) == 0


def test_snapshot_honest_count_and_paging(tmp_path, monkeypatch):
    monkeypatch.setenv("LUCARYIN_HOME", str(tmp_path))
    big = "x" * 9000 + " ".join(f"[ref=e{i}]" for i in range(60))
    with m.patch.object(bt, "_is_camofox_mode", return_value=False), \
         m.patch.object(bt, "_get_session_info", return_value={"features": {}}), \
         m.patch.object(bt, "_run_browser_command",
                        return_value={"success": True, "data": {"snapshot": big,
                                      "refs": {f"e{i}": 1 for i in range(60)}}}), \
         m.patch.object(bt, "_extract_relevant_content", side_effect=lambda t, u: "[ref=e0] [ref=e1] [ref=e2]"), \
         m.patch.object(bt, "_merge_supervisor_state", lambda *a: None):
        out = json.loads(bt.browser_snapshot(task_id="t1", user_task="find login"))
    assert out["total_element_count"] == 60
    assert out["element_count"] == 3          # only 3 refs survived summarization
    assert out["truncated"] is True
    assert "full_snapshot_path" in out and os.path.exists(out["full_snapshot_path"])
    assert "3 of 60" in out["truncation_hint"]


def test_post_action_adds_stale_hint_without_supervisor():
    resp = {"success": True, "clicked": "@e2"}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = None
        bt._post_action_state("t1", resp)
    assert "refs_stale_hint" in resp
    assert "url" not in resp  # no supervisor → no cheap url/title


def test_post_action_grabs_url_title_via_supervisor():
    fake = m.MagicMock()
    fake.evaluate_runtime.return_value = {"ok": True, "result": '{"u":"https://x.com/y","t":"Y Page"}'}
    resp = {"success": True, "clicked": "@e2"}
    with m.patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = fake
        bt._post_action_state("t1", resp)
    assert resp["url"] == "https://x.com/y" and resp["title"] == "Y Page"
    assert "refs_stale_hint" in resp


# ── Phase 4: session lifecycle ───────────────────────────────────────
def test_reaper_tiers_cdp_attached_sessions(monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(bt, "BROWSER_SESSION_INACTIVITY_TIMEOUT", 300)
    monkeypatch.setattr(bt, "CDP_ATTACHED_INACTIVITY_TIMEOUT", 3600)
    # one headless (idle 400s → reap), one CDP-attached (idle 400s → keep)
    bt._session_last_activity.clear(); bt._cdp_attached_tasks.clear()
    bt._session_last_activity["headless"] = now - 400
    bt._session_last_activity["signedin"] = now - 400
    bt._cdp_attached_tasks.add("signedin")
    reaped = []
    with m.patch.object(bt, "time") as tmock, \
         m.patch.object(bt, "cleanup_browser", side_effect=lambda t: reaped.append(t)):
        tmock.time.return_value = now
        bt._cleanup_inactive_browser_sessions()
    assert "headless" in reaped and "signedin" not in reaped
    bt._session_last_activity.clear(); bt._cdp_attached_tasks.clear()


def test_reaper_reaps_cdp_after_long_ttl(monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(bt, "CDP_ATTACHED_INACTIVITY_TIMEOUT", 3600)
    bt._session_last_activity.clear(); bt._cdp_attached_tasks.clear()
    bt._session_last_activity["signedin"] = now - 4000  # > 1h idle
    bt._cdp_attached_tasks.add("signedin")
    reaped = []
    with m.patch.object(bt, "time") as tmock, \
         m.patch.object(bt, "cleanup_browser", side_effect=lambda t: reaped.append(t)):
        tmock.time.return_value = now
        bt._cleanup_inactive_browser_sessions()
    assert "signedin" in reaped
    bt._session_last_activity.clear(); bt._cdp_attached_tasks.clear()


def test_browser_back_blocks_private_redirect():
    with m.patch.object(bt, "_is_camofox_mode", return_value=False), \
         m.patch.object(bt, "_is_local_backend", return_value=False), \
         m.patch.object(bt, "_allow_private_urls", return_value=False), \
         m.patch.object(bt, "_is_safe_url", return_value=False), \
         m.patch.object(bt, "_run_browser_command",
                        return_value={"success": True, "data": {"url": "http://169.254.169.254/"}}):
        out = json.loads(bt.browser_back("t1"))
    assert out["success"] is False and "private/internal" in out["error"]


def test_browser_back_allows_safe_url():
    with m.patch.object(bt, "_is_camofox_mode", return_value=False), \
         m.patch.object(bt, "_is_local_backend", return_value=False), \
         m.patch.object(bt, "_allow_private_urls", return_value=False), \
         m.patch.object(bt, "_is_safe_url", return_value=True), \
         m.patch.object(bt, "_run_browser_command",
                        return_value={"success": True, "data": {"url": "https://example.com/"}}):
        out = json.loads(bt.browser_back("t1"))
    assert out["success"] is True and out["url"] == "https://example.com/"
