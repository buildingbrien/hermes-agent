"""Budget exhaustion must end with a report, never a blank.

The incident this pins (2026-08-17): a tester's fresh-machine Heartbeat failed
EVERY run with "Agent completed but produced empty response". The turn loop's
grace-call mechanism — one final untooled call after the iteration budget runs
out — existed in the loop condition and was never armed anywhere: exhaustion
mid-tool-flow just broke out, and a model that had spent its whole budget
working had written nothing. A partial report beats a blank one every time.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run_agent.py")
SCHED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cron", "scheduler.py")


class GraceCallIsArmed(unittest.TestCase):

    def setUp(self):
        with open(SRC) as f:
            self.src = f.read()

    def test_exhaustion_arms_exactly_one_grace_call(self):
        self.assertIn("self._budget_grace_call = True", self.src,
                      "nothing armed the grace flag — exhaustion ended turns empty")
        self.assertIn("self._grace_call_used = True", self.src)

    def test_the_grace_message_forbids_tools_and_asks_for_the_report(self):
        self.assertIn("Do NOT call any more tools", self.src)
        self.assertIn("Write your final answer", self.src)

    def test_grace_is_per_turn_not_per_session(self):
        """A session's second turn must get its own grace call."""
        self.assertIn(
            "self._grace_call_used = False  # each turn gets its own exhaustion grace call",
            self.src)

    def test_scheduler_empty_response_error_names_what_the_run_did(self):
        with open(SCHED) as f:
            sched = f.read()
        self.assertIn("[diagnostic:", sched)
        self.assertIn("get_activity_summary", sched)


if __name__ == "__main__":
    unittest.main()
