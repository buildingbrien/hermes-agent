"""email_send puts on the wire exactly the recipients the approval gate checked.

A standing grant ("may email these people") is enforced by the bridge gate
(hermes-bridge approval_gate._recipients_of), which reads every recipient out of
to/cc/bcc, split on ',', ';' and line breaks, taking the address inside
"Name <addr>". Two ways the sent message could reach someone the gate never saw
(2026-09-22):

* a display name. EmailMessage decodes an RFC 2047 encoded-word name and writes
  it back unquoted, so "=?utf-8?q?Owner_=3Cz=40evil.com=3E=2C?= <owner@x.com>"
  went out as "Owner <z@evil.com>, <owner@x.com>". The headers now carry the
  bare addresses only.
* different splitting. The tool split on commas only, and never inside a list
  item. It now splits exactly where the gate does.

The explicit 'from' is held to a bare address or a plain ASCII "Name <address>"
on one line.

Bare tier: the real tool handler, with himalaya replaced by a stub that captures
the bytes it would have been piped, parsed back as an MTA would read them.
"""

from __future__ import annotations

import email
from email import policy

import pytest

from tools import email_send_tool as est

OWNER = "owner@x.com"
ENCODED_NAME = "=?utf-8?q?Owner_=3Cz=40evil.com=3E=2C?= <owner@x.com>"


class _Proc:
    returncode = 0
    stdout = b""
    stderr = b""


@pytest.fixture
def himalaya(monkeypatch, tmp_path):
    """Stub himalaya; returns the list of messages it was handed, parsed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # the idempotency ledger
    monkeypatch.delenv("HERMES_EMAIL_DRYRUN", raising=False)
    monkeypatch.setattr(est, "_himalaya", lambda: "/usr/bin/true")
    sent = []

    def fake_run(cmd, input=None, **_kw):
        sent.append(email.message_from_bytes(input, policy=policy.default))
        return _Proc()

    monkeypatch.setattr(est.subprocess, "run", fake_run)
    return sent


def _send(**args):
    base = {"subject": "Q3", "body": "hi", "signature": False, "force": True}
    return est.email_send_tool({**base, **args})


def _wire(msg, header):
    h = msg[header]
    return [a.addr_spec for a in h.addresses] if h is not None else []


# ── Headers carry bare addresses only ────────────────────────────────────────

def test_encoded_word_display_name_cannot_add_a_recipient(himalaya):
    result = _send(to=[ENCODED_NAME])
    assert result.get("sent") is True, result
    (msg,) = himalaya
    assert _wire(msg, "To") == [OWNER]
    assert "evil" not in str(msg["To"])
    assert result["to"] == [OWNER]


def test_display_names_are_dropped_from_to_cc_and_bcc(himalaya):
    result = _send(to="Owner Name <owner@x.com>", cc=['"Bob B." <bob@y.com>'],
                   bcc=["Eve <eve@z.com>"])
    assert result.get("sent") is True, result
    (msg,) = himalaya
    assert str(msg["To"]) == OWNER
    assert str(msg["Cc"]) == "bob@y.com"
    assert str(msg["Bcc"]) == "eve@z.com"
    assert result["cc"] == ["bob@y.com"]


def test_build_message_itself_drops_display_names():
    """Defence in depth: a direct _build_message caller gets bare headers too."""
    msg = est._build_message("Fleet <fleet@x.com>", [ENCODED_NAME], ["Bob <bob@y.com>"],
                             ["eve@z.com"], "s", "b", [])
    wire = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert _wire(wire, "To") == [OWNER]
    assert _wire(wire, "Cc") == ["bob@y.com"]
    assert _wire(wire, "Bcc") == ["eve@z.com"]


@pytest.mark.parametrize("bad", ["a@x.com, b@y.com", "not-an-address", "<a@x.com> b@y.com"])
def test_build_message_refuses_what_is_not_one_address(bad):
    with pytest.raises(ValueError):
        est._build_message("f@x.com", [bad], [], [], "s", "b", [])


# ── Splitting matches the gate ───────────────────────────────────────────────

def test_separator_is_the_gates():
    """Pinned to hermes-bridge approval_gate._RECIPIENT_SEP_RE. If either side
    changes, the gate and this tool disagree about who a send reaches."""
    assert est._RECIPIENT_SEP_RE.pattern == r"[,;\r\n]"


@pytest.mark.parametrize("to,expected", [
    ("a@x.com; b@y.com", ["a@x.com", "b@y.com"]),
    ("a@x.com\nb@y.com\r\nc@z.com", ["a@x.com", "b@y.com", "c@z.com"]),
    (["a@x.com, b@y.com", "c@z.com;d@w.com"], ["a@x.com", "b@y.com", "c@z.com", "d@w.com"]),
    ("a@x.com, , ;", ["a@x.com"]),
    ("Owner <owner@x.com>, Bob <bob@y.com>", [OWNER, "bob@y.com"]),
])
def test_every_piece_the_gate_sees_is_a_separate_recipient(himalaya, to, expected):
    result = _send(to=to)
    assert result.get("sent") is True, result
    assert _wire(himalaya[0], "To") == expected


@pytest.mark.parametrize("to", [
    # A quoted name with a separator is split by the gate, and the first half
    # is no address, so the tool refuses rather than guess.
    ['"Smith, John" <j@x.com>'], ['"Smith; John" <j@x.com>'],
    # An encoded word in the address itself is decoded by EmailMessage into a
    # different address ("z@evil.com@x.com").
    ["=?utf-8?q?z=40evil.com?=@x.com"],
    ["a@x.com b@y.com"], ["a@localhost"], ["a@x.com (comment)"], ['"a b"@x.com'],
    ["owıner@x.com"], ["a@x.com b@y.com"],
])
def test_what_is_not_a_plain_address_is_refused_and_nothing_is_sent(himalaya, to):
    result = _send(to=to)
    assert "do not look like email addresses" in result.get("error", ""), result
    assert himalaya == []


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("value", [5, {"a@x.com": 1}, ["a@x.com", 7], True])
def test_non_string_address_fields_are_refused_not_crashed(himalaya, field, value):
    args = {"to": "a@x.com", field: value}
    result = _send(**args)
    assert result.get("error") == f"'{field}' must be an address or a list of addresses."
    assert himalaya == []


# ── Explicit From ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sender,addr", [
    ("fleet-001@lucaryin.com", "fleet-001@lucaryin.com"),
    ("Lucaryin Fleet <fleet-001@lucaryin.com>", "fleet-001@lucaryin.com"),
    ('"J. O\'Brien" <j@x.com>', "j@x.com"),
    ("  Ops_Team-2 <ops@x.com>  ", "ops@x.com"),
])
def test_plain_from_is_accepted(himalaya, sender, addr):
    result = _send(to=OWNER, **{"from": sender})
    assert result.get("sent") is True, result
    assert _wire(himalaya[0], "From") == [addr]


@pytest.mark.parametrize("sender", [
    "Owner <a@x.com>\r\nBcc: z@evil.com",
    "a@x.com\n",
    "Owner\n <a@x.com>",
    "Owner\t<a@x.com>",
    "=?utf-8?q?Owner?= <a@x.com>",
    "José <j@x.com>",
    "Smith, John <j@x.com>",
    "a@x.com, b@y.com",
    "Group: a@x.com;",
    "Name <not-an-address>",
    "<a@x.com> trailing",
    "(Owner) a@x.com",
    5,
])
def test_from_that_is_not_a_plain_name_and_address_is_refused(himalaya, sender):
    result = _send(to=OWNER, **{"from": sender})
    assert result.get("error", "").startswith("'from' must be a bare address"), result
    assert himalaya == []


@pytest.mark.parametrize("sender", [None, "", "   "])
def test_absent_from_falls_back_to_the_account_identity(himalaya, monkeypatch, sender):
    monkeypatch.setattr(est, "_account_from", lambda account: "")
    result = _send(to=OWNER, account="fleet", **{"from": sender})
    assert result.get("sent") is True, result
    assert _wire(himalaya[0], "From") == ["fleet-001@lucaryin.com"]
