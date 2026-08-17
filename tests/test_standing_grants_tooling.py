"""The authoring half of standing grants: the tool and the run prompt.

Companion to hermes-bridge's test_standing_grants.py (which pins the gate).
Origin: the Google Ads reply monitor (2026-08-17) — blocked four times from
its own remit because no path existed to hand a job a grant, while the agent
told the founder pre-approval doesn't exist.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tools.cronjob_tools import _validate_grants
import importlib.util as _u

_spec = _u.spec_from_file_location(
    "cron_grants",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cron", "cron_grants.py"))
cron_grants = _u.module_from_spec(_spec)
_spec.loader.exec_module(cron_grants)

SCHED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cron", "scheduler.py")
KAFIA = "kafiak@xwf.google.com"


class GrantValidation(unittest.TestCase):

    def test_the_google_ads_grant_validates(self):
        self.assertIsNone(_validate_grants(
            [{"action": "email_send", "to": [KAFIA], "max_per_run": 1}]))

    def test_payment_is_refused_with_a_reason(self):
        err = _validate_grants([{"action": "payment_execute"}])
        self.assertIn("never", err)

    def test_grant_creation_cannot_be_granted(self):
        self.assertIsNotNone(_validate_grants([{"action": "standing_grant"}]))

    def test_email_grant_without_allowlist_is_refused(self):
        err = _validate_grants([{"action": "email_send"}])
        self.assertIn("allowlist", err)

    def test_outbound_call_needs_explicit_unattended(self):
        self.assertIsNotNone(_validate_grants([{"action": "outbound_call"}]))
        self.assertIsNone(_validate_grants(
            [{"action": "outbound_call", "unattended": True}]))

    def test_unknown_action_names_the_grantable_set(self):
        err = _validate_grants([{"action": "launch_rockets"}])
        self.assertIn("grantable", err)


class RunPromptAwareness(unittest.TestCase):

    def test_grant_phrasing_is_second_person_and_scoped(self):
        line = cron_grants.describe_grants_for_prompt(
            [{"action": "email_send", "to": [KAFIA], "max_per_run": 1}])
        self.assertIn("You may send email", line)
        self.assertIn(KAFIA, line)
        self.assertIn("ONLY", line)
        self.assertIn("at most 1 per run", line)

    def test_scheduler_injects_grants_into_the_prompt(self):
        """Source pin: the run must be TOLD its powers, or it refuses its own
        remit — that is the half of the incident enforcement can't fix."""
        with open(SCHED) as f:
            src = f.read()
        self.assertIn("Standing permissions for this run", src)
        self.assertIn("describe_grants_for_prompt", src)
        self.assertIn("without asking for approval", src)


if __name__ == "__main__":
    unittest.main()
