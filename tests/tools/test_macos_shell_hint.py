"""macOS shell guidance must reach the agent.

Two real failures from customer/founder transcripts, both exiting 0 so the
agent reported success:
  - unquoted 'Brand Assets/Logos & Marketing/Marks' fragmented into words,
    producing "Marketing: command not found" and a no-op rmdir
  - `free` was run on macOS ("free: command not found")
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.prompt_builder import build_environment_hints, MACOS_SHELL_HINT


def test_hint_is_emitted_on_macos():
    with patch("agent.prompt_builder.sys") as fake_sys:
        fake_sys.platform = "darwin"
        with patch("agent.prompt_builder.is_wsl", return_value=False):
            assert MACOS_SHELL_HINT in build_environment_hints()


def test_no_macos_hint_on_linux():
    with patch("agent.prompt_builder.sys") as fake_sys:
        fake_sys.platform = "linux"
        with patch("agent.prompt_builder.is_wsl", return_value=False):
            assert build_environment_hints() == ""


def test_hint_covers_both_real_failures():
    # quoting paths with spaces/ampersands
    assert "Quote every path" in MACOS_SHELL_HINT
    assert "&" in MACOS_SHELL_HINT or "ampersand" in MACOS_SHELL_HINT
    # linux-only tools that do not exist here
    assert "free" in MACOS_SHELL_HINT
    assert "vm_stat" in MACOS_SHELL_HINT


def test_hint_warns_that_exit_zero_can_still_be_failure():
    """Both real failures exited 0 — that is why they were reported as wins."""
    assert "exits 0" in MACOS_SHELL_HINT or "exit code" in MACOS_SHELL_HINT
