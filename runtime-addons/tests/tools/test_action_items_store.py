"""action_items store: cross-process lock, pid-unique temp, corrupt-vs-missing
(R2-2-10, HA4, bug hunt round 2).

Two agents committing at once lost most of what either wrote (unlocked
load->save on one file) and one raised FileNotFoundError from ``os.replace``
when the other had just renamed the shared ``.tmp``. A corrupt store was read
as "empty" and overwritten on the next add.

Bare tier: real subprocesses race the real store in a tmp dir.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import action_items_tool as ai

# The runtime the test process imported (build/runtime), not this file's own
# tree: run by its source path (runtime-addons/tests/...) the file's parents
# are runtime-addons, where tools/ has no registry.
RUNTIME = Path(ai.__file__).resolve().parents[1]
N = 40


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "shared" / "action_items.json"
    monkeypatch.setenv(ai.STORE_ENV, str(path))
    return path


def _writer(tag: str) -> str:
    return (
        "import os, sys, json\n"
        "sys.path.insert(0, %r)\n"
        "from tools import action_items_tool as ai\n"
        "errs = []\n"
        "for i in range(%d):\n"
        "    out = ai.action_items_tool({'op': 'add', 'title': f'%s item {i}', 'owner': '%s'})\n"
        "    if 'error' in out: errs.append(out['error'])\n"
        "print(json.dumps(errs))\n" % (str(RUNTIME), N, tag, tag))


def test_two_concurrent_writers_lose_nothing(store):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    procs = [subprocess.Popen([sys.executable, "-c", _writer(tag)], env=env, cwd=str(RUNTIME),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for tag in ("thoth", "ptah")]
    outs = [p.communicate(timeout=120) for p in procs]
    for p, (out, err) in zip(procs, outs):
        assert p.returncode == 0, err
        assert json.loads(out.strip().splitlines()[-1]) == [], out  # no FileNotFoundError, no errors
    items = json.loads(store.read_text())
    titles = sorted(i["title"] for i in items)
    assert len(titles) == 2 * N, (len(titles), titles[:5])
    assert titles == sorted(f"{tag} item {i}" for tag in ("thoth", "ptah") for i in range(N))
    assert not list(store.parent.glob("*.tmp"))  # no temp litter


def test_temp_file_is_unique_per_process(store, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr(ai.os, "replace", spy)
    ai.action_items_tool({"op": "add", "title": "one"})
    ai.action_items_tool({"op": "add", "title": "two"})
    assert len(seen) == 2 and seen[0] != seen[1]
    for name in seen:
        assert str(os.getpid()) in name and name != "action_items.json.tmp"


def test_missing_store_is_empty_and_writable(store):
    out = ai.action_items_tool({"op": "list"})
    assert out == {"items": [], "count": 0}
    assert "item" in ai.action_items_tool({"op": "add", "title": "call the bank"})


@pytest.mark.parametrize("garbage", ['{"not": "a list"}', "[1, 2, 3]", "{{{ broken", "\x00\x01"])
def test_corrupt_store_is_reported_and_left_untouched(store, garbage):
    store.parent.mkdir(parents=True)
    store.write_text(garbage)
    before = store.read_bytes()
    for op in ("add", "close", "list"):
        out = ai.action_items_tool({"op": op, "title": "x", "id": "x"})
        assert "error" in out and "left untouched" in out["error"], (op, out)
    assert store.read_bytes() == before


def test_empty_file_counts_as_missing(store):
    store.parent.mkdir(parents=True)
    store.write_text("")
    assert "item" in ai.action_items_tool({"op": "add", "title": "x"})


def test_close_and_reopen_still_work_under_the_lock(store):
    added = ai.action_items_tool({"op": "add", "title": "send the deck"})["item"]
    closed = ai.action_items_tool({"op": "close", "id": added["id"], "resolution": "sent"})["item"]
    assert closed["status"] == "closed" and closed["resolution"] == "sent"
    assert ai.action_items_tool({"op": "reopen", "title": "send the"})["item"]["status"] == "open"
    assert not list(store.parent.glob("*.tmp"))


# ── Windows edge cases (review, 2026-09-23) ──────────────────────────────────

def test_list_takes_the_lock_too(store, monkeypatch):
    """On Windows a reader holding the store open makes a writer's os.replace
    fail with a sharing violation: reads run under the same lock as writes."""
    import contextlib
    taken = []

    @contextlib.contextmanager
    def spy(path):
        taken.append(path)
        yield True

    monkeypatch.setattr(ai, "exclusive_lock", spy)
    ai.action_items_tool({"op": "list"})
    assert taken == [str(store) + ".lock"]


def test_the_lock_handle_is_closed_when_the_lock_cannot_be_taken(tmp_path, monkeypatch):
    from tools import lucaryin_filelock as fl
    opened = []

    def tracking_open(*a, **k):
        f = open(*a, **k)
        opened.append(f)
        return f

    class _NoLocks:
        LOCK_EX, LOCK_UN = 2, 8

        @staticmethod
        def flock(*_a):
            raise OSError("no locks on this filesystem")

    monkeypatch.setattr(fl, "open", tracking_open, raising=False)
    monkeypatch.setattr(fl, "fcntl", _NoLocks)
    with fl.exclusive_lock(str(tmp_path / "x.lock")) as held:
        assert held is False
    assert opened and all(f.closed for f in opened)


def test_replace_retries_a_sharing_violation(tmp_path, monkeypatch):
    from tools import lucaryin_filelock as fl
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("new")
    real_replace, calls = os.replace, []

    def flaky(s, d):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(s, d)

    monkeypatch.setattr(fl.os, "replace", flaky)
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    fl.replace_with_retry(str(src), str(dst))
    assert dst.read_text() == "new" and len(calls) == 3


def test_an_add_survives_a_transient_sharing_violation(store, monkeypatch):
    from tools import lucaryin_filelock as fl
    real_replace, calls = os.replace, []

    def flaky(s, d):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(s, d)

    monkeypatch.setattr(fl.os, "replace", flaky)
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    out = ai.action_items_tool({"op": "add", "title": "renew the lease"})
    assert "item" in out, out
    assert [i["title"] for i in json.loads(store.read_text())] == ["renew the lease"]
