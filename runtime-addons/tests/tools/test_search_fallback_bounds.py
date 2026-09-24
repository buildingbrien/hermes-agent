"""Lucaryin fold (runtime-patches/0020): the no-ripgrep content-search lane is
bounded and honest, and neither lane opens cloud placeholders.

Canary soak 2026-09-13 (Intel Mac, no rg): four content searches over $HOME,
~/Documents, ~/Desktop and ~/Downloads each burned the full 60 s and came back
``total_count 0, truncated, limit_reason=search_timeout, _hint: Use offset=30 to
see more``. Root causes, all in the BSD grep/find fallback:

1. grep ran without ``-I`` (every media file read end to end), with no size cap
   and none of ``SEARCH_PRUNE_DIR_NAMES`` pruned (node_modules, venv, .Trash...);
2. one 60 s kill of the whole process group threw away the hits still sitting in
   grep's/head's block buffers — a search that matched in its first second said 0;
3. ~/Documents and ~/Desktop hold thousands of iCloud "dataless" placeholders;
   reading one makes macOS download it (the rg lane reads them too, and its
   protected-folder globs only worked when the shell's cwd was the root);
4. the offset hint on a zero-result timeout read as "no matches, page on".

Round 2 (review): the at-bound group kill hit macOS ``killpg`` EPERM on an
exiting find/grep group (every local-Mac search with more hits than the page
failed with ``[Errno 1]``); files_only hits with whitespace in the name were
reported as the failure text; merged stderr diagnostics spent the fetch bound;
an explicit ``~/Library`` still ran rg over iCloud Drive; symlinked cwd/root broke
the rg anchoring; relative roots printed ``./x``; a symlinked root listed nothing.
"""

import json
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

import tools.file_operations as file_operations
import tools.file_operations_search as search_mod
from agent.search_policy import SEARCH_PRUNE_DIR_NAMES
from tools.environments.local import LocalEnvironment
from tools.file_operations import ExecuteResult, SearchResult, ShellFileOperations
from tools.file_operations_search import (
    _CONTENT_SEARCH_PRUNE_DIR_NAMES, _USER_CONTENT_DIR_NAMES, _cap_output_columns,
    _maybe_warn_line_oriented_newline_pattern, _parse_search_output, _split_tool_diagnostics)

PROTECTED = ("Desktop", "Documents", "Downloads", "Library")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="native search lane is POSIX-only")
DARWIN_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="find -flags +dataless / BSD find lane is macOS-only")


class RecordingEnvironment:
    """Shell double (not a ``LocalEnvironment``): records commands, finds nothing."""

    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.commands = []

    def execute(self, command, cwd=None, **kwargs):
        self.commands.append(command)
        if command.startswith("test -e"):
            return {"output": "exists\n", "returncode": 0}
        return {"output": "", "returncode": 1}


class NativeCapture:
    """Stands in for ``_run_rg_native``: records ``(command, cwd)``; answers "nothing"."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, fetch_limit, timeout, merge_stderr=False, cwd=None):
        self.calls.append((" ".join(argv), cwd))
        return ExecuteResult("", 1)


@pytest.fixture(scope="module")
def local_env(tmp_path_factory):
    """One real LocalEnvironment per module (constructing one costs ~0.8 s)."""
    return LocalEnvironment(cwd=str(tmp_path_factory.mktemp("fallback-bounds")))


def _local_ops(local_env, monkeypatch, cwd, engines=("grep", "find"), platform="darwin", home=None):
    """ShellFileOperations over the real local backend with the engine set pinned,
    the controller platform pinned, and the native lane captured (no subprocess)."""
    local_env.cwd = str(cwd)
    ops = ShellFileOperations(local_env, cwd=str(cwd))
    monkeypatch.setattr(file_operations.sys, "platform", platform)
    monkeypatch.setattr(ops, "_has_command", lambda command: command in engines)
    monkeypatch.setattr(ops, "_resolve_command", lambda command: command if command in engines else None)
    if home is not None:
        monkeypatch.setattr(file_operations, "_HOME", str(home))
    capture = NativeCapture()
    monkeypatch.setattr(ops, "_run_rg_native", capture)
    return ops, capture


def _real_ops(local_env, monkeypatch, cwd, engines=("grep", "find")):
    """The real local backend with real subprocesses; only the engine set is pinned
    (the soak box now ships ripgrep, so the no-rg lane must be forced)."""
    local_env.cwd = str(cwd)
    ops = ShellFileOperations(local_env, cwd=str(cwd))
    monkeypatch.setattr(ops, "_has_command", lambda command: command in engines)
    monkeypatch.setattr(ops, "_resolve_command", lambda command: command if command in engines else None)
    return ops


class TestFindEnumeratedFormOnMac:
    """Local macOS: every content search is ``find -H <bounds> -exec grep``."""

    def test_documents_root_skips_placeholders_binaries_and_big_files(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        root = home / "Documents" / "Work"
        root.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path, home=home)

        result = ops.search("bonini|michael", path=str(root), target="content")

        assert result.error is None
        (command, cwd), = capture.calls
        q = ops._escape_shell_arg
        assert cwd is None
        assert command.startswith(f"find -H {q(str(root))} ")
        assert "-flags +dataless -prune -o" in command
        assert "-type f -size -50M -exec grep -nHE -I --line-buffered 'bonini|michael' {} +" in command
        assert command.endswith("{} +")  # no shell redirect token on the native lane
        assert "--exclude-dir" not in command
        assert f"\\( -type d -name '.*' ! -path {q(str(root))} \\) -prune" in command
        # R2-3-35: dependency/cache names are pruned; user-folder names never are.
        for name in _CONTENT_SEARCH_PRUNE_DIR_NAMES:
            assert f"-name {q(name)}" in command
        for name in _USER_CONTENT_DIR_NAMES:
            assert f"-name {q(name)}" not in command, name
        assert "cloud files not downloaded to this Mac" in (result.warning or "")

    def test_home_root_keeps_protected_prunes_and_adds_the_bounds(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        home.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, home=home)

        result = ops.search("bonini", path=str(home), target="content")

        (command, _cwd), = capture.calls
        for dirname in PROTECTED:
            assert f"-path {ops._escape_shell_arg(str(home / dirname))}" in command
        assert "-flags +dataless -prune" in command
        assert "-size -50M" in command
        assert "-I --line-buffered" in command
        assert result.warning.startswith("Skipped macOS protected folders")
        # Zero results: the prune policy is no longer silent (round 2, N4).
        assert "Content search skipped binary files" in result.warning
        assert "discarded" not in result.warning  # not a timeout

    def test_root_named_like_a_pruned_dir_is_exempt(self, local_env, tmp_path, monkeypatch):
        root = tmp_path / "node_modules"  # "build" is user content since R2-3-35
        root.mkdir()
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)

        ops.search("needle", path=str(root), target="content")

        (command, _cwd), = capture.calls
        assert "-name 'node_modules'" in command
        assert f"! -path {ops._escape_shell_arg(str(root))} \\) -prune" in command

    def test_relative_root_is_anchored_absolute_and_exempt_from_the_hidden_prune(self, local_env, tmp_path, monkeypatch):
        """``.`` matches ``-name '.*'``; natively the root is anchored to its absolute
        path (hits keep the absolute shape the grep -r lane printed) and the root
        exemption follows it."""
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)

        result = ops.search("needle", path=".", target="content")

        assert result.error is None
        (command, _cwd), = capture.calls
        q = ops._escape_shell_arg
        assert command.startswith(f"find -H {q(str(tmp_path))} ")
        assert f"\\( -type d -name '.*' ! -path {q(str(tmp_path))} \\) -prune" in command
        assert " '.' " not in command

    def test_relative_root_at_home_prunes_absolute_protected_paths(self, local_env, tmp_path, monkeypatch):
        """The protected prunes are rebuilt against the anchored root: ``-path
        'Desktop'`` never matched find's ``/Users/alice/Desktop``."""
        home = tmp_path / "Users" / "alice"
        home.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, home=home)

        ops.search("needle", path=".", target="content")

        (command, _cwd), = capture.calls
        for dirname in PROTECTED:
            assert f"-path {ops._escape_shell_arg(str(home / dirname))}" in command
        assert "-path 'Desktop'" not in command

    def test_file_glob_rides_the_find_walk(self, local_env, tmp_path, monkeypatch):
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)

        ops.search("needle", path=str(tmp_path), file_glob="*.pdf", target="content")

        (command, _cwd), = capture.calls
        assert "-size -50M -name '*.pdf' -exec grep" in command

    def test_kill_switch_keeps_the_shell_pipeline_with_the_same_bounds(self, local_env, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)
        shell = []

        def fake_exec(command, **kwargs):
            shell.append(command)
            if command.startswith("test -e"):
                return ExecuteResult("exists", 0)
            return ExecuteResult("", 1)

        monkeypatch.setattr(ops, "_exec", fake_exec)
        ops.search("needle", path=str(tmp_path), target="content")

        assert capture.calls == []
        pipeline = next(c for c in shell if c.startswith("set -o pipefail; find -H "))
        assert "-flags +dataless -prune" in pipeline
        assert "-size -50M" in pipeline
        assert pipeline.endswith("{} + | head -n 50 | cut -c1-2000")

    def test_kill_switch_timeout_says_partials_were_discarded(self, local_env, tmp_path, monkeypatch):
        """The shell pipeline cannot keep what grep found before the deadline (the
        group kill drops the block buffers): the skip note must say so (N7)."""
        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
        ops, _capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)

        def fake_exec(command, **kwargs):
            if command.startswith("test -e"):
                return ExecuteResult("exists", 0)
            if command.startswith("set -o pipefail; find -H "):
                return ExecuteResult("[Command timed out after 60s]", 124)
            return ExecuteResult("", 1)

        monkeypatch.setattr(ops, "_exec", fake_exec)
        result = ops.search("needle", path=str(tmp_path), target="content")

        assert result.limit_reason == "search_timeout"
        assert "Content search skipped binary files" in result.warning
        assert "discarded along with the shell pipeline" in result.warning
        assert "HERMES_NATIVE_FILE_READ=0" in result.warning


class TestRecursiveGrepFormElsewhere:
    """Off-macOS (or a non-local shell) keeps ``grep -r``: -I, --line-buffered and the
    prune policy as --exclude-dir (grep itself has no size cap)."""

    def test_linux_local_native_lane_uses_absolute_root_and_exclude_dirs(self, local_env, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path, platform="linux")

        ops.search("needle", path="proj", target="content")

        (command, _cwd), = capture.calls
        q = ops._escape_shell_arg
        assert command.startswith("grep -rnHE --exclude-dir='.*' ")
        assert command.endswith(f"-I --line-buffered 'needle' {q(str(root))}")
        assert "$PWD" not in command
        # R2-3-35: the content lane excludes dependency/cache trees, never the
        # plain-English folder names upstream's code-probe policy also carries.
        for name in _CONTENT_SEARCH_PRUNE_DIR_NAMES:
            assert f"--exclude-dir={q(name)}" in command
        for name in _USER_CONTENT_DIR_NAMES:
            assert f"--exclude-dir={q(name)}" not in command, name
        assert "-flags" not in command and not command.startswith("find")

    def test_remote_shell_keeps_pwd_anchor_and_skips_the_roots_own_name(self, monkeypatch):
        env = RecordingEnvironment("/srv/vendor")  # the root's basename is in the policy
        ops = ShellFileOperations(env)
        monkeypatch.setattr(file_operations.sys, "platform", "linux")
        monkeypatch.setattr(ops, "_has_command", lambda command: command == "grep")

        ops.search("needle", path=".", target="content")

        command = next(c for c in env.commands if c.startswith("set -o pipefail; grep -rnHE "))
        assert '"$PWD"' in command
        assert "--exclude-dir='vendor'" not in command
        assert "--exclude-dir='node_modules'" in command
        assert "-I --line-buffered 'needle'" in command
        assert "| head -n 50 | cut -c1-2000" in command


class TestRipgrepLane:
    def test_cloud_root_routes_to_the_find_lane_without_rg_probes(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        root = home / "Desktop"
        root.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)

        result = ops.search("needle", path=str(root), target="content")

        assert [command.split(" ", 1)[0] for command, _cwd in capture.calls] == ["find"]
        assert "-flags +dataless -prune" in capture.calls[0][0]
        assert "cloud files not downloaded to this Mac" in result.warning

    def test_cloud_root_match_is_case_insensitive_like_apfs(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        root = home / "documents" / "notes"
        root.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)

        ops.search("needle", path=str(root), target="content")

        assert capture.calls[0][0].startswith("find -H ")

    def test_library_root_above_icloud_drive_routes_to_the_find_lane(self, local_env, tmp_path, monkeypatch):
        """An explicit ``~/Library`` gets no protected-folder globs (Library IS the
        root) yet holds Mobile Documents and CloudStorage: rg would materialise the
        whole iCloud Drive. Any cloud dir below an unexcluded root takes the find
        lane (N3)."""
        home = tmp_path / "Users" / "alice"
        root = home / "Library"
        (root / "Mobile Documents").mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)

        result = ops.search("needle", path=str(root), target="content")

        assert [command.split(" ", 1)[0] for command, _cwd in capture.calls] == ["find"]
        assert "-flags +dataless -prune" in capture.calls[0][0]
        assert "cloud files not downloaded to this Mac" in result.warning

    def test_library_subtree_without_cloud_dirs_stays_on_rg(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        root = home / "Library" / "Caches"
        root.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)

        ops.search("needle", path=str(root), target="content")

        assert capture.calls[0][0].startswith("rg --line-number")

    def test_non_cloud_root_stays_on_rg(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        root = home / "code"
        root.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)

        ops.search("needle", path=str(root), target="content")

        command, cwd = capture.calls[0]
        assert command.startswith("rg --line-number")
        assert cwd is None

    def test_broad_root_runs_rg_inside_the_root_so_exclusions_bite(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        (home / "work").mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home / "work", engines=("rg",), home=home)

        ops.search("needle", path=str(home), target="content")

        command, cwd = capture.calls[0]
        assert command.startswith("rg --line-number")
        assert "--glob '!Documents/**'" in command
        assert cwd == str(home)
        assert command.endswith(f" {ops._escape_native_tool_arg(str(home))}")
        probes = capture.calls[1:]  # the zero-match probes re-walk the same root
        assert probes
        for probe_command, probe_cwd in probes:
            assert "--count-matches" in probe_command
            assert "--glob '!Documents/**'" in probe_command
            assert probe_cwd == str(home)

    def test_cwd_already_at_the_root_leaves_the_command_alone(self, local_env, tmp_path, monkeypatch):
        """The home root excludes every cloud dir through its protected globs, so rg
        stays the engine and needs no re-anchoring from its own cwd."""
        home = tmp_path / "Users" / "alice"
        home.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)

        ops.search("needle", path=str(home), target="content")

        command, cwd = capture.calls[0]
        assert command.startswith("rg --line-number")
        assert cwd is None
        assert "--glob '!Documents/**'" in command

    def test_file_search_single_root_gets_the_same_anchoring(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        (home / "work").mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home / "work", engines=("rg",), home=home)

        ops.search("*.txt", path=str(home), target="files")

        (command, cwd), = capture.calls
        assert "--files" in command
        assert "'!Downloads/**'" in command
        assert cwd == str(home)

    @POSIX_ONLY
    def test_symlinked_root_and_cwd_are_anchored_by_realpath(self, local_env, tmp_path, monkeypatch):
        """rg's own getcwd() is canonical: a symlinked cwd or root (/tmp vs
        /private/tmp) made its root-prefix strip miss and the globs silently fail.
        Both sides compare — and rg is handed — the real path (N1)."""
        real_home = tmp_path / "real" / "Users" / "alice"
        (real_home / "work").mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        home = tmp_path / "link" / "Users" / "alice"  # the symlinked spelling everywhere
        ops, _capture = _local_ops(local_env, monkeypatch, cwd=home / "work", engines=("rg",), home=home)

        assert ops._rg_run_cwd(str(home)) == str(real_home)
        # cwd IS the root, but spelled through the symlink: re-anchor to the real path.
        local_env.cwd = str(home)
        assert ops._rg_run_cwd(str(home)) == str(real_home)
        # cwd is the root and the root is relative (rg builds relative paths): nothing to do.
        assert ops._rg_run_cwd(".") is None
        # cwd is the root, spelled canonically: nothing to do.
        local_env.cwd = str(real_home)
        assert ops._rg_run_cwd(str(real_home)) is None


class TestPartialResultsSurviveTimeout:
    def test_native_drain_keeps_hits_printed_before_the_deadline(self, local_env, tmp_path):
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))

        result = ops._run_rg_native(
            ["sh", "-c", "'printf \"/x/a.txt:1:needle\\n\"; sleep 30'"], 50, timeout=1)

        assert result.exit_code == 124
        assert "/x/a.txt:1:needle" in result.stdout
        parsed = _parse_search_output(result, "content", 50, 0, 0)
        assert [m.path for m in parsed.matches] == ["/x/a.txt"]
        assert parsed.limit_reason == "search_timeout"
        assert parsed.truncated is True
        assert parsed.error is None

    @DARWIN_ONLY
    def test_find_grep_lane_returns_what_it_found_with_limit_reason(self, local_env, tmp_path, monkeypatch):
        """A grep that stalls after its first hit: the pre-fix pipeline's group kill
        discarded that hit (block-buffered grep/head/cut) and reported 0."""
        (tmp_path / "hit.txt").write_text("needle here\n")
        ops = _real_ops(local_env, monkeypatch, cwd=tmp_path)
        monkeypatch.setattr(search_mod, "_CONTENT_SEARCH_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(ops, "_grep_cmd", lambda head, pattern, output_mode, context, file_glob=None: [
            "sh", "-c", "'grep -nHE -I --line-buffered needle \"$@\"; sleep 30'", "sh"])

        result = ops.search("needle", path=str(tmp_path), target="content")

        assert result.error is None
        assert [os.path.basename(m.path) for m in result.matches] == ["hit.txt"]
        assert result.limit_reason == "search_timeout"
        assert result.truncated is True
        assert "Content search skipped binary files" in result.warning
        assert "discarded" not in result.warning  # the native drain kept them


class TestDiagnosticsNeverSpendTheBound:
    """Merged stderr rides the native drain's pipe: a protected tree's worth of
    "find: …: Permission denied" lines used to exhaust fetch_limit before the first
    hit and, through exec_folded, surface as a hard error though hits followed (N2)."""

    @POSIX_ONLY
    def test_diagnostics_before_the_hits_do_not_exhaust_fetch_limit(self, local_env, tmp_path):
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))
        script = ("'for i in 1 2 3 4 5 6 7 8; do echo \"find: /locked/$i: Permission denied\"; done; "
                  "printf \"/x/a.txt:1:needle\\n/x/b.txt:1:needle\\n/x/c.txt:1:needle\\n\"'")

        result = ops._run_rg_native(["sh", "-c", script], 2, timeout=10, merge_stderr=True)

        assert result.exit_code == 0  # bounded after the 2nd HIT, not the 2nd line
        hits = [line for line in result.stdout.splitlines() if line.startswith("/x/")]
        assert hits == ["/x/a.txt:1:needle", "/x/b.txt:1:needle"]
        assert result.stdout.count("Permission denied") == 8  # kept for the error text
        parsed = _parse_search_output(result, "content", 2, 0, 0)
        assert parsed.error is None
        assert [m.path for m in parsed.matches] == ["/x/a.txt", "/x/b.txt"]

    @POSIX_ONLY
    def test_retained_diagnostics_are_capped(self, local_env, tmp_path):
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))
        script = ("'i=0; while [ $i -lt 500 ]; do echo \"grep: /locked/$i: Permission denied\"; "
                  "i=$((i+1)); done; printf \"/x/a.txt:1:needle\\n\"'")

        result = ops._run_rg_native(["sh", "-c", script], 5, timeout=10, merge_stderr=True)

        assert "/x/a.txt:1:needle" in result.stdout
        assert result.stdout.count("Permission denied") == search_mod._MAX_RETAINED_DIAGNOSTIC_LINES

    def test_discarded_stderr_lane_is_untouched(self, local_env, tmp_path):
        """Without merge_stderr nothing on the pipe is a diagnostic — a hit whose
        text happens to start like one still counts toward the bound."""
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))

        result = ops._run_rg_native(["sh", "-c", "'printf \"find: x\\nfind: y\\nfind: z\\n\"'"], 2, timeout=10)

        assert len(result.stdout.splitlines()) == 2


@DARWIN_ONLY
class TestBoundedKillOnMac:
    """Blocker A: the at-bound kill used upstream's group teardown, which tolerates
    only ProcessLookupError. macOS ``killpg`` answers EPERM once the group's members
    are exiting — the normal state of ``find -exec grep`` whose whole output fit
    the pipe buffer — so every local-Mac content search with more hits than the
    page raised ``[Errno 1] Operation not permitted`` (5/5 on the canary)."""

    def test_more_hits_than_the_page_returns_the_page(self, local_env, tmp_path, monkeypatch):
        src = tmp_path / "proj" / "src"
        src.mkdir(parents=True)
        for i in range(120):
            (src / f"mod{i:03d}.py").write_text("import os\nimport sys\nx = 1\n")
        ops = _real_ops(local_env, monkeypatch, cwd=tmp_path / "proj")

        for _attempt in range(5):  # the EPERM race needs the engines mid-exit
            result = ops.search("import", path=".", target="content")
            assert result.error is None
            assert len(result.matches) == 50
            assert result.total_count == 50
            assert all(m.path.startswith(str(src)) for m in result.matches)  # absolute (N12)

    def test_native_find_exec_grep_bounded_on_a_readable_tree(self, local_env, tmp_path):
        """The drain reaches its bound (50 of 240 lines) after grep has flushed
        everything and find/grep are mid-exit — the state in which killpg answers
        EPERM. A tiny bound reached while grep is still writing does not race."""
        for i in range(120):
            (tmp_path / f"f{i:03d}.txt").write_text("needle\nneedle\n")
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))
        q = ops._escape_shell_arg

        for _attempt in range(5):
            result = ops._run_rg_native(
                ["find", "-H", q(str(tmp_path)), "-type f", "-exec", "grep", "-nHE", "-I",
                 "--line-buffered", "'needle'", "{}", "+"], 50, timeout=30, merge_stderr=True)
            assert result.exit_code == 0
            assert len(result.stdout.splitlines()) == 50

    def test_group_kill_permission_error_degrades_to_the_leader(self, local_env, tmp_path, monkeypatch):
        """Deterministic form of the race: the group kill raises EPERM, the leader
        is still terminated and the drain returns its page."""
        import tools.environments.local as local_mod

        def eperm(proc):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(local_mod, "_kill_process_group_posix", eperm)
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))

        result = ops._run_rg_native(["sh", "-c", "'yes needle'"], 3, timeout=10, merge_stderr=True)

        assert result.exit_code == 0
        assert result.stdout.splitlines() == ["needle"] * 3


class TestFoldedFindExit:
    """find hides grep's exit 2 (and BSD find returns 1 for a no-match batch); the
    hard failure grep -r reports is recovered by shape: an engine-prefixed
    diagnostic and no payload at all."""

    @staticmethod
    def _ops(output, code):
        env = MagicMock(cwd="/repo")
        env.execute.return_value = {"output": output, "returncode": code}
        return ShellFileOperations(env)

    FIND = ["find", "-H", "'/repo'", "-type f", "-exec", "grep", "-nHE", "'['", "{}", "+"]
    FIND_L = ["find", "-H", "'/repo'", "-type f", "-exec", "grep", "-nHE", "-l", "'needle'", "{}", "+"]

    def test_bad_regex_diagnostic_without_payload_is_a_hard_failure(self):
        ops = self._ops("grep: brackets ([ ]) not balanced\n", 1)
        result = ops._run_search_pipeline(self.FIND, "content", 50, 0, 0, line_cap=True, exec_folded=True)
        assert result.error == "Search failed: grep: brackets ([ ]) not balanced"

    def test_partial_permission_error_keeps_matches(self):
        ops = self._ops("grep: /repo/locked.txt: Permission denied\n/repo/a.txt:1:needle\n", 1)
        result = ops._run_search_pipeline(self.FIND, "content", 50, 0, 0, line_cap=True, exec_folded=True)
        assert result.error is None
        assert [m.path for m in result.matches] == ["/repo/a.txt"]

    def test_timeout_marker_is_not_mistaken_for_a_failure(self):
        ops = self._ops("[Command timed out after 60s]", 124)
        result = ops._run_search_pipeline(self.FIND, "content", 50, 0, 0, line_cap=True, exec_folded=True)
        assert result.error is None
        assert result.limit_reason == "search_timeout"
        assert result.total_count == 0

    def test_find_permission_line_is_a_diagnostic_even_with_dash_digits(self):
        diagnostics, payload = _split_tool_diagnostics("find: /Users/x/file-2: Permission denied\n")
        assert payload == ""
        assert diagnostics.startswith("find: ")

    def test_files_only_hits_with_whitespace_are_files_not_a_failure(self):
        """Blocker B: ``grep -l`` paths with spaces fail the whitespace-free payload
        shape; they were reported as ``Search failed: <the matching paths>``. With
        the root known they are hits, and only an engine-prefixed line may promote."""
        ops = self._ops("/repo/My File.txt\n/repo/Bonini Contract.txt\n", 0)
        result = ops._run_search_pipeline(self.FIND_L, "files_only", 50, 0, 0,
                                          line_cap=True, exec_folded=True, payload_root="/repo")
        assert result.error is None
        assert result.files == ["/repo/My File.txt", "/repo/Bonini Contract.txt"]
        assert result.total_count == 2

    def test_unprefixed_noise_without_payload_is_an_empty_result_not_a_failure(self):
        ops = self._ops("something odd on stdout\n", 1)
        result = ops._run_search_pipeline(self.FIND, "content", 50, 0, 0, line_cap=True, exec_folded=True)
        assert result.error is None
        assert result.total_count == 0

    def test_prefixed_diagnostic_beside_a_space_named_hit_keeps_the_hit(self):
        ops = self._ops("find: /repo/locked: Permission denied\n/repo/My File.txt\n", 1)
        result = ops._run_search_pipeline(self.FIND_L, "files_only", 50, 0, 0,
                                          line_cap=True, exec_folded=True, payload_root="/repo")
        assert result.error is None
        assert result.files == ["/repo/My File.txt"]


class TestRootAnchoredPayload:
    def test_root_anchored_lines_are_payload_but_engine_lines_never_are(self):
        out = ("rg: /repo/locked: Permission denied (os error 13)\n"
               "grep: /repo/other: Permission denied\n"
               "/repo/My File.txt\n"
               "/repo/plain.txt\n"
               "error: unclosed group\n")
        diagnostics, payload = _split_tool_diagnostics(out, payload_root="/repo")
        assert payload.splitlines() == ["/repo/My File.txt", "/repo/plain.txt"]
        assert diagnostics.splitlines() == [
            "rg: /repo/locked: Permission denied (os error 13)",
            "grep: /repo/other: Permission denied",
            "error: unclosed group"]

    def test_relative_root_anchors_dot_slash_hits(self):
        _diagnostics, payload = _split_tool_diagnostics("./My File.txt\n./a b/c.txt\n", payload_root=".")
        assert payload.splitlines() == ["./My File.txt", "./a b/c.txt"]

    def test_unknown_root_keeps_the_shape_rule(self):
        diagnostics, payload = _split_tool_diagnostics("/repo/My File.txt\n")
        assert payload == "" and diagnostics == "/repo/My File.txt"

    @POSIX_ONLY
    def test_space_named_files_come_back_through_search(self, local_env, monkeypatch):
        """Real subprocesses: the find lane (macOS) or the grep -r lane (elsewhere).
        Not under pytest's tmp_path on purpose: its ``pytest-<N>`` segment carries a
        ``-<digit>`` that satisfies the payload shape by accident and hid the bug."""
        import re
        import shutil
        import tempfile
        root = tempfile.mkdtemp(prefix="space.names.")
        if re.search(r"[:\-]\d", root):
            shutil.rmtree(root)
            pytest.skip("temp root would satisfy the payload shape by accident")
        try:
            for name, text in (("My File.txt", "needle\n"), ("Bonini Contract.txt", "the needle clause\n"),
                               ("plain.txt", "needle\n")):
                with open(os.path.join(root, name), "w") as handle:
                    handle.write(text)
            ops = _real_ops(local_env, monkeypatch, cwd=root)

            result = ops.search("needle", path=root, target="content", output_mode="files_only")

            assert result.error is None
            assert sorted(os.path.basename(f) for f in result.files) == ["Bonini Contract.txt", "My File.txt", "plain.txt"]
        finally:
            shutil.rmtree(root, ignore_errors=True)


@POSIX_ONLY
class TestFindLaneRealSubprocess:
    @DARWIN_ONLY
    def test_relative_root_hits_are_absolute(self, local_env, tmp_path, monkeypatch):
        """N12: the old grep -r lane printed absolute paths for ``.``; the find lane
        printed ``./x`` until the root was anchored."""
        (tmp_path / "hit.txt").write_text("needle\n")
        ops = _real_ops(local_env, monkeypatch, cwd=tmp_path)

        result = ops.search("needle", path=".", target="content")

        assert result.error is None
        assert [m.path for m in result.matches] == [str(tmp_path / "hit.txt")]

    def test_symlinked_root_is_followed(self, local_env, tmp_path, monkeypatch):
        """N11: ``find <symlink>`` lists nothing below it; ``find -H`` follows the root."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "hit.txt").write_text("needle\n")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        ops = _real_ops(local_env, monkeypatch, cwd=tmp_path)

        result = ops.search("needle", path=str(link), target="content")

        assert result.error is None
        assert [os.path.basename(m.path) for m in result.matches] == ["hit.txt"]

    @DARWIN_ONLY
    def test_zero_results_carry_the_skip_note(self, local_env, tmp_path, monkeypatch):
        """N4: the prune policy is applied on every walk; 0 results say what was skipped."""
        (tmp_path / "other.txt").write_text("nothing here\n")
        ops = _real_ops(local_env, monkeypatch, cwd=tmp_path)

        result = ops.search("zzz_absent_zzz", path=str(tmp_path), target="content")

        assert result.error is None and result.total_count == 0
        assert "Content search skipped binary files" in result.warning
        assert "node_modules" in result.warning


def test_protected_warning_keeps_the_engine_note(tmp_path, monkeypatch):
    """``search()`` used to overwrite the engine's warning with the protected-folder
    note; a timed-out broad search must carry both."""
    home = tmp_path / "Users" / "alice"
    home.mkdir(parents=True)
    env = RecordingEnvironment(home)

    def execute(command, cwd=None, **kwargs):
        env.commands.append(command)
        if command.startswith("test -e"):
            return {"output": "exists\n", "returncode": 0}
        if command.startswith("set -o pipefail; find "):
            return {"output": "[Command timed out after 60s]", "returncode": 124}
        return {"output": "", "returncode": 1}

    env.execute = execute
    ops = ShellFileOperations(env)
    monkeypatch.setattr(file_operations, "_HOME", str(home))
    monkeypatch.setattr(file_operations.sys, "platform", "darwin")
    monkeypatch.setattr(ops, "_has_command", lambda command: command == "grep")

    result = ops.search("bonini", path=str(home), target="content")

    assert result.limit_reason == "search_timeout"
    assert result.total_count == 0
    assert result.warning.startswith("Skipped macOS protected folders")
    assert "Content search skipped binary files" in result.warning
    # A shell double runs the shell pipeline: partials are lost and the note says so.
    assert "discarded along with the shell pipeline" in result.warning
    # A shell double is not a local Mac: no -flags term, so no placeholder claim.
    assert "not downloaded" not in result.warning
    assert "-flags" not in next(c for c in env.commands if "; find " in c)


def test_newline_note_keeps_the_lanes_skip_note():
    result = SearchResult(total_count=0, warning="Content search skipped binary files.")
    out = _maybe_warn_line_oriented_newline_pattern(result, r"needle\n")
    assert out.warning.startswith("0 results found. Note: search_files content search is line-oriented")
    assert out.warning.endswith("Content search skipped binary files.")


class TestMultiPathMerge:
    """N6: a sub-search's limit_reason and warning survive the merge — the multi-path
    note used to overwrite the warning and drop the timeout, so a timed-out root read
    as fully searched."""

    def test_limit_reason_and_engine_notes_are_carried(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        ops = ShellFileOperations(RecordingEnvironment(tmp_path))
        monkeypatch.setattr(file_operations.sys, "platform", "linux")
        subs = {
            str(a): SearchResult(matches=[search_mod.SearchMatch(path=str(a / "x.txt"), line_number=1, content="needle")],
                                 total_count=1, truncated=True, limit_reason="search_timeout",
                                 warning="Content search skipped binary files."),
            str(b): SearchResult(total_count=0, warning="0 exact matches, but 2 case-insensitive match(es)."),
        }
        monkeypatch.setattr(ops, "_search_content", lambda pattern, root, *args: subs[root])

        merged = ops._try_multi_path_search("needle", f"{a} {b}", "content", None, 50, 0, "content", 0)

        assert merged.limit_reason == "search_timeout"
        assert merged.truncated is True
        assert merged.total_count == 1
        assert merged.warning.startswith("path contained 2 entries; searched 2 that exist")
        assert "Content search skipped binary files." in merged.warning
        assert "case-insensitive" in merged.warning

    def test_identical_notes_are_not_repeated(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        ops = ShellFileOperations(RecordingEnvironment(tmp_path))
        monkeypatch.setattr(file_operations.sys, "platform", "linux")
        monkeypatch.setattr(ops, "_search_content", lambda *args: SearchResult(
            total_count=0, warning="Content search skipped binary files."))

        merged = ops._try_multi_path_search("needle", f"{a} {b}", "content", None, 50, 0, "content", 0)

        assert merged.limit_reason is None
        assert merged.warning.count("Content search skipped binary files.") == 1


class TestHonestTimeoutHint:
    def setup_method(self):
        from tools.file_tools_read_tracking import _read_tracker
        _read_tracker.clear()

    @staticmethod
    def _search(payload, **kwargs):
        with patch("tools.file_tools._get_file_ops") as mock_get:
            result_obj = MagicMock()
            result_obj.to_dict.return_value = payload
            mock_ops = MagicMock()
            mock_ops.search.return_value = result_obj
            mock_get.return_value = mock_ops
            from tools.file_tools import search_tool
            return json.loads(search_tool(pattern="bonini", **kwargs))

    def test_timeout_with_nothing_found_says_so_and_drops_the_offset_hint(self):
        parsed = self._search({
            "total_count": 0, "truncated": True, "total_count_is_lower_bound": True,
            "limit_reason": "search_timeout",
            "warning": "Content search skipped binary files, files over 50 MB."})
        hint = parsed["_hint"]
        assert "timed out" in hint
        assert "offset=" not in hint
        assert "NOT that nothing matches" in hint
        assert "'warning'" in hint
        assert "target='files'" in hint and "file_glob" in hint

    def test_timeout_with_partial_results_is_marked_partial(self):
        parsed = self._search({
            "total_count": 3, "truncated": True, "limit_reason": "search_timeout",
            "matches": [{"path": "a.py", "line": 1, "content": "x"}] * 3})
        hint = parsed["_hint"]
        assert "partial" in hint
        assert "offset=" not in hint

    def test_files_target_gets_filename_advice(self):
        """N5: a filename search reads nothing — no binary/size-cap/file_glob advice,
        and no recommendation to switch to target='files' from a files search."""
        parsed = self._search({
            "total_count": 0, "truncated": True, "limit_reason": "search_timeout", "files": []},
            target="files")
        hint = parsed["_hint"]
        assert hint.startswith("File search timed out")
        assert "NOT that nothing matches" in hint
        assert "name pattern" in hint
        assert "target='files'" not in hint
        assert "binary files" not in hint and "file_glob" not in hint

    def test_plain_truncation_keeps_the_offset_hint(self):
        parsed = self._search({
            "total_count": 100, "truncated": True,
            "matches": [{"path": "a.py", "line": 1, "content": "x"}] * 50}, offset=0, limit=50)
        assert "offset=50" in parsed["_hint"]


def test_cap_output_columns_matches_cut():
    assert _cap_output_columns("a" * 3000 + "\nshort\n", 2000) == "a" * 2000 + "\nshort\n"


# ── R2-3-35: user folders named like build output are still searched ─────────

class TestContentPrunePolicyKeepsUserFolders:
    """Bug hunt round 2: the find/grep content lane applied the whole code-probe
    policy, so documents in folders named backup, backups, out, dist, build,
    target, vendor or coverage silently vanished from every user search."""

    def test_user_content_names_are_carved_out_of_upstreams_policy(self):
        assert _USER_CONTENT_DIR_NAMES == {"backup", "backups", "build", "coverage", "dist",
                                           "out", "target", "vendor"}
        assert _USER_CONTENT_DIR_NAMES <= SEARCH_PRUNE_DIR_NAMES
        assert _CONTENT_SEARCH_PRUNE_DIR_NAMES == SEARCH_PRUNE_DIR_NAMES - _USER_CONTENT_DIR_NAMES
        assert {"node_modules", "venv", ".git", "__pycache__", ".Trash", "site-packages"} <= _CONTENT_SEARCH_PRUNE_DIR_NAMES

    def test_find_lane_prunes_dependency_trees_only(self, local_env, tmp_path, monkeypatch):
        root = tmp_path / "Users" / "alice" / "Documents" / "Taxes"
        root.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path, home=tmp_path / "Users" / "alice")
        ops.search("needle", path=str(root), target="content")
        (command, _cwd), = capture.calls
        assert command.startswith("find -H ")
        assert "-name 'node_modules'" in command and "-name '.Trash'" in command
        for name in _USER_CONTENT_DIR_NAMES:
            assert f"-name '{name}'" not in command, name

    def test_grep_lane_note_no_longer_claims_build_and_backup_are_skipped(self, local_env, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        ops, _capture = _local_ops(local_env, monkeypatch, cwd=tmp_path, home=tmp_path)
        result = ops.search("needle", path=str(root), target="content")
        note = result.warning or ""
        assert "dependency/cache directories" in note
        assert "build" not in note and "backup" not in note

    @POSIX_ONLY
    def test_documents_in_a_backup_folder_are_found_and_node_modules_are_not(self, local_env, tmp_path, monkeypatch):
        """Real find/grep (macOS) or grep -r (elsewhere): a hit under backup/ is
        returned; the dependency tree next to it stays pruned."""
        root = tmp_path / "docs"
        for sub in ("backup", "out", "vendor", "node_modules", ".git"):
            (root / sub).mkdir(parents=True)
            (root / sub / "notes.txt").write_text("the needle is here\n")
        (root / "top.txt").write_text("needle at the top\n")
        ops = _real_ops(local_env, monkeypatch, cwd=root)
        result = ops.search("needle", path=str(root), target="content")
        assert result.error is None, result.error
        hit_dirs = {pathlib.Path(m.path).parent.name for m in result.matches}
        assert {"backup", "out", "vendor", "docs"} <= hit_dirs, hit_dirs
        assert "node_modules" not in hit_dirs and ".git" not in hit_dirs, hit_dirs


# ── R2-3-26: a symlinked cloud root is classified by its REAL path ───────────

class TestCloudRootIsClassifiedByRealPath:
    def _home_with_icloud_link(self, tmp_path):
        home = tmp_path / "Users" / "alice"
        drive = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        drive.mkdir(parents=True)
        (home / "iCloud").symlink_to(drive, target_is_directory=True)
        return home

    def test_symlinked_icloud_root_takes_the_find_lane(self, local_env, tmp_path, monkeypatch):
        """``~/iCloud -> ~/Library/Mobile Documents/com~apple~CloudDocs``: the raw
        spelling is not under a cloud dir, the real one is. Before the fix rg walked
        straight into iCloud Drive and materialised every placeholder."""
        home = self._home_with_icloud_link(tmp_path)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)
        result = ops.search("needle", path=str(home / "iCloud"), target="content")
        assert [c.split(" ", 1)[0] for c, _cwd in capture.calls] == ["find"], capture.calls
        assert "-flags +dataless -prune" in capture.calls[0][0]
        assert "cloud files not downloaded to this Mac" in (result.warning or "")

    def test_symlinked_subfolder_of_icloud_takes_the_find_lane(self, local_env, tmp_path, monkeypatch):
        home = self._home_with_icloud_link(tmp_path)
        (home / "iCloud" / "Projects").mkdir()
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)
        ops.search("needle", path=str(home / "iCloud" / "Projects"), target="content")
        assert capture.calls[0][0].startswith("find -H "), capture.calls

    def test_home_spelled_through_a_symlink_still_classifies_documents(self, local_env, tmp_path, monkeypatch):
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        home = tmp_path / "link" / "Users" / "alice"
        (home / "Documents").mkdir(parents=True)
        real_home = tmp_path / "real" / "Users" / "alice"
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)
        ops.search("needle", path=str(real_home / "Documents"), target="content")
        assert capture.calls[0][0].startswith("find -H "), capture.calls

    def test_plain_symlink_to_an_ordinary_folder_stays_on_rg(self, local_env, tmp_path, monkeypatch):
        home = tmp_path / "Users" / "alice"
        (home / "Projects").mkdir(parents=True)
        (home / "work").symlink_to(home / "Projects", target_is_directory=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg", "grep", "find"), home=home)
        ops.search("needle", path=str(home / "work"), target="content")
        assert capture.calls[0][0].startswith("rg "), capture.calls


def test_timeout_hint_no_longer_claims_build_folders_are_skipped():
    """R2-3-35 follow-up (review, 2026-09-23): content search stopped pruning
    user folders named build/backup/out/dist/vendor, and filename search never
    pruned by name — the timeout hint still said "dependency/build/cache
    directories" were never searched."""
    from tools.file_tools import _search_timeout_hint
    content = _search_timeout_hint({}, "content")
    files = _search_timeout_hint({}, "files")
    for hint in (content, files):
        assert "dependency/build/cache" not in hint, hint
    assert "node_modules" in content and "backup/build" in content
    assert "hidden directories" in files and "dependency" not in files
