"""dial_meeting mode routing — the founder-on-a-call incident, pinned.

2026-08-18: the founder added Ptah to a live call and addressed it by wake
word for minutes; it never answered. dial_meeting only ever reached
/api/voice/notetaker — the silent batch recorder with NO live audio path —
so a speaking join was impossible from this tool no matter what the user
asked. The agent even wrote "clerk" into the label trying. mode='clerk' now
routes to /api/voice/dial-meeting (the live wake-gated participant stream
proven on the 2026-08-18 09:38 call).
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import dial_meeting as dm


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkey_target, result_body):
    calls = {}

    def fake_urlopen(req, timeout=0):
        calls["url"] = req.full_url
        calls["body"] = json.loads(req.data.decode())
        return _FakeResp(json.dumps(result_body).encode())

    return calls, fake_urlopen


class DialMeetingModes(unittest.TestCase):
    def setUp(self):
        os.environ["BRIDGE_PROFILE"] = "ptah"
        os.environ["HERMES_SERVER_PORT"] = "9005"

    def test_default_is_notetaker(self):
        calls, fake = _capture(dm, {"success": True, "call_sid": "CAx"})
        with mock.patch.object(dm.urllib.request, "urlopen", fake):
            out = json.loads(dm.dial_meeting_tool({"to": "+12673586564"}))
        self.assertIn("/api/voice/notetaker", calls["url"])
        self.assertEqual(out["mode"], "notetaker")
        # The message must be honest about deafness — the incident began
        # with a user who could not know the bot could not hear him.
        self.assertIn("cannot hear or speak live", out["message"])

    def test_clerk_routes_to_dial_meeting_with_agent(self):
        calls, fake = _capture(dm, {"success": True, "call_sid": "CAy"})
        with mock.patch.object(dm.urllib.request, "urlopen", fake):
            out = json.loads(dm.dial_meeting_tool({
                "to": "+1 267-358-6564", "pin": "304944165",
                "label": "Brien's call", "mode": "clerk",
            }))
        self.assertIn("/api/voice/dial-meeting", calls["url"])
        self.assertEqual(calls["body"]["style"], "clerk")
        self.assertEqual(calls["body"]["agent"], "ptah")
        self.assertEqual(calls["body"]["to"], "+12673586564")
        self.assertEqual(out["mode"], "clerk")
        self.assertIn("copilot", out["message"])

    def test_unknown_mode_falls_back_to_notetaker(self):
        calls, fake = _capture(dm, {"success": True})
        with mock.patch.object(dm.urllib.request, "urlopen", fake):
            out = json.loads(dm.dial_meeting_tool(
                {"to": "+12673586564", "mode": "loudmouth"}))
        self.assertIn("/api/voice/notetaker", calls["url"])
        self.assertEqual(out["mode"], "notetaker")

    def test_clerk_in_label_does_not_select_clerk(self):
        # The exact live failure: intent expressed in the label, not the mode.
        calls, fake = _capture(dm, {"success": True})
        with mock.patch.object(dm.urllib.request, "urlopen", fake):
            out = json.loads(dm.dial_meeting_tool(
                {"to": "+12673586564", "label": "Brien's call — clerk"}))
        self.assertIn("/api/voice/notetaker", calls["url"])
        self.assertEqual(out["mode"], "notetaker")


if __name__ == "__main__":
    unittest.main()
