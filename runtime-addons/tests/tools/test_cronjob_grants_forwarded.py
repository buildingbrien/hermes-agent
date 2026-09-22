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

Bare tier: a temp HERMES_HOME and the REAL ``cron.jobs`` store, dispatched
through the registered handler exactly as a model tool call is — no mocks on
the path the bug lived on.
"""

from __future__ import annotations

import json

import pytest

from cron.jobs import load_jobs
from tools import cronjob_tools
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
