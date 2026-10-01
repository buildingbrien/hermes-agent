"""The addon suite's hermetic guard (runtime-addons/tests/conftest.py).

On 2026-09-23 addon tests run by their source path wrote rows into the
founder's live ~/.hermes/cron/executions.db. The guard must be loaded and
applied to every addon test in BOTH layouts — by source path, and in the
materialized runtime where build-runtime.sh installs it as the root
conftest.py scoped by .lucaryin-addon-tests — must redirect the homes, and its
audit hook must refuse and record access to protected state.

The hook is exercised against a SYNTHETIC protected root under tmp_path: these
tests never touch the real home.
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest


def _guard_plugin(request):
    for plugin in request.config.pluginmanager.get_plugins():
        if getattr(plugin, "LUCARYIN_ADDON_GUARD", False) is True:
            return plugin
    pytest.fail("runtime-addons/tests/conftest.py (the addon hermetic guard) is not loaded")


def test_the_guard_is_loaded_and_applies_to_this_file(request):
    guard = _guard_plugin(request)
    assert guard.applies_to(request.node)


def test_homes_point_at_tmp_dirs_not_the_real_home(request):
    guard = _guard_plugin(request)
    real = {guard._norm(h) for h in guard.REAL_HOMES}
    assert real, "the guard found no real home to protect"
    assert guard._norm(str(Path.home())) not in real
    assert guard._norm(os.path.expanduser("~")) not in real
    for var in ("HERMES_HOME", "LUCARYIN_AUTH_DIR"):
        value = os.environ.get(var) or ""
        assert value and not any(guard._norm(value).startswith(h) for h in real), (var, value)
    assert guard.resolution_problems() == []


@pytest.fixture
def synthetic(request, tmp_path):
    """A pretend real ~/.hermes under tmp_path, and an unprotected dir beside it."""
    plugin = _guard_plugin(request)
    root = tmp_path / "pretend-real-home" / ".hermes"
    (root / "cron").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    return plugin, root, outside


def test_the_hook_refuses_writes_and_reads_of_protected_state(synthetic):
    plugin, root, outside = synthetic
    (outside / "jobs.json").write_text("{}")
    guard = plugin.AuditGuard(plugin._spellings(str(root)))
    with guard:
        with pytest.raises(PermissionError):
            sqlite3.connect(str(root / "cron" / "executions.db"))   # the 2026-09-23 write
        with pytest.raises(PermissionError):
            open(root / "cron" / ".tick.lock", "a")                  # the tick lock
        with pytest.raises(PermissionError):
            os.replace(outside / "jobs.json", root / "cron" / "jobs.json")
        with pytest.raises(PermissionError):
            (root / "config.yaml").read_text()
        with pytest.raises(PermissionError):
            os.listdir(root)
        (outside / "fine.txt").write_text("unprotected paths are untouched")
    assert not (root / "cron" / "executions.db").exists()
    assert not (root / "cron" / ".tick.lock").exists()
    assert not (root / "cron" / "jobs.json").exists()
    assert len(guard.hits) == 5, guard.hits
    # Inactive once the test is over.
    (root / "cron" / "after.txt").write_text("x")
    assert len(guard.hits) == 5


def test_a_swallowed_refusal_still_fails_the_test(synthetic, monkeypatch):
    plugin, root, _ = synthetic
    guard = plugin.AuditGuard(plugin._spellings(str(root)))
    with guard:
        try:
            with open(root / "state.db", "wb"):
                pass
        except OSError:
            pass  # code under test that eats the error
    monkeypatch.setattr(plugin, "resolution_problems", lambda: [])
    problems = plugin.teardown_problems(guard)
    assert problems and "state.db" in problems[0], problems


def test_alias_spellings_of_a_protected_path_match(synthetic):
    plugin, root, _ = synthetic
    guard = plugin.AuditGuard(plugin._spellings(str(root)))
    target = str(root / "cron" / "executions.db")
    assert guard.match(target)
    assert guard.match(os.path.join(str(root), "cron", "..", "cron", "executions.db"))
    assert not guard.match(str(root.parent / ".hermes-sibling" / "x"))  # prefix, not a component
    assert not guard.match(":memory:") and not guard.match(3)
    assert guard.match("file:" + target + "?mode=ro")
    if sys.platform == "darwin":
        real = os.path.realpath(target)
        assert guard.match("/System/Volumes/Data" + real)   # firmlink spelling
        assert guard.match(real.upper())                    # APFS is case-insensitive
    if os.name == "nt":
        assert guard.match("\\\\?\\" + target)
        assert guard.match(target.upper())


def test_the_interpreter_tree_is_exempt(synthetic):
    """The local venv lives in ~/.lucaryin/venvs: imports from it are not state."""
    plugin, root, _ = synthetic
    venv = root / "venvs" / "hermes"
    guard = plugin.AuditGuard(plugin._spellings(str(root)), plugin._spellings(str(venv)))
    assert not guard.match(str(venv / "lib" / "site.py"))
    assert guard.match(str(root / "cron" / "jobs.json"))


PROBE = '''
import os, sqlite3

def test_probe():
    home = os.environ["PROBE_STARTUP_HOME"]   # the HOME this pytest started with
    try:
        sqlite3.connect(os.path.join(home, ".hermes", "cron", "executions.db")).execute("create table t(x)")
    except PermissionError:
        pass                                   # code under test that eats the refusal
'''


def test_the_fixture_fails_a_test_that_touches_real_state_end_to_end(request, tmp_path):
    """The guard, run as a real conftest in a child pytest whose "real home" is a
    tmp dir: a test that opens that home's cron ledger is refused (nothing is
    written) and fails at teardown even though it swallowed the error."""
    import subprocess
    plugin = _guard_plugin(request)
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pyproject.toml").write_text('[project]\nname = "probe"\n')
    (proj / "tests" / "conftest.py").write_text(Path(plugin.__file__).read_text(encoding="utf-8"),
                                                encoding="utf-8")
    (proj / "tests" / "test_probe.py").write_text(PROBE)
    fake_home = tmp_path / "startup-home"
    (fake_home / ".hermes" / "cron").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "LUCARYIN_AUTH_DIR", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env.update(HOME=str(fake_home), USERPROFILE=str(fake_home), PROBE_STARTUP_HOME=str(fake_home),
               PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-m", "pytest", "tests/test_probe.py", "-q", "-p", "no:cacheprovider"],
                         cwd=proj, env=env, capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    assert out.returncode != 0, text
    assert "addon test is not hermetic" in text and "executions.db" in text, text
    assert not (fake_home / ".hermes" / "cron" / "executions.db").exists()
