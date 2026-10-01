"""email_send's idempotent-send ledger (R2-1-38, HA4).

It keyed on recipients + subject only, so within 45 minutes a reply in the same
thread, a corrected body or a new bcc was silently dropped — and reported
``sent: true``. Two keys now:

* the MESSAGE key (to/cc/bcc, folded subject, body, html, attachments): the
  same message never goes twice in the window, whoever sends it;
* the THREAD key (to/cc/bcc + folded subject), the Aug-24 keystone (the
  founder got one recap three times: the cron recap plus re-sends, each
  re-rendering the body): it still holds back a send when a CRON run is on
  either side of the match. Two attended sends in a conversation — the
  R2-1-38 cases — both go out.

A skip follows the EMAIL_SEND RESULT CONTRACT (pinned key by key in
test_email_send_result_contract.py): the identical message is
``sent: true, already_sent: true, match: "message"`` (it went out earlier), a
cron thread hold is ``sent: false, held: true, match: "thread"`` (it did not).
The ledger lock is cross-platform (the old in-line ``import fcntl`` raised on
Windows).

Bare tier: the real tool handler with himalaya stubbed (the messages it would
have been piped are captured); HERMES_HOME is a tmp dir for the ledger.
"""

import email
from email import policy

import pytest

from gateway.session_context import _VAR_MAP
from tools import email_send_tool as est
from tools import lucaryin_filelock


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


def _send(**args):
    base = {"to": "owner@x.com", "subject": "Q3 numbers", "body": "first draft", "signature": False}
    return est.email_send_tool({**base, **args})


def _already_sent(out):
    """The identical message went out earlier: delivered, so sent:true."""
    return (out.get("sent") is True and out.get("already_sent") is True
            and out.get("deduplicated") is True and out.get("match") == "message")


def _held(out):
    """A cron thread hold: this version did NOT go out."""
    return (out.get("sent") is False and out.get("held") is True
            and out.get("deduplicated") is True and out.get("match") == "thread"
            and "idempotent_skip" not in out and "error" not in out)


def test_identical_resend_is_deduplicated_and_says_so(himalaya):
    first = _send()
    second = _send()
    assert first.get("sent") is True and "already_sent" not in first
    assert _already_sent(second), second
    assert second["summary"].startswith("Already sent")
    assert len(himalaya) == 1


def test_whitespace_only_body_edit_is_still_the_same_message(himalaya):
    _send(body="first  draft\n")
    out = _send(body="first draft")
    assert _already_sent(out), out
    assert len(himalaya) == 1


@pytest.mark.parametrize("change", [
    {"body": "corrected numbers: 42"},
    {"html": "<p>first draft</p>"},
    {"bcc": "auditor@y.com"},
    {"cc": "cfo@x.com"},
    {"subject": "Re: Q3 numbers", "body": "the reply"},
])
def test_a_different_message_to_the_same_people_is_sent(himalaya, change):
    assert _send().get("sent") is True
    out = _send(**change)
    assert out.get("sent") is True, out
    assert out.get("deduplicated") is None
    assert len(himalaya) == 2


def test_different_attachments_are_a_different_message(himalaya, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A"); b.write_text("B")
    assert _send(attachments=[str(a)]).get("sent") is True
    assert _send(attachments=[str(b)]).get("sent") is True
    out = _send(attachments=[str(b)])
    assert _already_sent(out), out
    assert len(himalaya) == 2


def test_reply_prefix_alone_does_not_make_a_new_message(himalaya):
    """The subject fold is unchanged: 'Re: ' + the same body is the same intent."""
    _send()
    out = _send(subject="Re: Q3 numbers")
    assert _already_sent(out), out
    assert len(himalaya) == 1


def test_force_sends_a_deliberate_second_copy(himalaya):
    _send()
    out = _send(force=True)
    assert out.get("sent") is True and len(himalaya) == 2


def test_key_covers_every_field():
    k = est._idem_key
    base = dict(bcc=[], body="b", html="", attachments=[])
    ref = k(["a@x.com"], [], "s", **base)
    assert k(["a@x.com"], [], "Re: s", **base) == ref
    assert k(["a@x.com"], [], "s", **{**base, "body": "b "}) == ref
    for variant in (dict(base, bcc=["z@y.com"]), dict(base, body="c"), dict(base, html="<b>"),
                    dict(base, attachments=["/tmp/x"])):
        assert k(["a@x.com"], [], "s", **variant) != ref, variant


# ── the Aug-24 keystone survives for scheduled sends ─────────────────────────

@pytest.fixture
def cron_run():
    """Inside a cron job: the runtime's own marker (the scheduler's job context
    sets the HERMES_CRON_SESSION ContextVar exactly like this)."""
    var = _VAR_MAP["HERMES_CRON_SESSION"]

    class _Ctx:
        def __enter__(self):
            self.token = var.set("1")

        def __exit__(self, *exc):
            var.reset(self.token)
    return _Ctx()


def test_a_rerendered_cron_recap_is_held_back(himalaya, cron_run):
    with cron_run:
        assert _send(subject="Evening recap", body="<b>recap</b> 18:00:01").get("sent") is True
        out = _send(subject="Evening recap", body="<b>recap</b> 18:00:07")
    assert _held(out), out
    assert "scheduled job" in out["summary"] and "force=true" in out["reason"]
    assert len(himalaya) == 1


def test_a_manual_resend_right_after_a_cron_recap_is_held_back(himalaya, cron_run):
    with cron_run:
        assert _send(subject="Evening recap", body="recap v1").get("sent") is True
    out = _send(subject="Re: Evening recap", body="recap v2 (re-rendered)")
    assert _held(out), out
    assert len(himalaya) == 1


def test_a_cron_send_right_after_a_manual_one_is_held_back(himalaya, cron_run):
    assert _send(subject="Evening recap", body="sent by hand").get("sent") is True
    with cron_run:
        out = _send(subject="Evening recap", body="the scheduled render")
    assert _held(out), out
    assert len(himalaya) == 1


def test_attended_conversation_sends_are_not_held_back(himalaya):
    """The R2-1-38 cases, spelled out: all in chat, all go."""
    assert _send(subject="Q3 numbers", body="first").get("sent") is True
    assert _send(subject="Re: Q3 numbers", body="the reply").get("sent") is True
    assert _send(subject="Q3 numbers", body="corrected: 42").get("sent") is True
    assert len(himalaya) == 3


def test_a_cron_send_to_a_different_audience_goes(himalaya, cron_run):
    with cron_run:
        assert _send(subject="Evening recap", body="v1").get("sent") is True
        out = _send(subject="Evening recap", body="v1", bcc="auditor@y.com")
    assert out.get("sent") is True, out
    assert len(himalaya) == 2


def test_force_overrides_the_thread_hold(himalaya, cron_run):
    with cron_run:
        _send(subject="Evening recap", body="v1")
    out = _send(subject="Evening recap", body="v2", force=True)
    assert out.get("sent") is True and len(himalaya) == 2


def test_a_failed_send_does_not_erase_an_earlier_record(himalaya, monkeypatch, cron_run):
    """Releasing a failed attempt must put the thread record back, not drop it —
    otherwise one failed correction reopens the recap storm for the cron."""
    assert _send(subject="Evening recap", body="sent by hand").get("sent") is True
    fake_run = est.subprocess.run

    class _Fail:
        returncode = 1
        stdout = b""
        stderr = b"smtp said no"

    monkeypatch.setattr(est.subprocess, "run", lambda *a, **k: _Fail())
    assert "error" in _send(subject="Evening recap", body="attended correction")
    monkeypatch.setattr(est.subprocess, "run", fake_run)
    with cron_run:
        out = _send(subject="Evening recap", body="the scheduled render")
    assert _held(out), out
    assert len(himalaya) == 1


def test_release_restores_the_prior_thread_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tk = est._thread_key(["a@x.com"], [], "Recap")
    decision, res = est._reserve_send("m:1", tk, unattended=True)
    assert decision == "go"
    est._commit_send(res, "first")
    decision, res2 = est._reserve_send("m:2", tk, unattended=False)
    assert decision == "skip" and res2["match"] == "thread"
    # an attended send in another thread reserves, fails, releases
    tk2 = est._thread_key(["a@x.com"], [], "Other")
    decision, res3 = est._reserve_send("m:3", tk2, unattended=False)
    est._commit_send(res3, "other")
    decision, res4 = est._reserve_send("m:4", tk2, unattended=False)
    assert decision == "go"  # attended + attended: allowed
    est._release_send(res4)
    ledger = __import__("json").loads((tmp_path / "email-send-ledger.json").read_text())
    assert ledger[tk2]["status"] == "sent" and ledger[tk2]["summary"] == "other"
    assert "m:4" not in ledger


def test_the_ledger_works_without_fcntl(himalaya, monkeypatch):
    """Windows has no fcntl: the old ``import fcntl`` inside the send path raised
    ImportError there. With neither lock primitive the ledger still dedups."""
    import sys
    monkeypatch.setitem(sys.modules, "fcntl", None)  # any `import fcntl` now raises
    monkeypatch.setattr(lucaryin_filelock, "fcntl", None)
    monkeypatch.setattr(lucaryin_filelock, "msvcrt", None)
    assert _send().get("sent") is True
    out = _send()
    assert _already_sent(out), out
    assert len(himalaya) == 1
