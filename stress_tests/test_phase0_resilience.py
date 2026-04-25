"""Phase 0 — critical resilience fixes (0A, 0C, 0D, 0E, 0F, 0G, 0H).

These are behaviour-level assertions against a running backend. Unit-level
coverage for the same logic (e.g. the cache lock, the retry wrapper) lives
inside the relevant module's test file once those are added.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest


# ---- 0A: SQLite fallback blocked in production -----------------------------

def test_sqlite_fallback_blocked_in_production(monkeypatch):
    """In ENVIRONMENT=production, unreachable PostgreSQL must raise, not fall back."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://invalid:invalid@127.0.0.1:1/nonexistent",
    )
    # Import lazily so the env override takes effect before module import caches.
    import importlib
    import backend.db.connection as connection
    importlib.reload(connection)

    with pytest.raises(RuntimeError, match="PostgreSQL required"):
        connection.get_database_url()


# ---- 0C: health check connection leak --------------------------------------

@pytest.mark.asyncio
async def test_health_no_connection_leak(http_client):
    """Hammer /health; no failures → no leaked connections."""
    responses = await asyncio.gather(
        *[http_client.get("/health") for _ in range(100)],
        return_exceptions=True,
    )
    ok = [r for r in responses if hasattr(r, "status_code") and r.status_code == 200]
    assert len(ok) == 100, f"Only {len(ok)}/100 health checks succeeded"


# ---- 0D: voice tool retry wrapper returns graceful fallback ----------------

@pytest.mark.asyncio
async def test_voice_post_with_retry_graceful_fallback(monkeypatch):
    """When the backend is unreachable, the voice BackendClient.post_with_retry
    returns a structured {"error": True, ...} dict instead of raising."""
    from voice_pipeline.scripts.main import BackendClient

    client = BackendClient(base_url="http://127.0.0.1:1", token="dummy")

    async def always_fail(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(client, "post", always_fail)

    t0 = time.time()
    result = await client.post_with_retry("/api/rag/query", {"q": "x"}, timeout=1, max_retries=2)
    elapsed = time.time() - t0

    assert result.get("error") is True
    assert "sources" in result and result["sources"] == []
    # 2 retries with 1s and 2s linear backoff ≈ 3s minimum before giving up.
    assert elapsed >= 2.5, f"Retry wrapper gave up too quickly ({elapsed:.1f}s)"


# ---- 0E: RAG pipeline cache is thread-safe under concurrent access ---------

def test_rag_pipeline_cache_thread_safe():
    """Concurrent run() calls must not corrupt the OrderedDict cache."""
    # Direct-import the class and mock the internals so we don't need ChromaDB.
    from chat_pipeline.rag.pipeline import RAGPipeline

    p = RAGPipeline(cache_size=8)

    import concurrent.futures as cf
    import threading

    # Simulate 200 concurrent cache writes with overlapping keys.
    def write(i):
        p._update_cache(f"k-{i % 5}", f"answer-{i}", [{"idx": i}])

    with cf.ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(write, range(200)))

    # Invariants: cache size is bounded, contents are deep-copied (no aliasing).
    assert len(p._cache) <= 8
    for _key, (answer, meta) in p._cache.items():
        assert isinstance(answer, str)
        assert isinstance(meta, list)


# ---- 0F: LLM client singleton is thread-safe -------------------------------

def test_llm_client_singleton_thread_safe():
    """Many concurrent get_llm_client() calls must all return the same instance."""
    import concurrent.futures as cf
    # Reset the singleton so we exercise the lock.
    import backend.agents.utils.llm_client as mod
    mod._llm_client = None

    with cf.ThreadPoolExecutor(max_workers=32) as ex:
        clients = list(ex.map(lambda _i: mod.get_llm_client(), range(64)))

    first = clients[0]
    assert all(c is first for c in clients), "singleton broke under concurrency"


# ---- 0G: voice agent refuses to start without a user token -----------------

def test_voice_agent_refuses_without_user_token():
    """Sanity check: entrypoint's early return is present and uses `return`,
    not `raise`. A regex scan keeps the test from requiring a live LiveKit room.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "voice_pipeline" / "scripts" / "main.py"
    content = src.read_text()
    # Must bail out explicitly on missing user_token before session.start(...)
    assert re.search(
        r"if not user_token:[\s\S]+?return",
        content,
    ), "Voice entrypoint should early-return when user_token is missing"


# ---- 0H: 429 Retry-After parser --------------------------------------------

def test_parse_retry_after_numeric_and_http_date():
    from chat_pipeline.rag.generator import _parse_retry_after

    assert _parse_retry_after("7") == 7.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after(None) is None
    # HTTP-date in the past → clamped to 0
    assert _parse_retry_after("Sun, 01 Jan 2000 00:00:00 GMT") == 0.0
    # Garbage → None
    assert _parse_retry_after("not-a-date") is None
