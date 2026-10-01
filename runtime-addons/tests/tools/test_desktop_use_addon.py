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
   Review fix-up: the name comes from the kernel's executable image (dyld
   ``_NSGetExecutablePath``, resolved), not ``sys.executable`` — on Homebrew / python.org
   framework builds ``bin/python3.13`` is a launcher that execs ``Python.app/Contents/MacOS/Python``
   and System Settings lists the bundle, "Python".
3. canary.6 verification sweep (2026-09-14): item 2 is right only for a worker with NO app above
   it. macOS files a TCC grant under the RESPONSIBLE process, and the fleet's bridges are spawned
   by the Electron app — TCC.db on the canary box carries kTCCServiceScreenCapture +
   kTCCServiceAccessibility rows for com.lucaryin.lucaryin-ai and no python3.12 row; System
   Settings lists "Lucaryin AI" — so the "python3.12" hint pointed at an entry that does not
   exist. The subject is now resolved at runtime (libquarantine responsible pid → that process's
   image → its .app bundle's display name) with the interpreter image kept for a worker nothing
   else is responsible for.
4. Review fix-up: responsibility is held by whatever launchd started and inherited by its
   children, so a responsible process that is NOT an .app is named too — a LaunchAgent's wrapper
   script (``bash``), an SSH session (``sshd-keygen-wrapper``) — as subject_kind "process";
   falling back to the interpreter there pointed at an entry that does not exist. The interpreter
   is right only when this process is responsible for itself (launchd started it directly) or the
   lookup failed. Terminal.app is the responsible process for its shell children, so a
   terminal-launched worker is filed under "Terminal" (kind "app"). App names come from
   LaunchServices (``NSFileManager.displayNameAtPath_``, the label the pane shows) first, then
   CFBundleDisplayName, then the .app folder name; CFBundleName is never consulted.

Bare tier: no pyobjc, no macOS — everything platform-specific is monkeypatched so this runs on
the Linux CI lane too; the darwin-only cases run for real on the ``runtime-tests-macos`` lane.
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

HOMEBREW_FRAMEWORK_IMAGE = ("/opt/homebrew/Cellar/python@3.13/3.13.15/Frameworks/Python.framework/"
                            "Versions/3.13/Resources/Python.app/Contents/MacOS/Python")
PYTHON_ORG_FRAMEWORK_IMAGE = ("/Library/Frameworks/Python.framework/Versions/3.13/Resources/"
                              "Python.app/Contents/MacOS/Python")
STANDALONE_IMAGE = "/Users/someone/.lucaryin/python/python/bin/python3.12"
LUCARYIN_APP_EXECUTABLE = "/Applications/Lucaryin AI.app/Contents/MacOS/Lucaryin AI"

# The live lookups, captured before any test pins them (the off-darwin / darwin-only tests
# exercise these directly).
_LIVE_RESPONSIBLE_PID = du._responsible_pid
_LIVE_PID_EXECUTABLE_PATH = du._pid_executable_path
_LIVE_BUNDLE_DISPLAY_NAME = du._bundle_display_name


@pytest.fixture(autouse=True)
def nothing_above_this_process(monkeypatch):
    """Bare tier: pin the responsibility lookups to "no process above this one" so every test
    derives the TCC subject from the pinned image path, and the LaunchServices display-name
    lookup to "unavailable" so app names come from the injected plist / folder name. On a
    developer Mac the real lookups name the app running pytest (Terminal, an IDE) — never the
    fleet's shape."""
    monkeypatch.setattr(du, "_responsible_pid", lambda pid: 0)
    monkeypatch.setattr(du, "_pid_executable_path", lambda pid: "")
    monkeypatch.setattr(du, "_bundle_display_name", lambda bundle_dir: "")


@pytest.fixture
def app_spawned(monkeypatch):
    """The fleet shape: the Lucaryin app spawned this worker, so macOS holds the app responsible —
    pid 4242, image inside the .app bundle, no Info.plist on disk here. Returns the pids whose
    image was looked up."""
    seen = []
    monkeypatch.setattr(du, "_responsible_pid", lambda pid: 4242)
    monkeypatch.setattr(du, "_pid_executable_path",
                        lambda pid: seen.append(pid) or LUCARYIN_APP_EXECUTABLE)
    monkeypatch.setattr(du, "_bundle_info", lambda bundle_dir: {})
    return seen


@pytest.fixture
def wrapper_spawned(monkeypatch):
    """A LaunchAgent whose program is a shell script that execs the worker: macOS holds ``bash``
    responsible — pid 4343, image ``/bin/bash``, no app anywhere above."""
    monkeypatch.setattr(du, "_responsible_pid", lambda pid: 4343)
    monkeypatch.setattr(du, "_pid_executable_path", lambda pid: "/bin/bash")


@pytest.fixture
def worker_binary(tmp_path, monkeypatch):
    """The fleet shape: a venv-style ``bin/python`` symlink to a real ``python3.12`` Mach-O, like
    ``~/.lucaryin/venvs/hermes/bin/python -> ~/.lucaryin/python/python/bin/python3.12``. dyld reports
    the path AS EXEC'D — the symlink (verified on macOS: ``_NSGetExecutablePath`` does not resolve
    it) — so the kernel path is the symlink here and the name must come from its target."""
    real = tmp_path / "python" / "bin" / "python3.12"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    os.symlink(real, link)
    monkeypatch.setattr(sys, "executable", str(link))
    monkeypatch.setattr(du, "_kernel_image_path", lambda: str(link))
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


class TestProcessName:
    """The interpreter case's name chain (``_kernel_image_path`` → ``_executable_image_path`` →
    ``_tcc_process_name_for_path``) as ``_tcc_subject`` reaches it while nothing else is
    responsible for this process (the autouse fixture)."""

    def test_process_name_is_the_resolved_interpreter_binary(self, worker_binary):
        assert du._tcc_subject()["process"] == worker_binary

    def test_kernel_image_wins_over_sys_executable_on_framework_builds(self, monkeypatch):
        """Homebrew / python.org: ``bin/python3.13`` is a launcher that execs
        ``Python.app/Contents/MacOS/Python``; ``sys.executable`` is rewritten to the launcher while
        the kernel holds the bundle image — the one TCC lists, as "Python"."""
        monkeypatch.setattr(sys, "executable", "/opt/homebrew/opt/python@3.13/bin/python3.13")
        monkeypatch.setattr(du, "_kernel_image_path", lambda: HOMEBREW_FRAMEWORK_IMAGE)
        assert du._tcc_subject()["process"] == "Python"

    def test_falls_back_to_sys_executable_without_a_kernel_path(self, tmp_path, monkeypatch):
        """Off darwin / dyld failure: ``realpath(sys.executable)`` as before."""
        real = tmp_path / "python" / "bin" / "python3.12"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\n")
        link = tmp_path / "venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        os.symlink(real, link)
        monkeypatch.setattr(du, "_kernel_image_path", lambda: "")
        monkeypatch.setattr(sys, "executable", str(link))
        assert du._tcc_subject()["process"] == "python3.12"

    def test_process_name_falls_back_to_python(self, monkeypatch):
        monkeypatch.setattr(du, "_kernel_image_path", lambda: "")
        monkeypatch.setattr(sys, "executable", "")
        assert du._tcc_subject()["process"] == "python"

    @pytest.mark.parametrize("path, expected", [
        (HOMEBREW_FRAMEWORK_IMAGE, "Python"),
        (PYTHON_ORG_FRAMEWORK_IMAGE, "Python"),
        (STANDALONE_IMAGE, "python3.12"),
        ("/Applications/Outer.app/Contents/MacOS/Inner.app/Contents/MacOS/Inner", "Inner"),
        ("/Applications/Xcode.app/Contents/Developer/usr/bin/python3", "python3"),
        ("", "python"),
    ])
    def test_tcc_name_mapping_is_the_bundle_or_the_file_name(self, path, expected):
        """Pure mapping, no ctypes: an executable inside an app bundle is listed as the (innermost)
        bundle; a bare Mach-O by file name; a path under an .app that is NOT its Contents/MacOS
        executable is not a bundle executable."""
        assert du._tcc_process_name_for_path(path) == expected

    def test_kernel_image_path_is_empty_off_darwin(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert du._kernel_image_path() == ""

    @pytest.mark.skipif(sys.platform != "darwin", reason="dyld _NSGetExecutablePath is macOS-only")
    def test_kernel_image_path_is_this_interpreter_on_darwin(self):
        path = du._kernel_image_path()
        assert os.path.isabs(path) and os.path.exists(path)
        assert du._executable_image_path() == os.path.realpath(path)
        assert du._tcc_subject()["process"] == du._tcc_process_name_for_path(os.path.realpath(path))


class TestTccSubject:
    """``_tcc_subject_for`` (pure) and the hook-fed ``_tcc_subject`` — no libquarantine, no
    libproc, no macOS."""

    def test_app_spawned_worker_is_filed_under_the_app(self):
        out = du._tcc_subject_for(own_pid=100, responsible_pid=4242,
                                  responsible_path=LUCARYIN_APP_EXECUTABLE,
                                  image_path=STANDALONE_IMAGE, bundle_info=lambda d: {})
        assert out == {"process": "Lucaryin AI", "subject_kind": "app", "bundle_id": None}

    def test_bundle_display_name_and_id_come_from_info_plist(self, tmp_path):
        """The real plist reader (LaunchServices pinned to "unavailable" by the autouse fixture):
        CFBundleDisplayName wins, then the folder name — CFBundleName is never consulted; the
        bundle id rides along when the plist is readable."""
        import plistlib
        bundle = tmp_path / "Lucaryin AI.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        exe = str(bundle / "Contents" / "MacOS" / "Lucaryin AI")
        plist = bundle / "Contents" / "Info.plist"
        common = {"CFBundleIdentifier": "com.lucaryin.lucaryin-ai", "CFBundleName": "lucaryin-ai"}
        plist.write_bytes(plistlib.dumps({**common, "CFBundleDisplayName": "Lucaryin AI"}))
        assert du._tcc_subject_for(100, 4242, exe, STANDALONE_IMAGE) == {
            "process": "Lucaryin AI", "subject_kind": "app", "bundle_id": "com.lucaryin.lucaryin-ai"}
        plist.write_bytes(plistlib.dumps(common))  # CFBundleName only → the folder, not "lucaryin-ai"
        assert du._tcc_subject_for(100, 4242, exe, STANDALONE_IMAGE) == {
            "process": "Lucaryin AI", "subject_kind": "app", "bundle_id": "com.lucaryin.lucaryin-ai"}
        plist.write_bytes(b"not a plist")
        assert du._tcc_subject_for(100, 4242, exe, STANDALONE_IMAGE) == {
            "process": "Lucaryin AI", "subject_kind": "app", "bundle_id": None}

    CLAUDE_APP_EXECUTABLE = ("/Users/dev/Library/Application Support/Claude/claude-code/2.1.260/"
                             "claude.app/Contents/MacOS/claude")

    @pytest.mark.parametrize("launchservices, info, expected", [
        # A dev Mac's claude.app: CFBundleName "Claude Code", no CFBundleDisplayName, and the pane
        # shows LaunchServices' "claude" — CFBundleName would have named a non-existent entry.
        ("claude", {"CFBundleName": "Claude Code"}, "claude"),
        ("claude", {"CFBundleName": "Claude Code", "CFBundleDisplayName": "Claude"}, "claude"),
        ("Foo.app", {}, "Foo.app"),                     # the mapper does not second-guess LaunchServices
        ("", {"CFBundleDisplayName": "Claude", "CFBundleName": "Claude Code"}, "Claude"),
        ("  ", {"CFBundleDisplayName": "  Claude  "}, "Claude"),
        ("", {"CFBundleName": "Claude Code"}, "claude"),  # the folder name; CFBundleName never
        ("", {}, "claude"),
    ])
    def test_app_name_prefers_launchservices_then_plist_display_name_then_folder(
            self, launchservices, info, expected):
        """Pure precedence with injected values: LaunchServices display name → CFBundleDisplayName
        → the .app folder name. The bundle id is independent of the name."""
        asked = []
        out = du._tcc_subject_for(
            100, 4242, self.CLAUDE_APP_EXECUTABLE, STANDALONE_IMAGE,
            bundle_info=lambda d: {**info, "CFBundleIdentifier": "com.anthropic.claude-code"},
            display_name=lambda d: asked.append(d) or launchservices)
        assert out == {"process": expected, "subject_kind": "app",
                       "bundle_id": "com.anthropic.claude-code"}
        assert asked == [self.CLAUDE_APP_EXECUTABLE[:-len("/Contents/MacOS/claude")]]

    def test_display_name_reader_is_gated_on_darwin_pyobjc_and_a_bundle_on_disk(self, tmp_path, monkeypatch):
        """``_bundle_display_name`` (the live reader, not the pinned one): "" off darwin, "" without
        pyobjc, "" for a bundle that is not on disk — without touching AppKit in any of those
        cases — and, for a bundle on disk, ``NSFileManager.displayNameAtPath_`` minus any ".app"
        Finder was told to show."""
        class _NoTouch:
            def __getattr__(self, name):
                raise AssertionError("AppKit consulted")
        answers = {}

        class _FM:
            def displayNameAtPath_(self, path):
                return answers[path]

        class _AppKit:
            class NSFileManager:
                @staticmethod
                def defaultManager():
                    return _FM()
        bundle = tmp_path / "Fake Thing.app"
        bundle.mkdir()
        on_disk = str(bundle)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), _NoTouch()))
        assert _LIVE_BUNDLE_DISPLAY_NAME(on_disk) == ""
        monkeypatch.setattr(sys, "platform", "darwin")
        assert _LIVE_BUNDLE_DISPLAY_NAME(str(tmp_path / "Missing.app")) == ""
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        assert _LIVE_BUNDLE_DISPLAY_NAME(on_disk) == ""
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), _AppKit()))
        answers[on_disk] = "Fake Thing"
        assert _LIVE_BUNDLE_DISPLAY_NAME(on_disk) == "Fake Thing"
        answers[on_disk] = "Fake Thing.APP"
        assert _LIVE_BUNDLE_DISPLAY_NAME(on_disk) == "Fake Thing"
        answers[on_disk] = None
        assert _LIVE_BUNDLE_DISPLAY_NAME(on_disk) == ""
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), _NoTouch()))
        assert _LIVE_BUNDLE_DISPLAY_NAME(on_disk) == ""  # AppKit raising → ""

    def test_worker_responsible_for_itself_is_filed_under_the_interpreter(self):
        """launchd started the interpreter itself, or the lookup failed: the responsible pid is
        our own, or 0. (A terminal-launched worker is NOT this case — Terminal.app is responsible
        for its shell children, so that one is filed under "Terminal", kind "app".)"""
        for responsible in (100, 0):
            out = du._tcc_subject_for(100, responsible, "", STANDALONE_IMAGE, bundle_info=lambda d: {})
            assert out == {"process": "python3.12", "subject_kind": "interpreter", "bundle_id": None}

    def test_framework_build_under_no_app_is_the_python_bundle(self):
        out = du._tcc_subject_for(100, 100, "", HOMEBREW_FRAMEWORK_IMAGE, bundle_info=lambda d: {})
        assert out == {"process": "Python", "subject_kind": "interpreter", "bundle_id": None}

    def test_every_lookup_failing_names_python(self):
        out = du._tcc_subject_for(100, 0, "", "", bundle_info=lambda d: {})
        assert out == {"process": "python", "subject_kind": "interpreter", "bundle_id": None}

    @pytest.mark.parametrize("responsible_path, expected", [
        ("/usr/libexec/sshd-keygen-wrapper", "sshd-keygen-wrapper"),  # an SSH session
        ("/bin/bash", "bash"),                                        # a LaunchAgent's wrapper script
        ("/usr/sbin/sshd", "sshd"),
        ("/opt/homebrew/bin/node/", "node"),
    ])
    def test_responsible_process_outside_an_app_is_named_by_its_binary(self, responsible_path, expected):
        """A responsible process that is not an .app still holds the grant — responsibility is
        inherited from whatever launchd started — so it is named, never the interpreter (which
        would point at an entry that does not exist)."""
        out = du._tcc_subject_for(100, 77, responsible_path, STANDALONE_IMAGE, bundle_info=lambda d: {},
                                  display_name=lambda d: (_ for _ in ()).throw(AssertionError("not an app")))
        assert out == {"process": expected, "subject_kind": "process", "bundle_id": None}

    def test_app_and_process_kinds_need_another_process_with_a_known_image(self):
        """Own pid, pid 0 or an unknown image never yield "app" / "process"."""
        for own, responsible, path in ((100, 100, "/bin/bash"), (100, 0, "/bin/bash"), (100, 77, "")):
            out = du._tcc_subject_for(own, responsible, path, STANDALONE_IMAGE, bundle_info=lambda d: {})
            assert out == {"process": "python3.12", "subject_kind": "interpreter", "bundle_id": None}

    def test_nested_bundle_names_the_innermost_app(self):
        path = "/Applications/Outer.app/Contents/MacOS/Inner.app/Contents/MacOS/Inner"
        out = du._tcc_subject_for(100, 77, path, STANDALONE_IMAGE, bundle_info=lambda d: {})
        assert out["process"] == "Inner" and out["subject_kind"] == "app"

    def test_bundle_info_reader_never_raises(self, tmp_path):
        assert du._bundle_info(str(tmp_path / "Missing.app")) == {}

    def test_live_subject_is_fed_by_the_hooks(self, worker_binary, app_spawned):
        """``_tcc_subject`` asks for the responsible pid, then THAT pid's image, then maps."""
        assert du._tcc_subject() == {"process": "Lucaryin AI", "subject_kind": "app", "bundle_id": None}
        assert app_spawned == [4242]

    def test_live_subject_names_a_wrapper_script_above_us(self, worker_binary, wrapper_spawned):
        assert du._tcc_subject() == {"process": "bash", "subject_kind": "process", "bundle_id": None}

    def test_live_subject_skips_the_path_lookup_when_nothing_is_above_us(self, worker_binary, monkeypatch):
        looked_up = []
        monkeypatch.setattr(du, "_responsible_pid", lambda pid: pid)  # macOS: "you are responsible"
        monkeypatch.setattr(du, "_pid_executable_path", lambda pid: looked_up.append(pid) or "/x")
        assert du._tcc_subject()["process"] == "python3.12"
        assert looked_up == []

    def test_lookups_are_empty_off_darwin(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert _LIVE_RESPONSIBLE_PID(1) == 0
        assert _LIVE_PID_EXECUTABLE_PATH(1) == ""
        assert _LIVE_BUNDLE_DISPLAY_NAME("/Applications/Lucaryin AI.app") == ""

    def test_path_lookup_rejects_a_non_pid(self):
        assert _LIVE_PID_EXECUTABLE_PATH(0) == "" and _LIVE_PID_EXECUTABLE_PATH(-1) == ""

    @pytest.mark.skipif(sys.platform != "darwin", reason="libquarantine / libproc are macOS-only")
    def test_live_lookups_resolve_this_interpreter_on_darwin(self):
        """The real libquarantine / libproc calls (runs on the ``runtime-tests-macos`` lane). The
        API answers > 0 for a live pid — a self-responsible process gets its own pid — so a
        missing symbol (``_responsible_pid`` maps -1 to 0, which would name every worker as the
        interpreter, the canary.6 bug) fails here instead of passing quietly."""
        me = os.getpid()
        responsible = _LIVE_RESPONSIBLE_PID(me)
        assert responsible > 0
        path = _LIVE_PID_EXECUTABLE_PATH(me)
        assert os.path.isabs(path) and os.path.exists(path)
        assert os.path.realpath(path) == du._executable_image_path()
        responsible_path = _LIVE_PID_EXECUTABLE_PATH(responsible)
        if responsible != me:
            assert os.path.isabs(responsible_path) and os.path.exists(responsible_path)
        subject = du._tcc_subject_for(me, responsible, responsible_path, du._executable_image_path(),
                                      display_name=_LIVE_BUNDLE_DISPLAY_NAME)
        assert subject["process"] and subject["subject_kind"] in ("app", "process", "interpreter")
        if responsible == me:
            assert subject["subject_kind"] == "interpreter"
        else:
            assert subject["subject_kind"] != "interpreter"

    @pytest.mark.skipif(sys.platform != "darwin", reason="NSFileManager is macOS-only")
    def test_live_display_name_is_launchservices_label_on_darwin(self, tmp_path):
        """The real AppKit call (the darwin lane installs pyobjc): a system app's label, "" for a
        bundle that is not on disk (never the echoed path component), and a bare folder named
        like an app is labelled without its ".app"."""
        if du._pyobjc() is None:
            pytest.skip("pyobjc not installed in this interpreter")
        textedit = "/System/Applications/TextEdit.app"
        if os.path.isdir(textedit):
            assert _LIVE_BUNDLE_DISPLAY_NAME(textedit) == "TextEdit"
        assert _LIVE_BUNDLE_DISPLAY_NAME(str(tmp_path / "Missing.app")) == ""
        (tmp_path / "Fake Thing.app").mkdir()
        assert _LIVE_BUNDLE_DISPLAY_NAME(str(tmp_path / "Fake Thing.app")) == "Fake Thing"


class TestTccErrors:
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

    def test_screen_recording_error_names_the_app_when_it_launched_the_worker(
            self, worker_binary, app_spawned, ready_app, monkeypatch):
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": True})
        err = du.desktop_screenshot(app="TextEdit")["error"]
        assert '"Lucaryin AI"' in err and "app that launched the worker" in err
        assert "python3.12" not in err and '(not "Lucaryin")' not in err
        assert "System Settings → Privacy & Security → Screen Recording" in err
        assert "retry" in err and "relaunch the Lucaryin app" in err
        assert ready_app == ["screen_recording"]

    def test_screen_recording_error_names_the_launching_process_when_it_is_not_an_app(
            self, worker_binary, wrapper_spawned, ready_app, monkeypatch):
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": True})
        err = du.desktop_screenshot(app="TextEdit")["error"]
        assert '"bash"' in err and "process that launched the worker" in err
        assert '(not "Lucaryin")' in err
        assert "python3.12" not in err and "app that launched" not in err
        assert "System Settings → Privacy & Security → Screen Recording" in err
        assert 'Switch "bash" on there' in err
        assert "retry" in err and "relaunch the Lucaryin app" in err
        assert ready_app == ["screen_recording"]

    def test_tcc_help_uses_the_status_subject_instead_of_a_second_lookup(self, monkeypatch):
        """``_require_ready`` hands ``_tcc_help`` the ``_tcc_status()`` result so the subject is
        resolved once per check; a status without a subject falls back to the live lookup."""
        monkeypatch.setattr(du, "_tcc_subject",
                            lambda: (_ for _ in ()).throw(AssertionError("looked up again")))
        err = du._tcc_help("Accessibility", "Accessibility", subject={
            "process": "Lucaryin AI", "subject_kind": "app", "bundle_id": "com.lucaryin.lucaryin-ai"})
        assert '"Lucaryin AI"' in err and "Privacy & Security → Accessibility" in err
        monkeypatch.setattr(du, "_tcc_subject", lambda: {
            "process": "python3.12", "subject_kind": "interpreter", "bundle_id": None})
        err = du._tcc_help("Accessibility", "Accessibility", subject={"pyobjc": True})
        assert '"python3.12" (not "Lucaryin")' in err
        err = du._tcc_help("Accessibility", "Accessibility", subject={
            "process": "sshd-keygen-wrapper", "subject_kind": "process", "bundle_id": None})
        assert ('under the process that launched the worker, the "sshd-keygen-wrapper" binary, so '
                'it appears as "sshd-keygen-wrapper" (not "Lucaryin") in System Settings → Privacy & '
                'Security → Accessibility') in err

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
        assert du._request_accessibility() is False  # the trust answer is returned, not discarded
        assert du._request_accessibility() is None   # already asked this process: no call, no answer
        assert seen == [{"AXTrustedCheckOptionPrompt": True}]

    def test_accessibility_prompt_falls_back_to_non_prompting_check(self, monkeypatch):
        seen = []
        fake = types.ModuleType("ApplicationServices")  # no *WithOptions, no option constant
        fake.AXIsProcessTrusted = lambda: seen.append("plain") or True
        monkeypatch.setitem(sys.modules, "ApplicationServices", fake)
        monkeypatch.setattr(du, "_TCC_PROMPTED", set())
        assert du._request_accessibility() is True  # the fallback's answer is returned too
        assert seen == ["plain"]

    def test_prompt_helpers_never_raise_without_pyobjc(self, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        monkeypatch.setitem(sys.modules, "ApplicationServices", None)  # import fails
        monkeypatch.setattr(du, "_TCC_PROMPTED", set())
        du._request_screen_recording()
        assert du._request_accessibility() is None

    def test_list_apps_permissions_name_the_process_to_enable(self, monkeypatch):
        """``permissions`` is ``_tcc_status()`` (which carries "process") plus the grant_under line —
        the subject is not looked up a second time here."""
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), object()))
        monkeypatch.setattr(du, "_allowlist", lambda: ["TextEdit", "Quicken"])
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": None,
            "process": "python3.12", "subject_kind": "interpreter", "bundle_id": None})
        monkeypatch.setattr(du, "_tcc_subject", lambda: {"process": "not-the-source"})
        out = du.desktop_list_apps()
        assert out["ok"] is True
        assert out["operable_apps"] == ["TextEdit"]  # financial app filtered
        perms = out["permissions"]
        assert perms["screen_recording"] is False
        assert perms["process"] == "python3.12" and perms["subject_kind"] == "interpreter"
        assert '"python3.12"' in perms["grant_under"] and "no app launched it" in perms["grant_under"]
        assert "Screen Recording" in perms["grant_under"] and "Accessibility" in perms["grant_under"]

    def test_list_apps_grant_under_names_the_app_when_it_launched_the_worker(self, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), object()))
        monkeypatch.setattr(du, "_allowlist", lambda: ["TextEdit"])
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": False,
            "process": "Lucaryin AI", "subject_kind": "app", "bundle_id": "com.lucaryin.lucaryin-ai"})
        perms = du.desktop_list_apps()["permissions"]
        assert perms["subject_kind"] == "app" and perms["bundle_id"] == "com.lucaryin.lucaryin-ai"
        assert '"Lucaryin AI"' in perms["grant_under"] and "app that launched it" in perms["grant_under"]
        assert "python" not in perms["grant_under"]
        assert "Screen Recording" in perms["grant_under"] and "Accessibility" in perms["grant_under"]

    def test_list_apps_grant_under_names_the_launching_process_when_it_is_not_an_app(self, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: (object(), object()))
        monkeypatch.setattr(du, "_allowlist", lambda: ["TextEdit"])
        monkeypatch.setattr(du, "_tcc_status", lambda: {
            "pyobjc": True, "screen_recording": False, "accessibility": False,
            "process": "bash", "subject_kind": "process", "bundle_id": None})
        perms = du.desktop_list_apps()["permissions"]
        assert perms["subject_kind"] == "process" and perms["bundle_id"] is None
        assert 'process that launched it, the "bash" binary' in perms["grant_under"]
        assert "python" not in perms["grant_under"] and "app that launched" not in perms["grant_under"]
        assert "Screen Recording" in perms["grant_under"] and "Accessibility" in perms["grant_under"]

    def test_tcc_status_carries_the_process_name(self, worker_binary, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        status = du._tcc_status()
        assert status["pyobjc"] is False and status["process"] == "python3.12"
        assert status["subject_kind"] == "interpreter" and status["bundle_id"] is None

    def test_tcc_status_carries_the_app_subject_when_the_app_launched_the_worker(
            self, worker_binary, app_spawned, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        status = du._tcc_status()
        assert status["process"] == "Lucaryin AI" and status["subject_kind"] == "app"
        assert status["bundle_id"] is None  # no Info.plist on disk in this fixture

    def test_tcc_status_carries_the_process_subject_under_a_wrapper_script(
            self, worker_binary, wrapper_spawned, monkeypatch):
        monkeypatch.setattr(du, "_pyobjc", lambda: None)
        status = du._tcc_status()
        assert status["process"] == "bash" and status["subject_kind"] == "process"
        assert status["bundle_id"] is None


# ── desktop_key: nothing model-authored reaches osascript (R2-2-07) ─────────────

@pytest.fixture
def key_ready(monkeypatch):
    """Every precondition passes; the app is "frontmost"; osascript is recorded, not run."""
    monkeypatch.setattr(du, "_require_ready", lambda app, need_input: None)
    focused, scripts = [], []
    monkeypatch.setattr(du, "_focus", lambda app, tries=4: focused.append(app) or True)
    monkeypatch.setattr(du, "_frontmost_name", lambda: "Notes")
    monkeypatch.setattr(du, "_audit", lambda entry: None)

    class _Done:
        returncode = 0
        stderr = b""

    def fake_run(argv, **_kw):
        scripts.append(argv)
        return _Done()

    monkeypatch.setattr(du.subprocess, "run", fake_run)
    return focused, scripts


class TestDesktopKeyAllowlist:
    INJECTION = 'a" & (do shell script "id > /tmp/pwned") & "'

    def test_quote_injection_is_refused_before_the_app_is_raised(self, key_ready):
        """The round-2 exploit: an approved "save the note" card whose ``keys`` closes the
        AppleScript literal. Refused, osascript never runs, the app is never focused."""
        focused, scripts = key_ready
        out = du.desktop_key(app="Notes", keys=self.INJECTION, description="save the note")
        assert out["ok"] is False and "Refused" in out["error"]
        assert scripts == [] and focused == []

    @pytest.mark.parametrize("keys", [
        'x"', "a\\", "a\nb", "a\rb", "up", "f5", "cmd+é", "cmd+", "shift", "a+b", "ab",
        "return\" & (do shell script \"id\") & \"", "cmd+s\" & \"", "\x00", "a\tb",
    ])
    def test_anything_outside_the_allowlist_is_refused(self, key_ready, keys):
        focused, scripts = key_ready
        out = du.desktop_key(app="Notes", keys=keys, description="x")
        assert out["ok"] is False, out
        assert scripts == [] and focused == []

    @pytest.mark.parametrize("keys,clause", [
        ("cmd+s", 'keystroke "s" using {command down}'),
        ("Cmd-Shift-S", 'keystroke "s" using {command down, shift down}'),
        ("return", "key code 36"),
        ("ctrl+enter", "key code 36 using {control down}"),
        ("tab", "key code 48"),
        ("esc", "key code 53"),
        ("space", "key code 49"),
        ("delete", "key code 51"),
        ("shift+a", 'keystroke "a" using {shift down}'),
        ("/", 'keystroke "/"'),
        ("'", 'keystroke "\'"'),
    ])
    def test_allowlisted_combos_build_the_exact_script(self, key_ready, keys, clause):
        focused, scripts = key_ready
        out = du.desktop_key(app="Notes", keys=keys, description="x")
        assert out["ok"] is True, out
        assert focused == ["Notes"]
        (argv,) = scripts
        assert argv[:2] == ["osascript", "-e"]
        assert argv[2] == f'tell application "System Events" to {clause}'

    def test_every_keystroke_char_is_a_single_safe_printable(self):
        """The literal that reaches ``keystroke "<x>"`` can never contain the two AppleScript
        string metacharacters, a control character or anything non-ASCII."""
        assert all(len(c) == 1 and 0x20 <= ord(c) < 0x7F for c in du._KEYSTROKE_CHARS)
        assert '"' not in du._KEYSTROKE_CHARS and "\\" not in du._KEYSTROKE_CHARS
        for bad in ('"', "\\", "\n", "\t", "é", "", "ab"):
            clause, _ = du._parse_key_combo(bad)
            assert clause is None, bad

    def test_named_keys_are_integers_never_the_model_text(self):
        for name, code in du._KEY_CODES.items():
            clause, mods = du._parse_key_combo(name)
            assert clause == f"key code {code}" and mods == []
            assert isinstance(code, int)
