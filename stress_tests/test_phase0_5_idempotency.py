"""Phase 0.5 — Idempotency keys.

Tests that POSTing the same body twice with the same Idempotency-Key returns
the cached response the second time (no duplicate writes), and that different
keys produce independent records even for identical bodies.

Requires ``STRESS_TEST_JWT`` for a seeded test user with PTO/HR permissions.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_duplicate_hr_key_returns_cached(http_client):
    """Same Idempotency-Key → same ticket_id, no second HRTicket row."""
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    body = {"message": "Need to discuss benefits options please."}

    r1 = await http_client.post("/api/hr-tickets/chat", json=body, headers=headers)
    r2 = await http_client.post("/api/hr-tickets/chat", json=body, headers=headers)

    assert r1.status_code == r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    # ticket_id may be None if the agent classified it as non-creating —
    # but the two responses must be identical either way.
    assert j1 == j2


@pytest.mark.asyncio
async def test_different_hr_keys_produce_independent_responses(http_client):
    """Different keys on identical bodies must create separate responses."""
    body = {"message": "I need leave advice."}
    r1 = await http_client.post(
        "/api/hr-tickets/chat",
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    r2 = await http_client.post(
        "/api/hr-tickets/chat",
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r1.status_code == r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    if j1.get("ticket_id") and j2.get("ticket_id"):
        assert j1["ticket_id"] != j2["ticket_id"]


@pytest.mark.asyncio
async def test_pto_duplicate_key_single_request(http_client):
    """Same key on PTO chat: the second call must not create a second PTORequest."""
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    body = {"message": "Please request one day off next Friday."}

    r1 = await http_client.post("/api/pto/chat", json=body, headers=headers)
    r2 = await http_client.post("/api/pto/chat", json=body, headers=headers)
    assert r1.status_code == r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    assert j1 == j2
    # If the first call created a request, both responses must cite the SAME id.
    if j1.get("request_id"):
        assert j1["request_id"] == j2["request_id"]


@pytest.mark.asyncio
async def test_no_key_header_still_works(http_client):
    """Idempotency is opt-in. No Idempotency-Key → request proceeds normally."""
    body = {"message": "What is the vacation policy?"}
    r = await http_client.post("/api/chat/message", json=body)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_error_responses_not_cached(http_client):
    """A key used on an attempt that errored must NOT short-circuit a retry."""
    # Hit the unified endpoint with an empty message — the handler catches and
    # returns a graceful "internal_server_error" response. That response must
    # not be cached; a retry should run the pipeline again.
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}

    r1 = await http_client.post("/api/chat/message", json={"message": ""}, headers=headers)
    r2 = await http_client.post(
        "/api/chat/message",
        json={"message": "What is the holiday list?"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # If r1 cached an error, r2 would return the same apology verbatim.
    # After the error-skip fix, r2 should contain a real answer.
    assert r2.json()["agent_used"] != "error" or r1.json()["agent_used"] != "error" or (
        r1.json()["response"] != r2.json()["response"]
    )
