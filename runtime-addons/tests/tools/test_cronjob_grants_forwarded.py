"""Lucaryin fold patch 0012 (runtime-patches/0012-cronjob-grants.patch): a
standing grant the MODEL requests through the ``cronjob_manage`` tool must
reach the stored job.

Bug-hunt follow-up F2 (2026-09-22): 0012 added ``grants`` to ``cronjob()``,
its schema, the create path and the update path — but not to upstream's
``_HANDLER_FORWARDED_ARGS``, the allowlist ``_cronjob_handler`` uses to copy
model arguments into ``cronjob()``. Every model tool call therefore dropped
``grants`` on the floor: the job was created WITHOUT the standing grant, the
scheduler's ``cron_gate.install(grants=job["grants"])`` saw ``[]``, and every
unattended run re-asked for the action the user had already approved once.
Direct ``cronjob()`` callers (the only thing 0012 was exercised through) were
fine, which is why nothing caught it.

Bare-string follow-up (2026-09-22): ``_validate_grants`` only applied the
email_send/message_send 'to'-allowlist rule to DICT grants, so ``["email_send"]``
or ``["message_send"]`` (or ``grants: "email_send"``, which schema coercion
wraps into that list) was stored with no recipients. The bridge gate honours a
bare "message_send" for ANY recipient — the "may message anyone" grant the rule
exists to refuse. Both forms must now be refused, as the no-'to' object is.
The same review found more shapes the gate would never honour: 'to' values it
cannot match ("*", non-strings) and meeting_schedule (the gate lists it as
ungrantable). Both are refused on create and on update.

outbound_call is NOT recipient-scoped (founder design, v4.6.43 standing
grants): it needs explicit unattended:true, the outbound contact policy still
clamps every call, and its 'to' is optional. A 'to' that IS given must list
exact E.164 numbers: the gate matches "self" only against the owner's EMAIL
addresses and "@domain" by suffix, so neither can ever cover a dialled number.
For the same reason message_send refuses "self" (message recipients are
numbers/chat ids, not the owner's email); email_send keeps it.

send_message targets (2026-09-22, with the bridge's _send_message_target):
the gate now covers send_message(target=...) when the grant's 'to' names the
exact "platform:ref", both sides normalised the way send_message splits a
target (first ':', platform lowercased, both halves stripped). A topic
("telegram:X:17") is its own target, and a bare platform (the home channel) is
never covered, so a 'to' entry of just a platform is refused.

Entries the gate stores but never matches (2026-09-22 review): the gate's
_allowlist_entries splits an entry on , ; or a line break only when every piece
is an email address or "@domain", drops an angle-bracket entry that is not an
email address, and drops a "@..." entry that is not a dotted DNS domain (so a
Matrix user id "@alice:matrix.org" covers nothing). email_send recipients reach
it as parsed bare addresses, so any other email_send entry never matches. Each
of those passed validation before; they are now refused with one recipient per
entry, bare addresses for email_send, and "matrix:@user:server" for Matrix.

Bare tier: a temp HERMES_HOME and the REAL ``cron.jobs`` store, dispatched
through the registered handler exactly as a model tool call is — no mocks on
the path the bug lived on.
"""

from __future__ import annotations

import json

import pytest

from cron.jobs import load_jobs
from tools import cronjob_tools
from tools.arg_coercion import coerce_tool_args
from tools.cronjob_tools import _HANDLER_FORWARDED_ARGS, _cronjob_handler

GRANT = {
    "action": "email_send",
    "to": ["person@example.com"],
    "max_per_run": 1,
    "max_per_day": 4,
}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME — cron.jobs resolves its store from the live
    get_hermes_home(), so the env var is enough to keep writes out of
    ~/.hermes."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # create/update outside an interactive session is fine for the store; keep
    # any session-context lookups from reaching real state.
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    return home


def _stored(job_id: str) -> dict:
    raw = next((j for j in load_jobs() if j.get("id") == job_id), None)
    assert raw is not None, f"job {job_id} not in the store"
    return raw


def _create_via_handler(**extra) -> dict:
    args = {
        "action": "create",
        "name": "weekly digest",
        "prompt": "Summarize the week's inbox and email it to person@example.com.",
        "schedule": "every 1d",
        "deliver": "local",
        **extra,
    }
    result = json.loads(_cronjob_handler(args, task_id="t-1", session_id="s-1"))
    assert result.get("success") is True, result
    return result


def test_grants_is_a_forwarded_handler_arg():
    assert "grants" in _HANDLER_FORWARDED_ARGS
    # And the registry entry dispatches through that same handler.
    assert "grants" in cronjob_tools.CRONJOB_SCHEMA["parameters"]["properties"]


def test_model_create_with_grants_stores_them(hermes_home):
    result = _create_via_handler(grants=[GRANT])
    job_id = result["job_id"]
    assert _stored(job_id)["grants"] == [GRANT]


def test_model_create_without_grants_stores_empty_list(hermes_home):
    job_id = _create_via_handler()["job_id"]
    assert _stored(job_id)["grants"] == []


def test_model_update_replaces_grants(hermes_home):
    job_id = _create_via_handler(grants=[GRANT])["job_id"]
    narrower = {**GRANT, "max_per_day": 1}
    result = json.loads(_cronjob_handler(
        {"action": "update", "job_id": job_id, "grants": [narrower]},
        task_id="t-1", session_id="s-1"))
    assert result.get("success") is True, result
    assert _stored(job_id)["grants"] == [narrower]


def test_model_update_without_grants_keeps_them(hermes_home):
    """An update that doesn't mention grants must not clear the standing
    approval (forwarding None, not [])."""
    job_id = _create_via_handler(grants=[GRANT])["job_id"]
    result = json.loads(_cronjob_handler(
        {"action": "update", "job_id": job_id, "name": "renamed digest"},
        task_id="t-1", session_id="s-1"))
    assert result.get("success") is True, result
    stored = _stored(job_id)
    assert stored["name"] == "renamed digest"
    assert stored["grants"] == [GRANT]


def test_model_create_with_ungrantable_action_is_refused(hermes_home):
    """With grants now forwarded, 0012's validator actually runs on model
    calls: payment is never pre-authorizable and nothing is stored."""
    result = json.loads(_cronjob_handler({
        "action": "create",
        "name": "pay invoices",
        "prompt": "Pay the open invoices.",
        "schedule": "every 1d",
        "deliver": "local",
        "grants": [{"action": "payment_execute"}],
    }, task_id="t-1", session_id="s-1"))
    assert result.get("success") is False, result
    assert "payment_execute" in json.dumps(result)
    assert load_jobs() == []


# ── Recipient rule: bare-string email/message grants ─────────────────────────

_CREATE = {
    "action": "create",
    "name": "weekly digest",
    "prompt": "Summarize the week's inbox and email it to person@example.com.",
    "schedule": "every 1d",
    "deliver": "local",
}


def _call(args: dict) -> dict:
    return json.loads(_cronjob_handler(args, task_id="t-1", session_id="s-1"))


@pytest.mark.parametrize("grant", [
    "email_send",                   # bare string: no place to put a 'to'
    "message_send",                 # bridge honours this for ANY recipient
    {"action": "email_send"},       # object without 'to' (already refused)
    {"action": "message_send", "to": []},
])
def test_recipient_less_email_or_message_grant_is_refused(hermes_home, grant):
    result = _call({**_CREATE, "grants": [grant]})
    assert result.get("success") is False, result
    assert "'to' allowlist" in json.dumps(result)
    assert load_jobs() == []


@pytest.mark.parametrize("to", [
    "*", ["*"], [""], ["  "], [], 5, True, {"a": 1},
    ["@"], ["*@"], ["*.com"], ["*@*.com"], ["ok@example.com", "*"],
])
def test_to_the_gate_cannot_match_is_refused(hermes_home, to):
    """The gate only matches "self", "@domain"/"*@domain" or an exact recipient.
    A bare "*" never matches, yet the card reads "send email to *" ("anyone").
    A non-string 'to' makes the gate's matcher raise. Either way the user
    approves a card and then every run blocks."""
    result = _call({**_CREATE, "grants": [{"action": "email_send", "to": to}]})
    assert result.get("success") is False, result
    assert load_jobs() == []


@pytest.mark.parametrize("grant", [
    {"action": "email_send", "to": "person@example.com"},  # gate wraps a str
    {"action": "email_send", "to": ["@partner.com", "*@vendor.io", "self"]},
    {"action": "message_send", "to": ["+15551230000"]},
    {"action": "outbound_call", "unattended": True, "to": ["+15551230000"]},
])
def test_named_recipient_grants_are_accepted(hermes_home, grant):
    job_id = _create_via_handler(grants=[grant])["job_id"]
    assert _stored(job_id)["grants"] == [grant]


# ── outbound_call: unattended:true required, 'to' optional ───────────────────

def test_unattended_outbound_call_without_to_is_accepted(hermes_home):
    """Founder design (v4.6.43): unattended:true is the bar for outbound_call,
    not a 'to' allowlist; the outbound contact policy still clamps each call."""
    grant = {"action": "outbound_call", "unattended": True}
    job_id = _create_via_handler(grants=[grant])["job_id"]
    assert _stored(job_id)["grants"] == [grant]


def test_update_to_unattended_outbound_call_without_to_is_accepted(hermes_home):
    job_id = _create_via_handler(grants=[GRANT])["job_id"]
    grant = {"action": "outbound_call", "unattended": True, "max_per_day": 2}
    result = _call({"action": "update", "job_id": job_id, "grants": [grant]})
    assert result.get("success") is True, result
    assert _stored(job_id)["grants"] == [grant]


@pytest.mark.parametrize("to", [
    # The gate and the card both treat a falsy 'to' as absent ("any number"),
    # but stored as [] / "" / null it reads as "nobody". Refused so the stored
    # shape is unambiguous; the error tells the model to omit 'to' instead.
    [], "", None,
    "*", ["*"], [""], 5, {"n": "+15551230000"}, ["+15551230000", "*"],
    # The gate matches "self" only against the owner's email addresses and
    # "@domain" by suffix, so neither ever covers a dialled number: the user
    # would approve a grant under which every held call is blocked.
    ["self"], "self", ["SELF"], ["@acme.com"], ["*@acme.com"],
    ["owner@acme.com"], ["+15551230000", "self"],
    # Not E.164: the gate compares the number literally, so only the canonical
    # form is accepted.
    ["+1 555 123 0000"], ["15551230000"], ["+0155512300"], ["+123"],
    ["+1234567890123456"], ["+1555123000x"], ["+１５５５１２３００００"],
])
def test_outbound_call_with_malformed_to_is_refused(hermes_home, to):
    grant = {"action": "outbound_call", "unattended": True, "to": to}
    result = _call({**_CREATE, "grants": [grant]})
    assert result.get("success") is False, result
    assert "outbound_call grant's 'to' must be" in result["error"]
    assert load_jobs() == []


def test_outbound_call_to_error_says_to_is_optional_and_offers_no_self():
    """The refusal must not read as 'to' being required, and must not offer
    "self"/"@domain" as a fix (both are accepted nowhere for outbound_call)."""
    error = cronjob_tools._validate_grants(
        [{"action": "outbound_call", "unattended": True, "to": None}])
    assert "'to' is optional for outbound_call: omit it" in error
    assert "E.164" in error
    assert '"self" (the owner)' not in error
    assert "never match a dialled number" in error


@pytest.mark.parametrize("to", [
    ["+15551230000"], "+15551230000", ["+442071838750", "+15551230000"],
    ["+1234567"], ["+123456789012345"], [" +15551230000 "],
])
def test_outbound_call_with_e164_to_is_accepted(hermes_home, to):
    grant = {"action": "outbound_call", "unattended": True, "to": to}
    job_id = _create_via_handler(grants=[grant])["job_id"]
    assert _stored(job_id)["grants"] == [grant]


@pytest.mark.parametrize("to", [["self"], "self", ["Self"], ["+15551230000", "self"]])
def test_message_send_to_self_is_refused(hermes_home, to):
    """The gate's "self" is the owner's email addresses; a message to the
    owner's phone number or chat id never matches it."""
    result = _call({**_CREATE, "grants": [{"action": "message_send", "to": to}]})
    assert result.get("success") is False, result
    assert '"self" is not accepted here' in result["error"]
    assert load_jobs() == []


@pytest.mark.parametrize("grant", [
    {"action": "message_send", "to": ["@example.com"]},
    {"action": "message_send", "to": ["owner@example.com", "123456789"]},
    {"action": "email_send", "to": ["SELF"]},
])
def test_message_send_named_and_email_self_are_accepted(hermes_home, grant):
    job_id = _create_via_handler(grants=[grant])["job_id"]
    assert _stored(job_id)["grants"] == [grant]


@pytest.mark.parametrize("grant", [
    "outbound_call",                                   # bare string
    {"action": "outbound_call"},
    {"action": "outbound_call", "to": ["+15551230000"]},
    {"action": "outbound_call", "unattended": False, "to": ["+15551230000"]},
    {"action": "outbound_call", "unattended": "true"},  # must be the bool
])
def test_outbound_call_without_unattended_true_is_refused(hermes_home, grant):
    result = _call({**_CREATE, "grants": [grant]})
    assert result.get("success") is False, result
    assert "explicit unattended:true" in result["error"]
    # The refusal no longer tells the model a 'to' allowlist is required.
    assert "'to'" not in result["error"]
    assert load_jobs() == []


def test_schema_describes_outbound_call_to_as_optional():
    text = cronjob_tools.CRONJOB_SCHEMA["parameters"]["properties"]["grants"]["description"]
    assert "outbound_call must be an object with explicit unattended:true" in text
    assert "its 'to' is optional (omit it to cover any number)" in text
    assert "exact E.164 phone numbers" in text
    # "self" is offered for email_send only, never for calls or messages.
    assert 'email_send also accepts "self" (the owner)' in text
    assert "follows the same rules when given" not in text


@pytest.mark.parametrize("grant", ["meeting_schedule", {"action": "meeting_schedule"}])
def test_meeting_schedule_is_ungrantable(hermes_home, grant):
    """The gate lists meeting_schedule in UNGRANTABLE_ACTIONS (it mints an
    outbound_call grant), so a stored grant for it could never be honoured."""
    result = _call({**_CREATE, "grants": [grant]})
    assert result.get("success") is False, result
    assert "can never be a standing grant" in json.dumps(result)
    assert load_jobs() == []


def test_message_send_guidance_describes_send_message_target_coverage(hermes_home):
    """The gate now reads send_message's 'target' too, normalised to
    "platform:ref" (approval_gate._send_message_target), and covers it only
    when the grant's 'to' names that exact target. The error and the schema
    must say so, including that a topic is a separate target and that a bare
    platform (its home channel) is never covered."""
    result = _call({**_CREATE, "grants": ["message_send"]})
    error = result["error"]
    assert "not covered and still gates" not in error
    assert "send_message(target=...) is covered only when 'to' names" in error
    assert '"platform:chat_id"' in error
    assert '"telegram:-1001234567890:17" is a different target' in error
    assert "a bare platform (its home channel) is never covered" in error
    schema_text = cronjob_tools.CRONJOB_SCHEMA["parameters"]["properties"]["grants"]["description"]
    assert "not send_message's 'target'" not in schema_text
    assert ("covers send_message only when its 'to' names the exact target "
            'as "platform:chat_id"') in schema_text
    assert "a bare platform, i.e. its home channel, is never covered" in schema_text


@pytest.mark.parametrize("to", [
    ["telegram:-1001234567890"],
    "telegram:-1001234567890",
    # The gate normalises the entry the way send_message splits a target
    # (first ':', platform lowercased, both halves stripped).
    [" Telegram : -1001234567890 "],
    ["telegram:-1001234567890:17"],          # a topic, named exactly
    ["discord:123456789", "+15551230000"],
])
def test_message_send_to_a_send_message_target_is_accepted(hermes_home, to):
    grant = {"action": "message_send", "to": to}
    job_id = _create_via_handler(grants=[grant])["job_id"]
    assert _stored(job_id)["grants"] == [grant]


@pytest.mark.parametrize("to", [
    # send_message's home-channel forms: the gate cannot see which chat that
    # is, so no grant ever covers it and the approved grant would never match.
    ["telegram"], "Telegram", [" SLACK "], ["sms"],
    ["telegram:"], ["telegram: "], ["+15551230000", "discord"],
])
def test_message_send_to_a_bare_platform_is_refused(hermes_home, to):
    result = _call({**_CREATE, "grants": [{"action": "message_send", "to": to}]})
    assert result.get("success") is False, result
    assert "means its home channel, which a grant never covers" in result["error"]
    assert load_jobs() == []


def test_bare_platform_names_are_only_refused_for_message_send(hermes_home):
    """The home-channel rule is message-only. email_send refuses "telegram"
    too, but because it is not an address, so its error must not send the
    model looking for a chat id."""
    assert cronjob_tools._to_entry_ok("message_send", "telegram") is False
    assert cronjob_tools._to_entry_ok("message_send", "general") is True
    error = cronjob_tools._validate_grants([{"action": "email_send", "to": ["telegram"]}])
    assert "exact bare addresses" in error
    assert "home channel" not in error


@pytest.mark.parametrize("grant", [
    {"action": "message_send"},
    {"action": "email_send", "to": ["*"]},
    {"action": "outbound_call", "unattended": True, "to": ["*"]},
    {"action": "outbound_call", "to": ["+15551230000"]},
    "meeting_schedule",
])
def test_update_applies_the_same_recipient_and_ungrantable_rules(hermes_home, grant):
    """Update keeps the standing approval and the gate does not repeat the
    recipient rule or the 'to' check, so skipping validation here would store
    an 'anyone' grant or one every run blocks on."""
    job_id = _create_via_handler(grants=[GRANT])["job_id"]
    result = _call({"action": "update", "job_id": job_id, "grants": [grant]})
    assert result.get("success") is False, result
    assert _stored(job_id)["grants"] == [GRANT]


def test_bare_string_grants_arg_is_refused_after_schema_coercion(hermes_home):
    """grants: "email_send" (a string, not a list) is what the model emits when
    it skips the object form; the runtime's coerce_tool_args wraps it into
    ["email_send"] before the handler, so the handler must refuse THAT."""
    args = coerce_tool_args("cronjob_manage", {**_CREATE, "grants": "email_send"})
    assert args["grants"] == ["email_send"]
    result = _call(args)
    assert result.get("success") is False, result
    assert load_jobs() == []


def test_update_to_a_bare_string_grant_is_refused_and_keeps_the_old_grant(hermes_home):
    job_id = _create_via_handler(grants=[GRANT])["job_id"]
    result = _call({"action": "update", "job_id": job_id, "grants": ["email_send"]})
    assert result.get("success") is False, result
    assert _stored(job_id)["grants"] == [GRANT]


def test_owner_only_email_grant_is_accepted(hermes_home):
    """The narrowest email grant in the bridge's own vocabulary ('self' = the
    principal addresses) is the fix the error message points the model at."""
    owner_only = {"action": "email_send", "to": ["self"], "max_per_run": 1}
    job_id = _create_via_handler(grants=[owner_only])["job_id"]
    assert _stored(job_id)["grants"] == [owner_only]


def test_bare_string_non_recipient_grant_is_still_accepted(hermes_home):
    """The bridge treats a bare string and a no-'to' object identically for
    actions without a recipient rule, so the bare form stays valid there."""
    job_id = _create_via_handler(grants=["file_write"])["job_id"]
    assert _stored(job_id)["grants"] == ["file_write"]


def test_non_string_action_is_malformed_not_a_crash(hermes_home):
    result = _call({**_CREATE, "grants": [{"action": ["email_send"], "to": ["self"]}]})
    assert result.get("success") is False, result
    assert "malformed grant" in json.dumps(result)
    assert load_jobs() == []


# ── 'to' entries the gate stores but never matches ───────────────────────────

@pytest.mark.parametrize("to", [
    "+15551230000, +15559870000",
    ["+15551230000; +15559870000"],
    ["telegram:-100, telegram:-200"],
    ["+15551230000\n+15559870000"],
    ["+15551230000\r+15559870000"],
    ["Bob <+15551230000>"],
    ["<telegram:-100>"],
    # The gate would parse these two, but one recipient per entry with no
    # display name is the single rule the error can state.
    ["Bob <bob@example.com>"],
    ["bob@example.com, carol@example.com"],
])
def test_message_send_list_or_display_name_entry_is_refused(hermes_home, to):
    """The gate keeps "+1555..., +1555..." as ONE literal recipient (it splits
    only all-email lists) and drops "Bob <+1555...>", so an approved grant with
    either would gate every unattended send."""
    result = _call({**_CREATE, "grants": [{"action": "message_send", "to": to}]})
    assert result.get("success") is False, result
    assert "one per entry: no comma or semicolon lists" in result["error"]
    assert load_jobs() == []


@pytest.mark.parametrize("act", ["email_send", "message_send"])
@pytest.mark.parametrize("entry", [
    "@alice:matrix.org",               # a Matrix user id: the gate reads a domain
    "@localhost", "*@localhost",       # no dot
    "@partner.com.", "@.partner.com", "@partner..com",
    "@-partner.com", "@partner-.com", "@part_ner.com",
    "@ partner.com", "@pärtner.com",
    "@partner.com, @vendor.io",
])
def test_domain_entry_the_gate_drops_is_refused(hermes_home, act, entry):
    result = _call({**_CREATE, "grants": [{"action": act, "to": [entry]}]})
    assert result.get("success") is False, result
    assert 'with a dotted domain such as "@partner.com"' in result["error"]
    assert load_jobs() == []


def test_matrix_user_guidance_points_at_the_send_message_target(hermes_home):
    result = _call({**_CREATE, "grants": [
        {"action": "message_send", "to": ["@alice:matrix.org"]}]})
    assert 'name a Matrix user as the send_message target "matrix:@user:server"' \
        in result["error"]
    text = cronjob_tools.CRONJOB_SCHEMA["parameters"]["properties"]["grants"]["description"]
    assert 'a Matrix user is "matrix:@user:server"' in text
    assert 'one per entry (no comma lists or "Name <...>" forms)' in text


@pytest.mark.parametrize("entry", [
    "bob",                                 # a name, not an address
    "Bob <bob@example.com>",               # the gate would parse it; bare only
    "bob@example.com, carol@example.com",
    "bob@localhost", "bob..x@example.com", ".bob@example.com",
    "bob%evil.com@partner.com",            # %-routing: the gate never parses it
    '"bob"@example.com',
])
def test_email_send_entry_that_is_not_one_bare_address_is_refused(hermes_home, entry):
    """email_send recipients reach the gate as parsed bare addresses, so an
    entry that is not one covers nothing."""
    result = _call({**_CREATE, "grants": [{"action": "email_send", "to": [entry]}]})
    assert result.get("success") is False, result
    assert "exact bare addresses (one per entry" in result["error"]
    assert load_jobs() == []


@pytest.mark.parametrize("grant", [
    {"action": "message_send", "to": ["matrix:@alice:matrix.org"]},
    {"action": "message_send", "to": ["whatsapp:15551230000@s.whatsapp.net"]},
    {"action": "message_send", "to": ["@Partner.COM", "*@mail.vendor.io"]},
    # Real shapes from stored jobs: mixed-case bare addresses.
    {"action": "email_send", "to": ["Brien@Example.com", "first_last2@example.com"]},
    {"action": "email_send", "to": ["first.last+tag@sub.example.co", "@xn--bcher-kva.example"]},
])
def test_entries_the_gate_matches_are_accepted(hermes_home, grant):
    job_id = _create_via_handler(grants=[grant])["job_id"]
    assert _stored(job_id)["grants"] == [grant]


# Copied from lucaryin-ai hermes-bridge/approval_gate.py (_DOMAIN_RE,
# _EMAIL_ADDR_RE). cronjob_tools mirrors them without `re`; if the gate's
# patterns change, update both.
_GATE_DOMAIN = r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_GATE_EMAIL = r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*@" + _GATE_DOMAIN


def test_regex_free_mirrors_agree_with_the_gate_patterns():
    import random
    import re

    domain_re, email_re = re.compile(_GATE_DOMAIN), re.compile(_GATE_EMAIL)
    rng = random.Random(20260922)
    alphabet = "ab0-._+@:%ä "
    for _ in range(20000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
        assert cronjob_tools._is_domain(s) == bool(domain_re.fullmatch(s)), s
        assert cronjob_tools._is_bare_email(s) == bool(email_re.fullmatch(s)), s
