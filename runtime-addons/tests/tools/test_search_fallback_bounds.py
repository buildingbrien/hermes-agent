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
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

import tools.file_operations as file_operations
import tools.file_operations_search as search_mod
from agent.search_policy import SEARCH_PRUNE_DIR_NAMES
from tools.environments.local import LocalEnvironment
from tools.file_operations import ExecuteResult, ShellFileOperations
from tools.file_operations_search import (
    _cap_output_columns, _parse_search_output, _split_tool_diagnostics)

PROTECTED = ("Desktop", "Documents", "Downloads", "Library")


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


class TestFindEnumeratedFormOnMac:
    """Local macOS: every content search is ``find <bounds> -exec grep``."""

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
        assert command.startswith(f"find {q(str(root))} ")
        assert "-flags +dataless -prune -o" in command
        assert "-type f -size -50M -exec grep -nHE -I --line-buffered 'bonini|michael' {} +" in command
        assert command.endswith("{} +")  # no shell redirect token on the native lane
        assert "--exclude-dir" not in command
        assert f"\\( -type d -name '.*' ! -path {q(str(root))} \\) -prune" in command
        for name in SEARCH_PRUNE_DIR_NAMES:
            assert f"-name {q(name)}" in command
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
        assert "Content search skipped" not in result.warning  # neither cloud root nor timeout

    def test_root_named_like_a_pruned_dir_is_exempt(self, local_env, tmp_path, monkeypatch):
        root = tmp_path / "build"
        root.mkdir()
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)

        ops.search("needle", path=str(root), target="content")

        (command, _cwd), = capture.calls
        assert "-name 'build'" in command
        assert f"! -path {ops._escape_shell_arg(str(root))} \\) -prune" in command

    def test_relative_root_is_not_pruned_as_hidden(self, local_env, tmp_path, monkeypatch):
        """``.`` matches ``-name '.*'``; the root exemption keeps it searchable."""
        ops, capture = _local_ops(local_env, monkeypatch, cwd=tmp_path)

        result = ops.search("needle", path=".", target="content")

        assert result.error is None
        (command, _cwd), = capture.calls
        assert command.startswith("find '.' ")
        assert "\\( -type d -name '.*' ! -path '.' \\) -prune" in command

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
        pipeline = next(c for c in shell if c.startswith("set -o pipefail; find "))
        assert "-flags +dataless -prune" in pipeline
        assert "-size -50M" in pipeline
        assert pipeline.endswith("{} + | head -n 50 | cut -c1-2000")


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
        for name in SEARCH_PRUNE_DIR_NAMES:
            assert f"--exclude-dir={q(name)}" in command
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

        assert capture.calls[0][0].startswith("find ")

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
        home = tmp_path / "Users" / "alice"
        home.mkdir(parents=True)
        ops, capture = _local_ops(local_env, monkeypatch, cwd=home, engines=("rg",), home=home)

        ops.search("needle", path=str(home), target="content")

        command, cwd = capture.calls[0]
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

    @pytest.mark.skipif(sys.platform != "darwin", reason="find -flags +dataless is macOS-only")
    def test_find_grep_lane_returns_what_it_found_with_limit_reason(self, local_env, tmp_path, monkeypatch):
        """A grep that stalls after its first hit: the pre-fix pipeline's group kill
        discarded that hit (block-buffered grep/head/cut) and reported 0."""
        (tmp_path / "hit.txt").write_text("needle here\n")
        local_env.cwd = str(tmp_path)
        ops = ShellFileOperations(local_env, cwd=str(tmp_path))
        monkeypatch.setattr(ops, "_has_command", lambda command: command in ("grep", "find"))
        monkeypatch.setattr(search_mod, "_CONTENT_SEARCH_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(ops, "_grep_cmd", lambda head, pattern, output_mode, context, file_glob=None: [
            "sh", "-c", "'grep -nHE -I --line-buffered needle \"$@\"; sleep 30'", "sh"])

        result = ops.search("needle", path=str(tmp_path), target="content")

        assert result.error is None
        assert [os.path.basename(m.path) for m in result.matches] == ["hit.txt"]
        assert result.limit_reason == "search_timeout"
        assert result.truncated is True
        assert "Content search skipped binary files" in result.warning


class TestFoldedFindExit:
    """find hides grep's exit 2 (and BSD find returns 1 for a no-match batch); the
    hard failure grep -r reports is recovered by shape: diagnostics, no payload."""

    @staticmethod
    def _ops(output, code):
        env = MagicMock(cwd="/repo")
        env.execute.return_value = {"output": output, "returncode": code}
        return ShellFileOperations(env)

    FIND = ["find", "'/repo'", "-type f", "-exec", "grep", "-nHE", "'['", "{}", "+"]

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
    # A shell double is not a local Mac: no -flags term, so no placeholder claim.
    assert "not downloaded" not in result.warning
    assert "-flags" not in next(c for c in env.commands if "; find " in c)


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

    def test_plain_truncation_keeps_the_offset_hint(self):
        parsed = self._search({
            "total_count": 100, "truncated": True,
            "matches": [{"path": "a.py", "line": 1, "content": "x"}] * 50}, offset=0, limit=50)
        assert "offset=50" in parsed["_hint"]


def test_cap_output_columns_matches_cut():
    assert _cap_output_columns("a" * 3000 + "\nshort\n", 2000) == "a" * 2000 + "\nshort\n"
