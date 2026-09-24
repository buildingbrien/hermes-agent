"""Lucaryin runtime deny layer (patches 0008 + 0009; HA3, bug hunt round 2, 2026-09-22).

0008 — agent/file_safety: the deny SET is profile-wide. A Lucaryin Mac runs one
runtime per agent (thoth, neith, ptah, set, motion) as the same OS user, each in
``<root>/profiles/<name>``. Upstream guarded only the ACTIVE home and the root,
so one agent's file tools could read a sibling's auth.json / auth/google_oauth.json
/ mcp-tokens / browser-profile / vault and rewrite its state.db and sessions with
no deny and no card (R2-3-13). Also new: ``cron/`` (jobs.json + tmp/backup
siblings — the standing-grant and trust record, R2-1-14) is write-denied in
every profile dir, and ``~/.lucaryin/approved`` joins ``~/.lucaryin/approvals``.

Review follow-up (2026-09-23): on macOS / Windows every one of these denies was
one letter-case change away from a bypass (``~/.hermes/CRON/jobs.json``,
``profiles/NEITH/auth.json``, ``~/.lucaryin/Approved/``); the bridge bearer file
(``~/.lucaryin/auth/bridge.token``, B1) was not denied at all; and a sibling's
config.yaml / state.db / sessions were still readable.

0009 — tools/browser_cdp_tool: the session-material denylist matched CDP method
NAMES only; ``Runtime.evaluate("document.cookie")`` walked straight past it
(R2-2-05). Every CDP method in a session-material domain is now refused by
prefix, and session-material JS anywhere in ANY method's params (the review
found eight methods that execute a params string) — see
test_session_material_contract.py for the shared definition.

Verifier follow-up (2026-09-23): text is not identity — the macOS firmlink
spelling (``/System/Volumes/Data/Users/...``), hardlinks and the Windows
``\\\\?\\`` / admin-share / 8.3 spellings reached every deny. 0008 now also
compares by file identity; see the FL / hardlink / Windows classes at the end.

Bare tier: no browser, no real HERMES_HOME — file_safety's home/root getters are
monkeypatched and browser_cdp must refuse BEFORE it tries to connect.
"""

import json
import os
import sys
from pathlib import Path

import pytest

import agent.file_safety as fs
from agent.file_safety import get_read_block_error, get_write_denied_error
import tools.browser_cdp_tool as cdp_tool
from tools.browser_cdp_tool import browser_cdp
from tools.session_material_policy import session_material_hit

SIBLING_READ_DENIED = (
    "auth.json", "auth.lock", ".env", "webhook_subscriptions.json", "auth/google_oauth.json",
    "cache/bws_cache.json", "mcp-tokens/github.json", "browser-profile/Cookies", "vault/vault.key",
    "skills/.hub/index.json",
)
SIBLING_WRITE_DENIED = (
    ".env", "state.db", "sessions/2026-09-22.json", "mcp-tokens/github.json", "pairing/code.txt",
    "cron/jobs.json", "cron/.jobs_ab12.tmp", "cron/jobs.json.bak", "cron/.jobs.lock",
    "cron/output/job1/2026.md",
)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """thoth is the active profile; neith is a sibling; the root is the shared parent."""
    root = tmp_path / "hermes"
    active, sibling = root / "profiles" / "thoth", root / "profiles" / "neith"
    for base in (root, active, sibling):
        for rel in SIBLING_READ_DENIED + SIBLING_WRITE_DENIED + ("SOUL.md", "memories/notes.md"):
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: active)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
    monkeypatch.setenv("HOME", str(tmp_path))  # ~/.lucaryin
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    return root, active, sibling


class TestDenySetIsProfileWide:
    @pytest.mark.parametrize("rel", SIBLING_READ_DENIED)
    def test_sibling_profile_credential_store_read_is_denied(self, fleet, rel):
        _root, _active, sibling = fleet
        assert get_read_block_error(str(sibling / rel)), rel

    @pytest.mark.parametrize("rel", SIBLING_WRITE_DENIED)
    def test_sibling_profile_state_write_is_denied(self, fleet, rel):
        _root, _active, sibling = fleet
        assert get_write_denied_error(str(sibling / rel)), rel

    @pytest.mark.parametrize("which", ["root", "active", "sibling"])
    @pytest.mark.parametrize("rel", ["cron/jobs.json", "cron/.jobs_ab12.tmp", "cron/jobs.json.bak"])
    def test_cron_store_is_write_denied_in_every_profile_dir(self, fleet, which, rel):
        root, active, sibling = fleet
        base = {"root": root, "active": active, "sibling": sibling}[which]
        assert get_write_denied_error(str(base / rel)), (which, rel)
        assert get_read_block_error(str(base / rel)) is None  # reads stay allowed

    def test_deny_holds_at_every_trust_level(self, fleet, monkeypatch):
        """The runtime layer has no trust dial: nothing in the environment relaxes it."""
        _root, _active, sibling = fleet
        for trust in ("full", "cautious", "manual", "always_approve"):
            monkeypatch.setenv("HERMES_TRUST_LEVEL", trust)
            monkeypatch.setenv("LUCARYIN_TRUST_LEVEL", trust)
            assert get_read_block_error(str(sibling / "auth.json"))
            assert get_write_denied_error(str(sibling / "state.db"))

    def test_profile_created_after_import_is_covered(self, fleet):
        root, _active, _sibling = fleet
        late = root / "profiles" / "ptah"
        (late / "mcp-tokens").mkdir(parents=True)
        (late / "mcp-tokens" / "t.json").write_text("x")
        (late / "auth.json").write_text("x")
        assert get_read_block_error(str(late / "auth.json"))
        assert get_read_block_error(str(late / "mcp-tokens" / "t.json"))
        assert get_write_denied_error(str(late / "state.db"))

    def test_ordinary_sibling_files_are_not_over_blocked(self, fleet):
        _root, _active, sibling = fleet
        assert get_read_block_error(str(sibling / "SOUL.md")) is None
        assert get_write_denied_error(str(sibling / "memories" / "notes.md")) is None
        assert get_read_block_error(str(sibling / "cron" / "jobs.json")) is None

    def test_a_file_named_profiles_is_not_a_profile(self, fleet, tmp_path):
        root, _active, _sibling = fleet
        (root / "profiles" / "README.md").write_text("not a profile")
        assert fs._lucaryin_profile_homes() == sorted(
            p for p in (root / "profiles").iterdir() if p.is_dir())
        assert get_read_block_error(str(root / "profiles" / "README.md")) is None

    def test_missing_profiles_dir_is_harmless(self, tmp_path, monkeypatch):
        root = tmp_path / "solo"
        root.mkdir()
        monkeypatch.setattr(fs, "_hermes_home_path", lambda: root)
        monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
        assert fs._lucaryin_profile_homes() == []
        assert get_read_block_error(str(root / "SOUL.md")) is None


class TestLucaryinGateStateDirs:
    @pytest.mark.parametrize("sub", ["approved", "approvals", "policies", "interactive-grants", "grants"])
    def test_gate_state_dirs_are_write_denied(self, fleet, tmp_path, sub):
        target = tmp_path / ".lucaryin" / sub / "abc123.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        assert get_write_denied_error(str(target)), sub
        assert get_write_denied_error(str(target.parent / "nested" / "deep.json")), sub

    def test_other_lucaryin_dirs_stay_writable(self, fleet, tmp_path):
        (tmp_path / ".lucaryin" / "notes").mkdir(parents=True)
        assert get_write_denied_error(str(tmp_path / ".lucaryin" / "notes" / "a.md")) is None
        assert get_write_denied_error(str(tmp_path / ".lucaryin" / "action_items.json")) is None


# ── case / normalization variants (review, 2026-09-23) ───────────────────────

@pytest.fixture
def folding(monkeypatch):
    """Case-insensitive semantics (macOS / Windows) on any CI host: the deny layer
    folds on darwin and nt; forcing it here runs the same assertions on Linux."""
    monkeypatch.setattr(fs, "_DENY_FOLDS_CASE", True)


class TestCaseVariantsAreDenied:
    @pytest.mark.parametrize("variant", [
        "CRON/jobs.json", "Cron/jobs.json", "cron/JOBS.JSON", "CRON/.jobs_ab12.tmp",
    ])
    @pytest.mark.parametrize("which", ["root", "active", "sibling"])
    def test_cron_store_write_case_variants(self, fleet, folding, which, variant):
        root, active, sibling = fleet
        base = {"root": root, "active": active, "sibling": sibling}[which]
        assert get_write_denied_error(str(base / variant)), (which, variant)

    @pytest.mark.parametrize("variant", [
        "NEITH/auth.json", "neith/AUTH.JSON", "Neith/Auth.Json", "NEITH/.ENV",
        "neith/MCP-TOKENS/github.json", "neith/Auth/Google_OAuth.json",
    ])
    def test_sibling_credential_read_case_variants(self, fleet, folding, variant):
        root, _active, _sibling = fleet
        assert get_read_block_error(str(root / "profiles" / variant)), variant

    @pytest.mark.parametrize("sub", ["Approved", "APPROVALS", "Interactive-Grants"])
    def test_gate_state_dir_case_variants(self, fleet, folding, tmp_path, sub):
        target = tmp_path / ".lucaryin" / sub / "forged.json"
        assert get_write_denied_error(str(target)), sub

    def test_unicode_variant_of_a_denied_name(self, fleet, folding):
        """KELVIN SIGN (U+212A) normalizes to 'K': APFS opens mcp-to\u212aens as mcp-tokens."""
        root, _active, _sibling = fleet
        assert get_read_block_error(str(root / "profiles" / "neith" / "mcp-to\u212aens" / "github.json"))

    def test_folding_does_not_widen_to_unrelated_names(self, fleet, folding):
        _root, _active, sibling = fleet
        assert get_read_block_error(str(sibling / "SOUL.md")) is None
        assert get_write_denied_error(str(sibling / "memories" / "notes.md")) is None
        assert get_write_denied_error(str(sibling / "cronjobs-notes.md")) is None

    def test_on_a_case_insensitive_volume_the_variant_is_the_real_file(self, fleet):
        """No monkeypatch: the PLATFORM default folds (macOS / Windows), and the
        variant spelling really opens the protected file — the reviewer's probe."""
        root, _active, _sibling = fleet
        variant = root / "CRON" / "jobs.json"
        if not variant.exists():
            pytest.skip("case-sensitive volume: the variant is a different file here")
        assert fs._DENY_FOLDS_CASE, "a case-insensitive volume must fold"
        assert get_write_denied_error(str(variant))
        assert get_read_block_error(str(root / "profiles" / "NEITH" / "auth.json"))


class TestBridgeBearerAndAuthDir:
    """B1 publishes the bridge bearer to $LUCARYIN_AUTH_DIR/bridge.token and states
    the runtime's deny set must cover it (server.py _publish_bridge_bearer_file)."""

    @pytest.fixture
    def auth(self, fleet, tmp_path, monkeypatch):
        monkeypatch.delenv("LUCARYIN_AUTH_DIR", raising=False)
        d = tmp_path / ".lucaryin" / "auth"
        d.mkdir(parents=True)
        for name in ("bridge.token", "google.token", "plaid.token", "google.json"):
            (d / name).write_text("secret")
        return d

    @pytest.mark.parametrize("name", ["bridge.token", "google.token", "plaid.token", "google.json"])
    def test_read_and_write_denied(self, auth, name):
        assert get_read_block_error(str(auth / name)), name
        assert get_write_denied_error(str(auth / name)), name

    def test_the_directory_itself_and_new_files(self, auth):
        assert get_read_block_error(str(auth))
        assert get_write_denied_error(str(auth / "new.token"))

    def test_env_spelling_of_the_dir(self, auth, tmp_path, monkeypatch):
        other = tmp_path / "custom-auth"
        other.mkdir()
        (other / "bridge.token").write_text("secret")
        monkeypatch.setenv("LUCARYIN_AUTH_DIR", str(other))
        assert get_read_block_error(str(other / "bridge.token"))
        assert get_write_denied_error(str(other / "bridge.token"))

    def test_case_variant(self, auth, folding, tmp_path):
        assert get_read_block_error(str(tmp_path / ".lucaryin" / "AUTH" / "Bridge.Token"))

    def test_symlink_to_the_bearer_is_denied(self, auth, tmp_path):
        link = tmp_path / "innocent.txt"
        link.symlink_to(auth / "bridge.token")
        assert get_read_block_error(str(link))

    def test_neighbouring_lucaryin_files_stay_readable(self, auth, tmp_path):
        (tmp_path / ".lucaryin" / "notes.md").write_text("x")
        assert get_read_block_error(str(tmp_path / ".lucaryin" / "notes.md")) is None
        assert get_read_block_error(str(tmp_path / ".lucaryin" / "authors.md")) is None


class TestSiblingPrivateState:
    @pytest.fixture
    def private(self, fleet):
        root, active, sibling = fleet
        for base in (root, active, sibling):
            (base / "config.yaml").write_text("mcp_servers: {}")
            (base / "sessions").mkdir(exist_ok=True)
            (base / "sessions" / "s1.json").write_text("{}")
        return fleet

    @pytest.mark.parametrize("rel", ["config.yaml", "state.db", "sessions/s1.json", "sessions"])
    def test_sibling_private_state_is_read_denied(self, private, rel):
        root, _active, sibling = private
        assert get_read_block_error(str(sibling / rel)), rel
        assert get_read_block_error(str(root / rel)), rel  # the root is thoth's home here

    def test_sibling_config_yaml_is_write_denied(self, private):
        _root, _active, sibling = private
        assert get_write_denied_error(str(sibling / "config.yaml"))

    @pytest.mark.parametrize("rel", ["config.yaml", "sessions/s1.json"])
    def test_own_copies_keep_upstream_behaviour(self, private, rel):
        _root, active, _sibling = private
        assert get_read_block_error(str(active / rel)) is None, rel

    def test_own_config_yaml_stays_writable(self, private):
        _root, active, _sibling = private
        assert get_write_denied_error(str(active / "config.yaml")) is None

    def test_root_as_the_active_home(self, private, monkeypatch):
        root, _active, sibling = private
        monkeypatch.setattr(fs, "_hermes_home_path", lambda: root)
        assert get_read_block_error(str(root / "config.yaml")) is None
        assert get_read_block_error(str(sibling / "config.yaml"))


def test_deny_check_cost_stays_near_upstream(fleet, monkeypatch):
    """Review perf finding: with the set profile-wide, every candidate store was
    resolved in full — ~90 Path.resolve() calls per check with six profiles (3.3 ms
    against upstream's 0.95 ms). Now only the base dirs are resolved and each
    candidate costs an lstat or two (_resolve_child). Counted, not timed, so a
    slow CI runner cannot flake it."""
    from pathlib import Path
    root, _active, _sibling = fleet
    for name in ("ptah", "set", "motion", "clara"):
        (root / "profiles" / name).mkdir(parents=True, exist_ok=True)
    calls = []
    real_resolve = Path.resolve

    def counting(self, *a, **k):
        calls.append(str(self))
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", counting)
    assert fs.get_read_block_error(str(root.parent / "Documents" / "report.txt")) is None
    n_dirs = len(fs._hermes_dirs())  # itself resolves each dir once
    calls.clear()
    fs.get_read_block_error(str(root.parent / "Documents" / "report.txt"))
    # target + (home, root, profiles) + auth dir(s) + agent-browser + active home
    assert len(calls) <= n_dirs + 8, (len(calls), calls)


def test_resolve_child_matches_a_full_resolve(tmp_path):
    base = tmp_path.resolve()
    (base / "auth").mkdir()
    (base / "real.json").write_text("x")
    (base / "auth" / "google_oauth.json").symlink_to(base / "real.json")
    (base / "linkdir").symlink_to(base / "auth", target_is_directory=True)
    for rel in ("auth.json", os.path.join("auth", "google_oauth.json"), os.path.join("linkdir", "x.json"),
                os.path.join("missing", "deeper", "x"), "auth"):
        assert fs._resolve_child(base, rel) == str((base / rel).resolve()), rel


# ── alias spellings: file identity, not only text (verifier, 2026-09-23) ─────
# realpath() keeps the macOS firmlink spelling: /System/Volumes/Data/<root>/...
# is the same inode as /<root>/..., so every text-only deny above fell to one
# prefix — reproduced end to end on a temp HOME (write_file rewrote jobs.json;
# read_file returned a sibling's auth.json and ~/.lucaryin/auth/bridge.token).
# 0008 now folds that prefix textually AND compares file identity (st_dev,
# st_ino); the `layer` fixture runs every FL case with both layers and with
# each layer ALONE, so either one regressing fails here.

FIRMLINK = "/System/Volumes/Data"


def _fl(p) -> str:
    """The firmlink spelling of ``p`` (same inode; realpath() leaves it alone)."""
    return FIRMLINK + os.path.realpath(str(p))


@pytest.fixture(params=["both-layers", "identity-only", "text-only"])
def layer(request, monkeypatch):
    if request.param == "both-layers":  # the shipped code, untouched
        yield request.param
        return
    fs._deny_key_of.cache_clear()
    if request.param == "identity-only":
        monkeypatch.setattr(fs, "_firmlink_roots_cache", ())      # no textual fold
    else:
        monkeypatch.setattr(fs, "_deny_same_or_under", lambda *a, **k: False)
    yield request.param
    fs._deny_key_of.cache_clear()


@pytest.fixture
def firmlinked(fleet, tmp_path, monkeypatch):
    if sys.platform != "darwin" or not os.path.exists(_fl(tmp_path)):
        pytest.skip("the /System/Volumes/Data firmlink spelling exists only on macOS")
    monkeypatch.delenv("LUCARYIN_AUTH_DIR", raising=False)
    auth = tmp_path / ".lucaryin" / "auth"
    auth.mkdir(parents=True)
    (auth / "bridge.token").write_text("secret")
    (tmp_path / ".lucaryin" / "approvals").mkdir(parents=True)
    return fleet


class TestFirmlinkSpellingsAreDenied:
    @pytest.mark.parametrize("which", ["root", "active", "sibling"])
    def test_FL_cron_jobs_json_write(self, firmlinked, layer, which):
        root, active, sibling = firmlinked
        base = {"root": root, "active": active, "sibling": sibling}[which]
        real = base / "cron" / "jobs.json"
        target = _fl(real)
        assert os.path.samefile(target, real)  # the spelling really opens jobs.json
        assert get_write_denied_error(target), (layer, target)
        assert get_write_denied_error(_fl(base / "cron" / ".jobs_new.tmp")), layer  # a new file there

    @pytest.mark.parametrize("rel", ["auth.json", "state.db", "config.yaml", "sessions/2026-09-22.json"])
    def test_FL_sibling_private_read(self, firmlinked, layer, rel):
        _root, _active, sibling = firmlinked
        (sibling / "config.yaml").write_text("mcp_servers: {}")
        assert os.path.samefile(_fl(sibling / rel), sibling / rel)
        assert get_read_block_error(_fl(sibling / rel)), (layer, rel)

    def test_FL_bridge_token_read_and_write(self, firmlinked, layer, tmp_path):
        token = tmp_path / ".lucaryin" / "auth" / "bridge.token"
        assert get_read_block_error(_fl(token)), layer
        assert get_write_denied_error(_fl(token)), layer

    def test_FL_approvals_write(self, firmlinked, layer, tmp_path):
        assert get_write_denied_error(_fl(tmp_path / ".lucaryin" / "approvals" / "forged.json")), layer

    def test_FL_ordinary_files_are_not_over_blocked(self, firmlinked, layer, tmp_path):
        _root, active, sibling = firmlinked
        (tmp_path / "notes.md").write_text("x")
        assert get_read_block_error(_fl(sibling / "SOUL.md")) is None
        assert get_read_block_error(_fl(active / "memories" / "notes.md")) is None
        assert get_write_denied_error(_fl(tmp_path / "notes.md")) is None
        assert get_write_denied_error(_fl(tmp_path / ".lucaryin" / "notes.md")) is None


class TestHardlinksAreDenied:
    """A hardlink is the same file under an unrelated name: identity catches it
    for every exact-file deny, and for files inside a small denied directory."""

    @pytest.fixture
    def links(self, fleet, tmp_path, monkeypatch):
        monkeypatch.delenv("LUCARYIN_AUTH_DIR", raising=False)
        auth = tmp_path / ".lucaryin" / "auth"
        auth.mkdir(parents=True)
        (auth / "bridge.token").write_text("secret")
        (fleet[2] / "config.yaml").write_text("mcp_servers: {}")
        out = tmp_path / "work"
        out.mkdir()
        return fleet, auth, out

    @pytest.mark.parametrize("rel", ["auth.json", ".env", "state.db", "config.yaml",
                                     "mcp-tokens/github.json"])
    def test_hardlink_to_sibling_private_state_is_read_denied(self, links, rel):
        (_root, _active, sibling), _auth, out = links
        link = out / "innocent.txt"
        os.link(sibling / rel, link)
        assert get_read_block_error(str(link)), rel

    def test_hardlink_to_the_bridge_token(self, links):
        _fleet, auth, out = links
        link = out / "notes.txt"
        os.link(auth / "bridge.token", link)
        assert get_read_block_error(str(link))
        assert get_write_denied_error(str(link))

    def test_hardlink_to_jobs_json_is_write_denied(self, links):
        (_root, active, _sibling), _auth, out = links
        link = out / "jobs-copy.json"
        os.link(active / "cron" / "jobs.json", link)
        assert get_write_denied_error(str(link))

    def test_an_unrelated_hardlink_is_not_denied(self, links):
        _fleet, _auth, out = links
        (out / "a.txt").write_text("x")
        os.link(out / "a.txt", out / "b.txt")
        assert get_read_block_error(str(out / "b.txt")) is None
        assert get_write_denied_error(str(out / "b.txt")) is None


class TestWindowsSpellings:
    """No NTFS on the CI hosts: the text fold runs on ntpath-style strings, and
    the identity walk runs with ntpath splitting over a fake stat table that
    gives every alias spelling of a file one (st_dev, st_ino) — what NTFS
    reports for \\\\?\\, \\\\?\\UNC\\, \\\\localhost\\C$\\ and 8.3 spellings."""

    @pytest.mark.parametrize("raw,want", [
        ("\\\\?\\C:\\Users\\X\\.hermes\\cron\\jobs.json", "c:\\users\\x\\.hermes\\cron\\jobs.json"),
        ("//?/C:/Users/X/.hermes/cron/jobs.json", "c:\\users\\x\\.hermes\\cron\\jobs.json"),
        ("\\\\?\\UNC\\server\\share\\x\\auth.json", "\\\\server\\share\\x\\auth.json"),
        ("\\\\?\\unc\\server\\share\\x\\auth.json", "\\\\server\\share\\x\\auth.json"),
        ("\\\\.\\C:\\Users\\X\\.lucaryin\\auth\\bridge.token", "c:\\users\\x\\.lucaryin\\auth\\bridge.token"),
        ("C:\\Users\\X\\.hermes\\CRON.\\jobs.json ", "c:\\users\\x\\.hermes\\cron\\jobs.json"),
    ])
    def test_text_fold(self, raw, want):
        assert fs._deny_fold(fs._deny_text(raw, nt=True, darwin=False), nt=True) == want

    def test_darwin_text_fold_only_strips_firmlinked_roots(self):
        assert fs._deny_text("/System/Volumes/Data/Users/x/.hermes", nt=False, darwin=True) == "/Users/x/.hermes"
        assert fs._deny_text("/System/Volumes/Data/private/tmp/x", nt=False, darwin=True) == "/private/tmp/x"
        # not a firmlinked root: left alone (the identity compare still applies)
        assert fs._deny_text("/System/Volumes/Data/bin/x", nt=False, darwin=True) == "/System/Volumes/Data/bin/x"

    EXISTING = {  # canonical (long, local) spelling -> is_dir
        "c:\\": True, "c:\\users": True, "c:\\users\\briencollier": True,
        "c:\\users\\briencollier\\.hermes": True, "c:\\users\\briencollier\\.hermes\\cron": True,
        "c:\\users\\briencollier\\.hermes\\cron\\jobs.json": False,
        "c:\\users\\briencollier\\.hermes\\profiles": True,
        "c:\\users\\briencollier\\.hermes\\profiles\\neith": True,
        "c:\\users\\briencollier\\.hermes\\profiles\\neith\\auth.json": False,
        "c:\\users\\briencollier\\.lucaryin": True, "c:\\users\\briencollier\\.lucaryin\\auth": True,
        "c:\\users\\briencollier\\.lucaryin\\auth\\bridge.token": False,
        "c:\\users\\briencollier\\documents": True,
        "c:\\users\\briencollier\\documents\\notes.txt": False,
    }

    @classmethod
    def _canon(cls, p: str) -> str:
        import ntpath
        s = p.replace("/", "\\")
        low = s.lower()
        for alias, real in (("\\\\?\\unc\\localhost\\c$\\", "c:\\"), ("\\\\localhost\\c$\\", "c:\\"),
                            ("\\\\?\\", ""), ("\\\\.\\", "")):
            if low.startswith(alias):
                low = real + low[len(alias):]
                break
        low = low.replace("\\brienc~1", "\\briencollier")  # the 8.3 short name
        return ntpath.normpath(low) if low not in ("c:\\",) else low

    @pytest.fixture
    def ntfs(self, monkeypatch, folding):
        import ntpath
        import types

        def fake_stat(p):
            c = self._canon(p)
            if c not in self.EXISTING:
                return None
            mode = 0o040755 if self.EXISTING[c] else 0o100644
            return types.SimpleNamespace(st_dev=0x5EED, st_ino=1 + sorted(self.EXISTING).index(c),
                                         st_nlink=1, st_mode=mode)
        monkeypatch.setattr(fs, "_deny_path", ntpath)
        monkeypatch.setattr(fs, "_deny_stat", fake_stat)
        monkeypatch.setattr(fs, "_deny_base_ids", {})
        monkeypatch.setattr(fs, "_deny_dir_ids", {})
        fs._deny_begin_check()
        yield
        fs._deny_begin_check()

    CRON = "C:\\Users\\briencollier\\.hermes\\cron"
    TOKEN = "C:\\Users\\briencollier\\.lucaryin\\auth\\bridge.token"

    @pytest.mark.parametrize("alias", [
        "\\\\localhost\\C$\\Users\\briencollier\\.hermes\\cron\\jobs.json",        # admin share
        "\\\\?\\UNC\\localhost\\C$\\Users\\briencollier\\.hermes\\cron\\jobs.json",
        "\\\\?\\C:\\Users\\briencollier\\.hermes\\cron\\jobs.json",
        "C:\\Users\\BRIENC~1\\.hermes\\cron\\jobs.json",                           # 8.3 short name
        "\\\\localhost\\C$\\Users\\BRIENC~1\\.hermes\\cron\\new-file.json",         # not created yet
    ])
    def test_identity_under_a_denied_dir(self, ntfs, alias):
        fs._deny_begin_check()
        assert fs._deny_same_or_under(alias, self.CRON, exact=False), alias

    @pytest.mark.parametrize("alias", [
        "\\\\localhost\\C$\\Users\\briencollier\\.lucaryin\\auth\\bridge.token",
        "C:\\Users\\BRIENC~1\\.lucaryin\\auth\\bridge.token",
    ])
    def test_identity_of_an_exact_file(self, ntfs, alias):
        fs._deny_begin_check()
        assert fs._deny_same_or_under(alias, self.TOKEN, exact=True), alias

    def test_unrelated_paths_do_not_match(self, ntfs):
        for other in ("\\\\localhost\\C$\\Users\\briencollier\\Documents\\notes.txt",
                      "C:\\Users\\BRIENC~1\\.hermes\\profiles\\neith\\auth.json"):
            fs._deny_begin_check()
            assert not fs._deny_same_or_under(other, self.CRON, exact=False), other
            assert not fs._deny_same_or_under(other, self.TOKEN, exact=True), other

    def test_a_zero_file_id_is_no_identity(self, monkeypatch, ntfs):
        import types
        monkeypatch.setattr(fs, "_deny_stat", lambda p: types.SimpleNamespace(
            st_dev=0, st_ino=0, st_nlink=1, st_mode=0o100644))
        fs._deny_begin_check()
        assert not fs._deny_same_or_under("\\\\server\\share\\x.txt", self.CRON, exact=False)


def test_a_warm_check_stats_only_the_checked_paths_ancestors(fleet, monkeypatch):
    """Identity cost: deny-base stats are cached, so a repeat check stats the
    checked path's ancestors (and itself) and nothing else."""
    root, _active, sibling = fleet
    target = str(root.parent / "Documents" / "report.txt")
    get_read_block_error(target)                     # warm the base cache
    calls = []
    real = fs._deny_stat

    def counting(p):
        calls.append(p)
        return real(p)
    monkeypatch.setattr(fs, "_deny_stat", counting)
    get_read_block_error(target)
    depth = len(Path(target).parts)
    assert len(calls) <= depth + 1, (len(calls), calls)
