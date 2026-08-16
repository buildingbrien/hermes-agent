"""Background work has to still be answerable after the writer dies.

The incident, 2026-08-16. Ptah launched a 25-logo focus group as a detached
process and its record in ~/.hermes/profiles/ptah/processes.json read:

    pid=48695, command="cd ~/.lucaryin/rebrand-v2 && python3 focus_group.py",
    started=1786906326.7, id=None, status=None    # no exit_code, no ended_at

That pid had been dead for over an hour and nothing on disk said so. Asked "how
is the project going", Ptah could not see its own work, fell back to session
recall, and told the founder a finished exercise was "about to run" — then
restated month-old contract facts as current. The missing lifecycle became
confident fiction.

Reconcile-on-read is the load-bearing idea: the failure mode IS that the writer
died, so anything relying on the writer recording its own exit cannot work.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.process_registry import (
    reconcile_process_records,
    _record_liveness,
)


class ReconcileOnReadTest(unittest.TestCase):
    """A record whose pid is gone must read as finished, not running."""

    def _write(self, records):
        d = tempfile.mkdtemp()
        p = Path(d) / "processes.json"
        p.write_text(json.dumps(records), encoding="utf-8")
        return p

    def test_the_exact_record_ptah_was_left_with(self):
        """Verbatim shape from the incident: started, never updated."""
        p = self._write([{
            "session_id": "proc_cc4c9f83cb9a",
            "command": "cd /Users/briencollier/.lucaryin/rebrand-v2 && python3 focus_group.py",
            "pid": 48695,
            "started_at": 1786906326.703939,
            "status": None,
            "exit_code": None,
            "ended_at": None,
        }])
        out = reconcile_process_records(p)
        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertNotEqual(
            _record_liveness(rec), "running",
            "a record whose pid is gone must never still read as running — "
            "that is the whole bug",
        )

    def test_a_live_pid_is_never_reported_gone(self):
        """Liveness is alive | gone | unverified. A running process must not be
        written off as finished — that would invent a completion, which is the
        mirror image of the original bug."""
        p = self._write([{
            "session_id": "proc_self",
            "command": "python3 -c pass",
            "pid": os.getpid(),          # certainly alive: it is this test
            "started_at": 1786906326.0,
            "status": "running",
        }])
        rec = reconcile_process_records(p)[0]
        self.assertIn(_record_liveness(rec), ("alive", "unverified"))
        self.assertNotEqual(_record_liveness(rec), "gone")

    def test_unprovable_identity_says_so_rather_than_guessing(self):
        """A PID from a different namespace cannot be checked with a host ps.
        Saying "unverified" is the honest answer; guessing either way is how a
        status field starts lying."""
        p = self._write([{
            "session_id": "proc_sandboxed",
            "command": "sleep 999",
            "pid": os.getpid(),
            "pid_scope": "sandbox",
            "started_at": 1786906326.0,
            "status": "running",
        }])
        rec = reconcile_process_records(p)[0]
        self.assertEqual(_record_liveness(rec), "unverified")

    def test_a_recorded_exit_is_believed_over_any_pid_guess(self):
        """PIDs are recycled, so a live PID proves nothing on its own. A
        recorded ending is authoritative."""
        p = self._write([{
            "session_id": "proc_done",
            "command": "echo hi",
            "pid": os.getpid(),          # alive, but this run already ended
            "started_at": 1786906326.0,
            "status": "exited",
            "exit_code": 0,
            "ended_at": 1786906400.0,
        }])
        rec = reconcile_process_records(p)[0]
        self.assertEqual(_record_liveness(rec), "gone")

    def test_missing_file_is_not_an_exception(self):
        """A plain hermes-agent host has no registry. Reading it must degrade,
        never raise into an agent's turn."""
        missing = Path(tempfile.mkdtemp()) / "nope.json"
        self.assertEqual(reconcile_process_records(missing), [])

    def test_corrupt_file_is_not_an_exception(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "processes.json"
        p.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(reconcile_process_records(p), [])

    def test_reconcile_does_not_rewrite_the_caller_s_file(self):
        """Reading status must not mutate a file another process may be
        appending to — that class already cost this fleet a silent cron outage
        via a non-atomic config rewrite."""
        p = self._write([{
            "session_id": "proc_x", "command": "sleep 1",
            "pid": 424242, "started_at": 1786906326.0, "status": None,
        }])
        before = p.read_text(encoding="utf-8")
        reconcile_process_records(p)
        self.assertEqual(p.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
