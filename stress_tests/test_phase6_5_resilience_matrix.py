"""Phase 6.5 — Resilience policy matrix + @resilient decorator unit tests.

These don't need a running backend; they exercise the in-process module.
"""
from __future__ import annotations

import asyncio
import time

import pytest


# ---- Policy values match the matrix ----------------------------------------

@pytest.mark.parametrize(
    "name,timeout,retries",
    [
        ("external_llm", 8.0, 3),
        ("external_search", 5.0, 2),
        ("internal_db", 2.0, 1),
        ("voice_tool", 8.0, 2),
        ("user_facing_http", 10.0, 0),
        ("gcs_sync", 300.0, 3),
    ],
)
def test_policy_values_match_matrix(name, timeout, retries):
    from backend.utils.resilience import get_policy

    pol = get_policy(name)
    assert pol.timeout_s == timeout
    assert pol.max_retries == retries


def test_unknown_policy_raises():
    from backend.utils.resilience import get_policy

    with pytest.raises(KeyError):
        get_policy("does_not_exist")


# ---- @resilient retries and eventually raises ------------------------------

def test_sync_resilient_retries_then_raises():
    from backend.utils.resilience import resilient, reset_breakers_for_tests

    reset_breakers_for_tests()
    calls = {"n": 0}

    @resilient(policy="external_search", breaker_key="test-search-A")
    def flaky():
        calls["n"] += 1
        raise RuntimeError("boom")

    t0 = time.time()
    with pytest.raises(RuntimeError):
        flaky()
    # external_search: max_retries=2 → 3 total attempts.
    assert calls["n"] == 3
    # Two sleeps between three attempts (exp base 1s, jittered ±20%).
    assert time.time() - t0 >= 1.5


# ---- Circuit breaker opens after threshold failures ------------------------

def test_breaker_opens_after_threshold():
    from backend.utils.resilience import (
        CircuitOpenError,
        breaker_states,
        reset_breakers_for_tests,
        resilient,
    )

    reset_breakers_for_tests()

    @resilient(policy="external_search", breaker_key="test-search-B")
    def always_fail():
        raise RuntimeError("nope")

    # First call: 3 attempts → 3 failures → breaker opens after this call.
    with pytest.raises(RuntimeError):
        always_fail()

    # Next call should short-circuit immediately with CircuitOpenError.
    with pytest.raises(CircuitOpenError):
        always_fail()

    states = breaker_states()
    assert states.get("test-search-B") == "open"


# ---- Async variant ----------------------------------------------------------

def test_async_resilient_retries():
    from backend.utils.resilience import resilient, reset_breakers_for_tests

    reset_breakers_for_tests()
    calls = {"n": 0}

    @resilient(policy="voice_tool", breaker_key=None)  # voice_tool has no breaker
    async def flaky():
        calls["n"] += 1
        raise RuntimeError("blip")

    with pytest.raises(RuntimeError):
        asyncio.get_event_loop().run_until_complete(flaky())

    # voice_tool: max_retries=2 → 3 total.
    assert calls["n"] == 3


# ---- Success clears the breaker --------------------------------------------

def test_breaker_closes_on_success():
    from backend.utils.resilience import (
        breaker_states,
        reset_breakers_for_tests,
        resilient,
    )

    reset_breakers_for_tests()
    state = {"fail_times": 2}

    @resilient(policy="external_search", breaker_key="test-search-C")
    def recovering():
        if state["fail_times"] > 0:
            state["fail_times"] -= 1
            raise RuntimeError("transient")
        return "ok"

    # external_search: 3 attempts; after 2 failures + 1 success → success.
    assert recovering() == "ok"
    assert breaker_states().get("test-search-C") == "closed"
