"""gbrain recall must not depend on the shell environment.

The incident this pins (2026-08-16): gbrain_tool shelled out to the `psql`
binary and gated availability on shutil.which("psql"). Every terminal has
Homebrew's /opt/homebrew/bin on PATH, so every hand-run check passed — and the
actual product failed: a Finder-launched app inherits launchd's PATH (no
Homebrew), so every worker reported "gbrain is not reachable" against a healthy
854-page database, for the entire release that shipped the feature.

The fix is a Python driver (psycopg). These tests pin the class, not just the
instance: no PATH-dependent lookup may come back, and the two unavailability
reasons (driver missing vs database absent) must stay distinguishable, because
the first is our provisioning bug and the second is a normal machine.
"""

import builtins
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "gbrain_tool.py")


def _db_reachable() -> bool:
    try:
        import psycopg
        with psycopg.connect(
                os.environ.get("GBRAIN_URL", "postgres://localhost:5432/gbrain"),
                connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


class NoShellDependency(unittest.TestCase):
    """The class-level pin: recall must work wherever Python works."""

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

    def test_queries_are_parameterized_not_escaped(self):
        # The old code hand-escaped quotes into inline SQL. With a driver that
        # job belongs to parameterization; the escaper must not survive.
        self.assertNotIn('replace("\'", "\'\'")', self.src)


class UnavailabilityReasons(unittest.TestCase):
    """Driver-missing and database-missing are different problems."""

    def test_driver_missing_is_named_as_a_provisioning_problem(self):
        import tools.gbrain_tool as gt
        real_import = builtins.__import__

        def no_psycopg(name, *a, **kw):
            if name == "psycopg":
                raise ImportError("simulated: driver not in venv")
            return real_import(name, *a, **kw)

        builtins.__import__ = no_psycopg
        # psycopg may already be imported from an earlier test — hide it so
        # the lazy import inside the tool actually re-runs.
        saved = sys.modules.pop("psycopg", None)
        try:
            res = gt.check_gbrain_requirements()
        finally:
            builtins.__import__ = real_import
            if saved is not None:
                sys.modules["psycopg"] = saved

        self.assertFalse(res["available"])
        self.assertIn("driver", res["reason"])
        self.assertNotIn("database", res["reason"])

    def test_search_degrades_to_json_not_exception(self):
        """A dead DB must never raise into an agent turn."""
        import tools.gbrain_tool as gt
        old = gt.GBRAIN_URL
        gt.GBRAIN_URL = "postgres://localhost:59999/nope"
        try:
            out = json.loads(gt.gbrain_search("anything"))
        finally:
            gt.GBRAIN_URL = old
        self.assertFalse(out.get("recall_available", True))
        self.assertIn("not reachable", out["error"])


@unittest.skipUnless(_db_reachable(), "no gbrain database on this machine")
class LiveRoundTrip(unittest.TestCase):
    """On the founder's machine: the real brain answers through the driver."""

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
        """The old inline-SQL path needed hand-escaping to survive this; the
        parameterized path must treat it as literal text."""
        import tools.gbrain_tool as gt
        out = json.loads(gt.gbrain_search("Rob'); DROP TABLE pages;--"))
        # No exception, and the DB is intact for the next query.
        self.assertNotIn("error", out.get("results", [{}])[0] if out.get("results") else {})
        again = json.loads(gt.gbrain_search("Courtenay"))
        self.assertGreater(len(again.get("results", [])), 0)


if __name__ == "__main__":
    unittest.main()
