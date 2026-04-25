"""Phase 0.6 — Multi-tenant isolation.

Mostly unit-ish: the event listener and the TenantScopedRetriever wrapper
can be exercised without a running backend. One full integration test
(cross-tenant HTTP access denial) requires both tenant-A and tenant-B JWTs.
"""
from __future__ import annotations

import pytest


# ---- 0.6D: TenantScopedRetriever rejects missing company -------------------

def test_tenant_scoped_retriever_rejects_empty_company():
    from chat_pipeline.rag.tenant_scoped_retriever import TenantScopedRetriever

    class _FakeCollection:
        def query(self, **_kwargs):
            raise AssertionError("must not be called when company is missing")

    retriever = TenantScopedRetriever(collection=_FakeCollection())

    with pytest.raises(ValueError):
        retriever.query("q", company="", top_k=3)
    with pytest.raises(ValueError):
        retriever.query("q", company=None, top_k=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        retriever.query("q", company="   ", top_k=3)


def test_tenant_scoped_retriever_forces_where_clause():
    from chat_pipeline.rag.tenant_scoped_retriever import TenantScopedRetriever

    calls = []

    class _FakeCollection:
        def query(self, **kwargs):
            calls.append(kwargs)
            return {"documents": [[]], "metadatas": [[]]}

    retriever = TenantScopedRetriever(collection=_FakeCollection())
    retriever.query("q", company="AcmeCorp", top_k=5)
    assert calls, "underlying collection.query not invoked"
    where = calls[0]["where"]
    assert "company" in str(where)


# ---- 0.6A: bypass_tenant_filter emits an audit log line --------------------

def test_bypass_tenant_filter_emits_audit_log(caplog):
    import logging
    from backend.db.tenant_context import bypass_tenant_filter

    caplog.set_level(logging.WARNING, logger="backend.db.tenant_context")
    with bypass_tenant_filter(reason="unit test audit", actor="tests@example.com"):
        pass

    audit_records = [
        r for r in caplog.records if "TENANT FILTER BYPASS" in r.getMessage()
    ]
    assert audit_records, "bypass_tenant_filter should produce a WARNING audit line"


# ---- 0.6B: integration — cross-tenant access is denied ---------------------

@pytest.mark.asyncio
async def test_cross_tenant_conversation_access_denied(
    backend_url, auth_headers, auth_headers_tenant_b
):
    """Tenant B cannot read Tenant A's conversation list."""
    import httpx

    async with httpx.AsyncClient(base_url=backend_url, timeout=15) as client:
        # Tenant A creates a conversation
        r = await client.post(
            "/api/chat/message",
            json={"message": "What is the vacation policy?"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        conversation_id = r.json()["conversation_id"]

        # Tenant B asks for it — must come back empty (scoped query).
        listing = await client.get(
            "/api/chat/conversations",
            headers=auth_headers_tenant_b,
        )
        assert listing.status_code == 200
        assert all(c["id"] != conversation_id for c in listing.json())
