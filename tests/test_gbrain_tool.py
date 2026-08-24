"""gbrain recall must not depend on the shell environment OR a specific engine.

Two incidents this pins:
  • 2026-08-16: gbrain_tool shelled out to `psql` and gated availability on
    shutil.which("psql"). Every terminal has Homebrew on PATH, so hand-run
    checks passed — but a Finder-launched app inherits launchd's PATH (no
    Homebrew), so every worker reported "not reachable" against a healthy brain.
  • 2026-08-24: the follow-up used psycopg against a HARDCODED
    postgres://localhost:5432. That works only on the founder's hand-built
    Postgres box; every customer runs a PGLite brain with no :5432 server, so
    recall silently failed fleet-wide and the registry dropped both tools.

The fix for both: route through the local gbrain BRIDGE (:9050), which shells to
the `gbrain` CLI and so speaks PGLite and Postgres identically, with no PATH and
no engine assumption. These tests pin that class of behavior: no shell lookup,
no hardcoded DB, and graceful degradation when the bridge is unreachable.
"""

import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "gbrain_tool.py")


def _bridge_reachable() -> bool:
    """True when a gbrain bridge answers on :9050 (the founder's live machine)."""
    import tools.gbrain_tool as gt
    try:
        with urllib.request.urlopen(f"{gt.GBRAIN_BRIDGE}/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class NoShellOrEngineDependency(unittest.TestCase):
    """The class-level pin: recall must work wherever the bridge works — no PATH
    lookup, no shell-out, and no hardcoded database of any engine."""

    def setUp(self):
        with open(MODULE_PATH) as f:
            self.src = f.read()

    def test_no_psql_binary_dependency(self):
        # "psql" may appear in prose (the incident writeup) but never as a
        # looked-up executable.
        self.assertNotIn('which("psql")', self.src)
        self.assertNotIn("which('psql')", self.src)

    def test_no_shell_out_at_all(self):
        self.assertNotIn("import subprocess", self.src)
        self.assertNotIn("import shutil", self.src)

    def test_no_hardcoded_postgres_or_driver(self):
        # The 2026-08-24 regression: a direct psycopg driver against a hardcoded
        # postgres://localhost:5432. "postgres://" may appear in the incident
        # writeup (docstring) but the DRIVER and the CONNECT CALL must not.
        self.assertNotIn("import psycopg", self.src)
        self.assertNotIn("psycopg.connect", self.src)

    def test_routes_through_the_bridge(self):
        # Positive pin: recall goes over loopback HTTP to the gbrain bridge.
        self.assertIn("GBRAIN_BRIDGE", self.src)
        self.assertIn("/api/gbrain/", self.src)


class UnavailabilityIsReportedNotRaised(unittest.TestCase):
    """A machine whose gbrain bridge is unreachable reports recall unavailable —
    it must never raise into an agent turn."""

    def test_bridge_unreachable_is_named(self):
        import tools.gbrain_tool as gt
        old = gt.GBRAIN_BRIDGE
        gt.GBRAIN_BRIDGE = "http://127.0.0.1:59999"  # nothing listens here
        try:
            res = gt.check_gbrain_requirements()
        finally:
            gt.GBRAIN_BRIDGE = old
        self.assertFalse(res["available"])
        self.assertIn("bridge", res.get("reason", "").lower())

    def test_search_degrades_to_json_not_exception(self):
        """A dead bridge must never raise into an agent turn."""
        import tools.gbrain_tool as gt
        old = gt.GBRAIN_BRIDGE
        gt.GBRAIN_BRIDGE = "http://127.0.0.1:59999"
        try:
            out = json.loads(gt.gbrain_search("anything"))
        finally:
            gt.GBRAIN_BRIDGE = old
        self.assertFalse(out.get("recall_available", True))
        self.assertIn("not reachable", out["error"])

    def test_read_degrades_to_json_not_exception(self):
        import tools.gbrain_tool as gt
        old = gt.GBRAIN_BRIDGE
        gt.GBRAIN_BRIDGE = "http://127.0.0.1:59999"
        try:
            out = json.loads(gt.gbrain_read("people/anyone"))
        finally:
            gt.GBRAIN_BRIDGE = old
        self.assertFalse(out.get("recall_available", True))


@unittest.skipUnless(_bridge_reachable(), "no gbrain bridge on this machine")
class LiveRoundTrip(unittest.TestCase):
    """On the founder's machine: the real brain answers through the bridge —
    engine-agnostic (works whether it's Postgres or PGLite behind :9050)."""

    def test_search_finds_a_known_entity(self):
        import tools.gbrain_tool as gt
        out = json.loads(gt.gbrain_search("Courtenay"))
        self.assertIn("results", out)
        self.assertGreater(len(out["results"]), 0)
        slug = out["results"][0]["slug"]

        page = json.loads(gt.gbrain_read(slug))
        self.assertEqual(page["slug"], slug)
        self.assertTrue(page["compiled_truth"])

    def test_search_survives_a_hostile_query(self):
        """A hostile query is URL-encoded to the bridge as literal text; it must
        never break the query path or the brain."""
        import tools.gbrain_tool as gt
        out = json.loads(gt.gbrain_search("Rob'); DROP TABLE pages;--"))
        self.assertNotIn("error", out.get("results", [{}])[0] if out.get("results") else {})
        again = json.loads(gt.gbrain_search("Courtenay"))
        self.assertGreater(len(again.get("results", [])), 0)


if __name__ == "__main__":
    unittest.main()
