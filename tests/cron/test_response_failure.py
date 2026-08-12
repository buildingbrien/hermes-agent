"""A cron run whose whole response is a runtime failure sentinel is a failure.

The agent swallows some errors (spent API budget, provider outage) and returns
them as its final text rather than raising. The 9:09 Heartbeat on 2026-08-12
returned "API call failed after 3 retries: Connection error." and was marked
'ok' — green board, empty Errored filter, no consecutive-failure alert.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _response_failure_reason


def test_api_failure_sentinel_is_a_failure():
    r = _response_failure_reason("API call failed after 3 retries: Connection error.")
    assert r and "API call failed" in r


def test_openai_call_error_is_a_failure():
    assert _response_failure_reason(
        "Error during OpenAI-compatible API call #1: slice(None, 500, None)"
    )


def test_provider_exhaustion_is_a_failure():
    assert _response_failure_reason("All API providers failed — no credentials left")


def test_normal_response_is_not_a_failure():
    assert _response_failure_reason("Good morning! Your calendar is clear today.") is None


def test_empty_response_is_not_flagged_here():
    # Empty is handled separately (soft failure in the delivery path); this
    # helper only judges non-empty sentinel text.
    assert _response_failure_reason("") is None
    assert _response_failure_reason("   ") is None


def test_only_the_first_line_is_judged():
    # A report that legitimately DISCUSSES an API error in its body, but whose
    # opening line is a normal summary, is not a failed run.
    body = "Heartbeat summary: 2 findings.\n\nThe logs show 'API call failed' warnings worth review."
    assert _response_failure_reason(body) is None


def test_error_word_mid_sentence_is_not_a_failure():
    assert _response_failure_reason("No errors found in today's inbox.") is None
