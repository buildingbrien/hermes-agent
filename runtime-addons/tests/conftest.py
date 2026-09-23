"""Hermetic guard for the fork-owned addon tests (runtime-addons/tests).

Why it exists: on 2026-09-23 the addon tests were run by their SOURCE path
(``cd build/runtime && python -m pytest ../../runtime-addons/tests/...``). That
path is outside build/runtime/tests, so upstream's tests/conftest.py (the
per-test HERMES_HOME sandbox) never loaded, and a real ``cron.scheduler.tick()``
took the live ``~/.hermes/cron/.tick.lock`` and wrote 'claimed' rows into the
founder's live ``~/.hermes/cron/executions.db``. Twice, by two different agents.

This file makes every addon test hermetic however it is invoked (pytest 9.1.1,
pinned in the runtime's dev extra, anchors rootdir at the repo's pyproject.toml
for any path inside the checkout, so this conftest loads for a source-path run
from any working directory):

* by source path (``runtime-addons/tests/...``, from anywhere) — pytest loads
  it as the addon tree's conftest, and it also sandboxes HERMES_HOME at import
  (before any test module imports runtime code), the way upstream's does;
* from the materialized runtime (CI: ``build/runtime/tests/...``) —
  build-runtime.sh installs it as the runtime ROOT ``conftest.py`` (it must not
  replace upstream's tests/conftest.py) next to ``.lucaryin-addon-tests``, the
  list of addon test files it applies to. Upstream's tests are left alone.

For every addon test (autouse):

1. ``HOME`` / ``USERPROFILE`` / ``HERMES_HOME`` / ``LUCARYIN_AUTH_DIR`` point
   at fresh tmp dirs.
2. An audit hook (``sys.addaudithook``) watches file opens, sqlite connects,
   renames, removes, mkdirs, listdirs, copies and subprocess cwd while the test
   runs. Anything aimed at the REAL Hermes or Lucaryin state — ``~/.hermes``,
   ``~/.lucaryin`` (except the interpreter's own venv), and HERMES_HOME /
   LUCARYIN_AUTH_DIR as they stood before pytest started — is refused with
   PermissionError on the spot (so nothing is written) AND fails the test at
   teardown, even if the code under test swallowed the error. macOS firmlink
   (``/System/Volumes/Data/...``), case and Windows ``\\\\?\\`` spellings count.
3. Setup and teardown fail if ``Path.home()``, ``expanduser('~')``,
   ``HERMES_HOME``/``get_hermes_home()`` or ``LUCARYIN_AUTH_DIR`` resolve to
   the real home.

Not covered (documented, not policed): metadata-only calls (stat/exists have no
audit event), a tmp symlink that points into the real home, and writes
elsewhere under the real HOME (the checkout itself lives there).
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

#: Marker the guard's own tests use to find this plugin in either layout.
LUCARYIN_ADDON_GUARD = True

MANIFEST_NAME = ".lucaryin-addon-tests"
_HERE = Path(__file__).resolve().parent
#: build/runtime/conftest.py (the materialized runtime root), vs the source tree.
MATERIALIZED = (_HERE / "run_agent.py").is_file()
_CASELESS = sys.platform == "darwin" or os.name == "nt"
_FIRMLINK_PREFIX = "/System/Volumes/Data"


# ── what "the real home" is, captured before anything is redirected ──────────

def _candidate_homes() -> set:
    homes = set()
    for var in ("HOME", "USERPROFILE"):
        value = os.environ.get(var, "")
        if value:
            homes.add(value)
    if os.name == "nt" and os.environ.get("HOMEDRIVE") and os.environ.get("HOMEPATH"):
        homes.add(os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"])
    try:
        homes.add(os.path.expanduser("~"))
    except Exception:  # noqa: BLE001
        pass
    if os.name == "posix":
        try:
            import pwd
            homes.add(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception:  # noqa: BLE001
            pass
    out = set()
    for h in homes:
        if h and os.path.isabs(h):
            h = os.path.abspath(h)
            if os.path.dirname(h) != h:  # never a filesystem root
                out.add(h)
    return out


REAL_HOMES = frozenset(_candidate_homes())
_PRE_HERMES_HOME = os.environ.get("HERMES_HOME", "")
_PRE_AUTH_DIR = os.environ.get("LUCARYIN_AUTH_DIR", "")


def _norm(path: str) -> str:
    """The comparison spelling of an absolute path: abspath, the cheap textual
    aliases folded (darwin firmlink prefix, nt \\\\?\\ and \\\\?\\UNC\\), case
    folded where the filesystem is case-insensitive."""
    p = os.path.abspath(path)
    if os.name == "nt":
        if p.startswith("\\\\?\\UNC\\"):
            p = "\\\\" + p[8:]
        elif p.startswith("\\\\?\\"):
            p = p[4:]
        p = os.path.normcase(p)
    elif sys.platform == "darwin" and p.startswith(_FIRMLINK_PREFIX + "/"):
        # /System/Volumes/Data/Users/x is /Users/x (a firmlink); for a path
        # that is not firmlinked the stripped spelling matches nothing here.
        p = p[len(_FIRMLINK_PREFIX):]
    return p.casefold() if _CASELESS else p


def _spellings(path: str) -> set:
    out = set()
    for p in {path, os.path.realpath(path)}:
        out.add(_norm(p))
    return out


def _is_under(p: str, roots) -> bool:
    for r in roots:
        if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _real_state_roots() -> tuple:
    roots = set()
    for h in REAL_HOMES:
        roots |= _spellings(os.path.join(h, ".hermes"))
        roots |= _spellings(os.path.join(h, ".lucaryin"))
    tmp_roots = _spellings(tempfile.gettempdir())
    for pre in (_PRE_HERMES_HOME, _PRE_AUTH_DIR):
        if pre and os.path.isabs(os.path.expanduser(pre)):
            spelled = _spellings(os.path.expanduser(pre))
            # A pre-set HERMES_HOME in the system temp dir is somebody's sandbox,
            # not live state.
            if not any(_is_under(s, tmp_roots) for s in spelled):
                roots |= spelled
    return tuple(sorted(roots))


def _exempt_roots() -> tuple:
    """The interpreter's own tree (the local venv lives in ~/.lucaryin/venvs)."""
    roots = set()
    for p in {sys.prefix, sys.exec_prefix, sys.base_prefix, getattr(sys, "base_exec_prefix", "")}:
        if p:
            roots |= _spellings(p)
    return tuple(sorted(roots))


# ── the audit hook ────────────────────────────────────────────────────────────

# event -> indexes of its path arguments (CPython audit-events table)
_PATH_ARGS = {
    "open": (0,),
    "os.rename": (0, 1),          # os.rename and os.replace
    "os.remove": (0,),            # os.remove and os.unlink
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.utime": (0,),
    "os.truncate": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.listdir": (0,),
    "os.scandir": (0,),
    "os.chdir": (0,),
    "os.chflags": (0,),
    "os.lchflags": (0,),
    "os.setxattr": (0,),
    "os.removexattr": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.make_archive": (2,),
    "sqlite3.connect": (0,),
    "glob.glob": (0,),
    "glob.glob/2": (0, 2),
    "pathlib.Path.glob": (0,),
    "pathlib.Path.rglob": (0,),
    "tempfile.mkstemp": (0,),
    "tempfile.mkdtemp": (0,),
    "subprocess.Popen": (2,),     # cwd
}


def _as_path(value):
    if isinstance(value, int) or value is None:
        return None
    try:
        value = os.fspath(value)
    except TypeError:
        return None
    if isinstance(value, bytes):
        try:
            value = os.fsdecode(value)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(value, str) or not value or value == ":memory:":
        return None
    if value.startswith("file:"):  # sqlite URI
        value = value[5:].split("?", 1)[0]
        if value.startswith("//"):
            value = value[2:]
    return value


class AuditGuard:
    """Refuses and records file-level events aimed at ``roots`` while active."""

    _hook_installed = False
    _active: list = []          # guards currently active (normally one)
    _lock = threading.Lock()

    def __init__(self, roots, exempt=()):
        self.roots = tuple(roots)
        self.exempt = tuple(exempt)
        self.hits: list = []

    def match(self, raw) -> bool:
        path = _as_path(raw)
        if path is None:
            return False
        try:
            p = _norm(path)
        except Exception:  # noqa: BLE001
            return False
        return _is_under(p, self.roots) and not _is_under(p, self.exempt)

    @classmethod
    def _hook(cls, event, args):
        active = cls._active
        if not active or event not in _PATH_ARGS:
            return
        for guard in tuple(active):
            for i in _PATH_ARGS[event]:
                if i < len(args) and guard.match(args[i]):
                    guard.hits.append(f"{event} {args[i]!r}")
                    raise PermissionError(
                        f"hermetic addon test refused {event} on real Hermes/Lucaryin state: {args[i]!r}")

    def __enter__(self):
        with AuditGuard._lock:
            if not AuditGuard._hook_installed:
                sys.addaudithook(AuditGuard._hook)
                AuditGuard._hook_installed = True
            AuditGuard._active = AuditGuard._active + [self]
        return self

    def __exit__(self, *exc):
        with AuditGuard._lock:
            AuditGuard._active = [g for g in AuditGuard._active if g is not self]
        return False


# ── which tests the guard applies to ──────────────────────────────────────────

def _load_scope():
    """None in the source tree (every test below this file is an addon test);
    in the materialized runtime, the set of addon test files build-runtime.sh
    listed. Missing manifest there = a runtime built by an older script: fail
    the session rather than run addon tests unguarded."""
    if not MATERIALIZED:
        return None
    manifest = _HERE / MANIFEST_NAME
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(
            f"{manifest} is missing ({exc}); re-run ./build-runtime.sh so the addon "
            "tests get their hermetic guard") from exc
    return frozenset(_norm(os.path.realpath(_HERE / ln.strip())) for ln in lines
                     if ln.strip() and not ln.lstrip().startswith("#"))


ADDON_TEST_FILES = _load_scope()


def applies_to(node) -> bool:
    if ADDON_TEST_FILES is None:
        return True
    path = getattr(node, "path", None) or getattr(node, "fspath", None)
    return path is not None and _norm(os.path.realpath(str(path))) in ADDON_TEST_FILES


# ── source-tree runs: sandbox HERMES_HOME before any test module imports ──────
# Upstream's tests/conftest.py does this for build/runtime/tests; a source-path
# run never loads it. Import-time constants (cron.jobs.CRON_DIR, logging
# handlers, hermes_state.DEFAULT_DB_PATH) would otherwise freeze the real home.

def _points_at_real_state(value: str) -> bool:
    if not value:
        return True
    try:
        spelled = _spellings(os.path.expanduser(value))
    except Exception:  # noqa: BLE001
        return True
    return any(_is_under(s, _real_state_roots_at_import) for s in spelled)


_real_state_roots_at_import = tuple(
    s for h in REAL_HOMES for sub in (".hermes", ".lucaryin") for s in _spellings(os.path.join(h, sub)))

if not MATERIALIZED:
    if _points_at_real_state(os.environ.get("HERMES_HOME", "")):
        _session_home = tempfile.mkdtemp(prefix="lucaryin-addon-hermes-home-")
        os.environ["HERMES_HOME"] = _session_home
        atexit.register(shutil.rmtree, _session_home, True)
    if _points_at_real_state(os.environ.get("LUCARYIN_AUTH_DIR", "")):
        _session_auth = tempfile.mkdtemp(prefix="lucaryin-addon-auth-")
        os.environ["LUCARYIN_AUTH_DIR"] = _session_auth
        atexit.register(shutil.rmtree, _session_auth, True)
    # Upstream's subprocess-surviving marker: hermes_state_guard fails a child
    # that carries it and still resolves the production state.db.
    os.environ["HERMES_TEST_ISOLATION"] = os.environ["HERMES_HOME"]


def pytest_report_header(config):
    if ADDON_TEST_FILES is None:
        return f"lucaryin addon guard: source tree ({_HERE})"
    return f"lucaryin addon guard: {len(ADDON_TEST_FILES)} addon test files (materialized runtime)"


# ── the per-test guard ────────────────────────────────────────────────────────

def resolution_problems() -> list:
    """Anything that still RESOLVES to the real home right now."""
    problems = []
    real_homes = {_norm(h) for h in REAL_HOMES}
    for label, fn in (("Path.home()", lambda: str(Path.home())),
                      ("expanduser('~')", lambda: os.path.expanduser("~"))):
        try:
            if _norm(fn()) in real_homes:
                problems.append(f"{label} is the real home")
        except Exception:  # noqa: BLE001
            pass
    state = _real_state_roots()
    # Unset falls back under HOME (which the guard redirected): judge the
    # value each reader would actually use.
    for var, default in (("HERMES_HOME", "~/.hermes"), ("LUCARYIN_AUTH_DIR", "~/.lucaryin/auth")):
        value = os.environ.get(var, "") or default
        try:
            if any(_is_under(s, state) for s in _spellings(os.path.expanduser(value))):
                problems.append(f"{var} -> {value} is real state")
        except Exception:  # noqa: BLE001
            pass
    hc = sys.modules.get("hermes_constants")
    if hc is not None and hasattr(hc, "get_hermes_home"):
        try:
            got = str(hc.get_hermes_home())
            if any(_is_under(s, state) for s in _spellings(got)):
                problems.append(f"get_hermes_home() -> {got} is real state")
        except Exception:  # noqa: BLE001
            pass
    return problems


@pytest.fixture(autouse=True)
def _lucaryin_addon_hermetic_guard(request):
    # Materialized runtime: this runs for upstream's tests too (root conftest)
    # and must not touch them — no fixture is requested unless it applies.
    if not applies_to(request.node):
        yield
        return
    monkeypatch = request.getfixturevalue("monkeypatch")
    tmp_path_factory = request.getfixturevalue("tmp_path_factory")
    home = tmp_path_factory.mktemp("addon-home")
    # NOT home/.hermes: hermes_state's live-DB guard treats <HOME>/.hermes as the
    # production root and refuses to open a state.db there. Upstream's conftest
    # keeps HERMES_HOME outside HOME the same way.
    hermes_home = tmp_path_factory.mktemp("addon-hermes-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("LUCARYIN_AUTH_DIR", str(home / ".lucaryin" / "auth"))
    before = resolution_problems()
    if before:
        pytest.fail("addon guard could not isolate the test: " + "; ".join(before), pytrace=False)
    with AuditGuard(_real_state_roots(), _exempt_roots()) as guard:
        yield
    problems = teardown_problems(guard)
    if problems:
        pytest.fail("addon test is not hermetic:\n  " + "\n  ".join(problems), pytrace=False)


def teardown_problems(guard: AuditGuard) -> list:
    return [f"touched real state: {h}" for h in guard.hits[:10]] + resolution_problems()
