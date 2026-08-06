"""~/.local/bin must be on the agent shell's PATH.

That is where the DESKTOP APP installs the binaries it ships for the agent —
himalaya (fleet email), cloudflared (voice tunnel) — because customer machines
have no Homebrew. On 2026-08-04 a real customer's agent ran `himalaya` to read
a forwarded meeting invite and got "command not found" (exit 127): the sane-PATH
repair only fired when PATH was missing /usr/bin, so a normal-looking PATH was
never augmented.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.environments.local import _make_run_env

LOCAL_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin")


def test_normal_path_still_gets_local_bin():
    """The regression: a healthy PATH skipped the repair entirely."""
    env = _make_run_env({"PATH": "/usr/bin:/bin:/usr/sbin"})
    assert LOCAL_BIN in env["PATH"].split(":")


def test_minimal_path_gets_both_local_bin_and_system_dirs():
    env = _make_run_env({"PATH": "/bin"})
    parts = env["PATH"].split(":")
    assert LOCAL_BIN in parts
    assert "/usr/bin" in parts


def test_empty_path_gets_local_bin():
    env = _make_run_env({"PATH": ""})
    assert LOCAL_BIN in env["PATH"].split(":")


def test_no_duplicate_when_already_present():
    env = _make_run_env({"PATH": f"{LOCAL_BIN}:/usr/bin:/bin"})
    assert env["PATH"].split(":").count(LOCAL_BIN) == 1
