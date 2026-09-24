"""EMAIL_SEND RESULT CONTRACT (Wave 2 lead decision; R2-1-38 / HA4 pairing).

The bridge reads email_send's result in three places (lucaryin-ai
hermes-bridge): the chat confirmation line, the worker's false-success guard,
and the approved-send path that decides whether an approval was used. A
de-duplicated call has exactly two shapes, and both repos pin them key by key
(lucaryin-ai carries the mirror fixture test in the companion PR):

* ``match: "message"`` — the identical message already went out inside the
  window. It WAS delivered, so it counts as sent everywhere::

      {sent: true, already_sent: true, deduplicated: true, match: "message",
       idempotent_skip: true, summary: "<human line>"}

* ``match: "thread"`` — same recipients + subject from a cron run inside the
  hold window, different content. It did NOT go out, and an approval must not
  be consumed by it — so no ``idempotent_skip`` and no ``error``::

      {sent: false, held: true, deduplicated: true, match: "thread",
       reason: "<why>", summary: "<human line>"}

A send that happens now is unchanged (``sent: true`` plus the send details,
none of the dedup keys).

Bare tier: the real tool handler, himalaya stubbed, HERMES_HOME a tmp dir.
"""

import email
from email import policy

import pytest

from gateway.session_context import _VAR_MAP
from tools import email_send_tool as est

# The contract, written out literally. Do NOT derive these from the module: the
# point is that a change to email_send_tool.py without a change here (and in
# the lucaryin-ai mirror) fails.
MESSAGE_MATCH = {
    "sent": True,
    "already_sent": True,
    "deduplicated": True,
    "match": "message",
    "idempotent_skip": True,
    "summary": str,
}
THREAD_HOLD = {
    "sent": False,
    "held": True,
    "deduplicated": True,
    "match": "thread",
    "reason": str,
    "summary": str,
}
SENT_NOW_KEYS = {"sent", "to", "cc", "subject", "attachments", "account", "summary"}


class _Proc:
    returncode = 0
    stdout = b""
    stderr = b""


@pytest.fixture
def himalaya(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_EMAIL_DRYRUN", raising=False)
    monkeypatch.setattr(est, "_himalaya", lambda: "/usr/bin/true")
    sent = []

    def fake_run(cmd, input=None, **_kw):
        sent.append(email.message_from_bytes(input, policy=policy.default))
        return _Proc()

    monkeypatch.setattr(est.subprocess, "run", fake_run)
    return sent


@pytest.fixture
def cron_run():
    var = _VAR_MAP["HERMES_CRON_SESSION"]

    class _Ctx:
        def __enter__(self):
            self.token = var.set("1")

        def __exit__(self, *exc):
            var.reset(self.token)
    return _Ctx()


def _send(**args):
    base = {"to": "owner@x.com", "subject": "Evening recap", "body": "v1", "signature": False}
    return est.email_send_tool({**base, **args})


def _assert_shape(out, contract):
    assert set(out) == set(contract), (sorted(out), sorted(contract))
    for key, want in contract.items():
        if want is str:
            assert isinstance(out[key], str) and out[key].strip(), (key, out[key])
        else:
            # `is` for the booleans: 1 / "true" are not the contract.
            assert out[key] is want if isinstance(want, bool) else out[key] == want, (key, out[key])


def test_sent_now_shape_is_unchanged(himalaya):
    out = _send()
    assert out["sent"] is True
    assert set(out) == SENT_NOW_KEYS, sorted(out)
    assert len(himalaya) == 1


def test_message_match_is_delivered_earlier_and_counts_as_sent(himalaya):
    assert _send()["sent"] is True
    out = _send()
    _assert_shape(out, MESSAGE_MATCH)
    assert "Evening recap" in out["summary"] and "owner@x.com" in out["summary"]
    assert len(himalaya) == 1


def test_message_match_from_a_cron_run_is_the_same_shape(himalaya, cron_run):
    """Both sides in cron, identical content: the message key wins (delivered)."""
    with cron_run:
        assert _send()["sent"] is True
        out = _send()
    _assert_shape(out, MESSAGE_MATCH)
    assert len(himalaya) == 1


def test_thread_hold_did_not_go_out_and_carries_no_skip_or_error(himalaya, cron_run):
    with cron_run:
        assert _send(body="v1")["sent"] is True
        out = _send(body="v2, re-rendered")
    _assert_shape(out, THREAD_HOLD)
    assert "idempotent_skip" not in out and "error" not in out
    assert "force=true" in out["reason"]
    assert "Evening recap" in out["summary"] and "owner@x.com" in out["summary"]
    assert len(himalaya) == 1


def test_thread_hold_after_an_attended_send_is_the_same_shape(himalaya, cron_run):
    assert _send(body="by hand")["sent"] is True
    with cron_run:
        out = _send(body="the scheduled render")
    _assert_shape(out, THREAD_HOLD)
    assert len(himalaya) == 1


def test_dry_run_dedup_uses_the_same_shapes(himalaya, monkeypatch, cron_run):
    monkeypatch.setenv("HERMES_EMAIL_DRYRUN", "1")
    assert _send()["sent"] is True
    _assert_shape(_send(), MESSAGE_MATCH)
    with cron_run:
        assert _send(subject="Other", body="a")["sent"] is True
        _assert_shape(_send(subject="Other", body="b"), THREAD_HOLD)
    assert himalaya == []


def test_module_key_sets_match_the_contract():
    assert est.DEDUP_MESSAGE_KEYS == set(MESSAGE_MATCH)
    assert est.DEDUP_THREAD_KEYS == set(THREAD_HOLD)
    _assert_shape(est._dedup_result("message", ["a@x.com"], "s"), MESSAGE_MATCH)
    _assert_shape(est._dedup_result("thread", ["a@x.com"], "s"), THREAD_HOLD)
