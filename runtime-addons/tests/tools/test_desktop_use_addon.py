"""Lucaryin addon ``tools/desktop_use.py`` (Tier 2 native desktop computer-use): tool definitions
and honest TCC errors. Canary.5 soak, 2026-09-14:

1. ``tool_describe`` for desktop_screenshot / desktop_click / desktop_type returned
   ``{"description": "", "parameters": {"type": "object", "properties": {}}}`` — the agent guessed
   argument names and looped on "No app named". Upstream's ``registry.register(schema=...)``
   takes the FULL OpenAI function definition (``get_definitions`` emits ``{**schema, "name"}``);
   the addon still passed the pre-rebase parameters-only dict plus a ``description=`` kwarg.
   Three older addons (ask_agent, gbrain_search, gbrain_read) carried the sibling mistake — an
   Anthropic-style ``input_schema`` key nothing in the runtime reads.
2. The permission error said Screen Recording "is off for Lucaryin", but macOS files the grant
   under the Python binary running the agent worker — the box's prompt said "python3.12 is
   requesting to access…" and the System Settings entry is "python3.12", not "Lucaryin".

Bare tier: no pyobjc, no macOS — everything platform-specific is monkeypatched so this runs on
the Linux CI lane too.
"""

import importlib
import os
import pathlib
import sys
import types

import pytest

import tools.desktop_use as du
from tools.registry import registry

# name -> the argument names the model must see (also the ``required`` list, in order).
DESKTOP_TOOL_ARGS = {
    "desktop_list_apps": [],
    "desktop_screenshot": ["app"],
    "desktop_click": ["app", "x", "y", "description"],
    "desktop_type": ["app", "text", "description"],
    "desktop_key": ["app", "keys", "description"],
}


# ── Fix 2: full function definitions ─────────────────────────────────────────

class TestToolDefinitions:
    def test_each_desktop_tool_advertises_description_and_real_parameters(self):
        defs = {d["function"]["name"]: d["function"]
                for d in registry.get_definitions(set(DESKTOP_TOOL_ARGS))}
        assert set(defs) == set(DESKTOP_TOOL_ARGS)
        for name, args in DESKTOP_TOOL_ARGS.items():
            fn = defs[name]
            assert fn["description"].strip(), name
            params = fn["parameters"]
            assert params["type"] == "object", name
            assert sorted(params["properties"]) == sorted(args), name
            if args:
                assert params["required"] == args, name
                for arg in args:
                    assert params["properties"][arg]["type"], (name, arg)
            else:
                assert "required" not in params, name

    def test_registry_entry_description_comes_from_the_schema(self):
        entries = {e.name: e for e in registry._snapshot_entries() if e.name in DESKTOP_TOOL_ARGS}
        assert set(entries) == set(DESKTOP_TOOL_ARGS)
        for name, entry in entries.items():
            assert entry.description == entry.schema["description"], name
            assert entry.schema["name"] == name

    def test_every_addon_tool_registers_the_openai_function_shape(self):
        """Every ``runtime-addons/tools/*.py`` module: each tool it registers carries
        ``{"name", "description", "parameters"}`` — never a bare parameters object and never an
        ``input_schema`` key (which nothing in the runtime reads)."""
        addons_dir = pathlib.Path(__file__).resolve().parents[4] / "runtime-addons" / "tools"
        if not addons_dir.is_dir():
            pytest.skip("runtime-addons/tools not beside this materialized runtime")
        modules = {f"tools.{p.stem}" for p in addons_dir.glob("*.py") if p.name != "__init__.py"}
        for mod in sorted(modules):
            importlib.import_module(mod)
        seen = 0
        for entry in registry._snapshot_entries():
            handler_mod = getattr(entry.handler, "__module__", "") or ""
            if handler_mod not in modules:
                continue
            seen += 1
            schema = entry.schema
            assert isinstance(schema, dict), entry.name
            assert schema.get("name") == entry.name, entry.name
            assert (entry.description or "").strip(), entry.name
            assert "input_schema" not in schema, entry.name
            params = schema.get("parameters")
            assert isinstance(params, dict) and params.get("type") == "object", entry.name
            assert isinstance(params.get("properties"), dict), entry.name
        assert seen >= len(DESKTOP_TOOL_ARGS), "addon tools were not registered"

    def test_handler_accepts_the_registry_calling_convention(self, monkeypatch):
        """Upstream dispatches ``handler(args_dict, task_id=...)``; the kwargs adapter must unpack."""
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        entry = next(e for e in registry._snapshot_entries() if e.name == "desktop_screenshot")
        out = entry.handler({"app": "TextEdit"}, task_id="t1")
        assert out["ok"] is False and "pyobjc" in out["error"]


# ── Fix 3: honest TCC errors ─────────────────────────────────────────────────

@pytest.fixture
def worker_binary(tmp_path, monkeypatch):
    """A venv-style ``bin/python`` symlink to a ``python3.12`` binary, like
    ``~/.lucaryin/venvs/hermes/bin/python -> ~/.lucaryin/python/python/bin/python3.12``."""
    real = tmp_path / "python" / "bin" / "python3.12"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    os.symlink(real, link)
    monkeypatch.setattr(sys, "executable", str(link))
    return "python3.12"


@pytest.fixture
def ready_app(monkeypatch):
    """pyobjc "present", the app allowlisted, the prompt helpers recorded instead of run."""
    monkeypatch.setattr(du, "_pyobjc", lambda: (object(), object()))
    monkeypatch.setattr(du, "_allow_reason", lambda app: None)
    calls = []
    monkeypatch.setattr(du, "_request_screen_recording", lambda: calls.append("screen_recording"))
    monkeypatch.setattr(du, "_request_accessibility", lambda: calls.append("accessibility"))
    return calls


class TestTccErrors:
    def test_process_name_is_the_resolved_interpreter_binary(self, worker_binary):
        assert du._agent_process_name() == worker_binary

    def test_process_name_falls_back_to_python(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "")
        assert du._agent_process_name() == "python"

    def test_screen_recording_error_names_the_binary_pane_and_recovery(self, worker_binary, ready_app, monkeypatch):
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": True})
        out = du.desktop_screenshot(app="TextEdit")
        assert out["ok"] is False
        err = out["error"]
        assert '"python3.12"' in err
        assert '(not "Lucaryin")' in err
        assert "System Settings → Privacy & Security → Screen Recording" in err
        assert "retry" in err and "relaunch the Lucaryin app" in err
        assert ready_app == ["screen_recording"]  # the prompt was requested, once

    def test_accessibility_error_names_the_binary_and_pane(self, worker_binary, ready_app, monkeypatch):
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": True, "accessibility": False})
        out = du.desktop_click(app="TextEdit", x=10, y=20, description="the OK button")
        assert out["ok"] is False
        assert '"python3.12"' in out["error"]
        assert "System Settings → Privacy & Security → Accessibility" in out["error"]
        assert ready_app == ["accessibility"]
        # Read-tier actions do not need Accessibility: no error, no prompt.
        monkeypatch.setattr(du, "_focus", lambda app: False)
        assert "Couldn't bring" in du.desktop_screenshot(app="TextEdit")["error"]
        assert ready_app == ["accessibility"]

    def test_screen_recording_prompt_fires_once_per_process(self, monkeypatch):
        quartz = types.SimpleNamespace(calls=0)
        quartz.CGRequestScreenCaptureAccess = lambda: setattr(quartz, "calls", quartz.calls + 1) or False
        monkeypatch.setattr(du, "_pyobjc", lambda: (quartz, object()))
        monkeypatch.setattr(du, "_TCC_PROMPTED", set())
        du._request_screen_recording()
        du._request_screen_recording()
        assert quartz.calls == 1

    def test_accessibility_prompt_uses_the_prompt_option_once(self, monkeypatch):
        seen = []
        fake = types.ModuleType("ApplicationServices")
        fake.kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"
        fake.AXIsProcessTrustedWithOptions = lambda opts: seen.append(dict(opts)) or False
        fake.AXIsProcessTrusted = lambda: (_ for _ in ()).throw(AssertionError("prompting API exists"))
        monkeypatch.setitem(sys.modules, "ApplicationServices", fake)
        monkeypatch.setattr(du, "_TCC_PROMPTED", set())
        du._request_accessibility()
        du._request_accessibility()
        assert seen == [{"AXTrustedCheckOptionPrompt": True}]

    def test_accessibility_prompt_falls_back_to_non_prompting_check(self, monkeypatch):
        seen = []
        fake = types.ModuleType("ApplicationServices")  # no *WithOptions, no option constant
        fake.AXIsProcessTrusted = lambda: seen.append("plain") or False
        monkeypatch.setitem(sys.modules, "ApplicationServices", fake)
        monkeypatch.setattr(du, "_TCC_PROMPTED", set())
        du._request_accessibility()
        assert seen == ["plain"]

    def test_prompt_helpers_never_raise_without_pyobjc(self, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        monkeypatch.setitem(sys.modules, "ApplicationServices", None)  # import fails
        monkeypatch.setattr(du, "_TCC_PROMPTED", set())
        du._request_screen_recording()
        du._request_accessibility()

    def test_list_apps_permissions_name_the_process_to_enable(self, worker_binary, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), object()))
        monkeypatch.setattr(du, "_allowlist", lambda: ["TextEdit", "Quicken"])
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": None})
        out = du.desktop_list_apps()
        assert out["ok"] is True
        assert out["operable_apps"] == ["TextEdit"]  # financial app filtered
        perms = out["permissions"]
        assert perms["screen_recording"] is False
        assert perms["process"] == "python3.12"
        assert '"python3.12"' in perms["grant_under"]
        assert "Screen Recording" in perms["grant_under"] and "Accessibility" in perms["grant_under"]

    def test_tcc_status_carries_the_process_name(self, worker_binary, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        status = du._tcc_status()
        assert status["pyobjc"] is False and status["process"] == "python3.12"
