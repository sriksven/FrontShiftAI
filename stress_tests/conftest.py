"""Shared fixtures for phase stress tests.

These tests hit a *running* backend. Set env:
    STRESS_TEST_BACKEND_URL   (default http://localhost:8000)
    STRESS_TEST_JWT           (access token for a seeded test user)
    STRESS_TEST_JWT_TENANT_B  (optional: access token for a different tenant —
                               needed only by phase-0.6 cross-tenant tests)
"""
from __future__ import annotations

import os
import statistics
from typing import List

import httpx
import pytest
import pytest_asyncio

BACKEND_URL = os.getenv("STRESS_TEST_BACKEND_URL", "http://localhost:8000")
JWT_TOKEN = os.getenv("STRESS_TEST_JWT")
JWT_TOKEN_TENANT_B = os.getenv("STRESS_TEST_JWT_TENANT_B")


@pytest.fixture
def backend_url() -> str:
    return BACKEND_URL


@pytest.fixture
def auth_headers() -> dict:
    if not JWT_TOKEN:
        pytest.skip("Set STRESS_TEST_JWT to run this test")
    return {"Authorization": f"Bearer {JWT_TOKEN}"}


@pytest.fixture
def auth_headers_tenant_b() -> dict:
    if not JWT_TOKEN_TENANT_B:
        pytest.skip("Set STRESS_TEST_JWT_TENANT_B to run cross-tenant tests")
    return {"Authorization": f"Bearer {JWT_TOKEN_TENANT_B}"}


@pytest_asyncio.fixture
async def http_client(auth_headers: dict):
    """Persistent client — also useful for measuring pooling benefit."""
    async with httpx.AsyncClient(
        base_url=BACKEND_URL,
        headers=auth_headers,
        timeout=30.0,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        yield client


class LatencyReport:
    """Collects timings and asserts on p50/p95 targets."""

    def __init__(self, name: str, target_p50: float, target_p95: float):
        self.name = name
        self.target_p50 = target_p50
        self.target_p95 = target_p95
        self.times: List[float] = []

    def record(self, seconds: float) -> None:
        self.times.append(seconds)

    def report(self) -> dict:
        if not self.times:
            return {"count": 0}
        s = sorted(self.times)
        n = len(s)
        stats = {
            "count": n,
            "avg": statistics.mean(s),
            "p50": statistics.median(s),
            "p95": s[min(int(n * 0.95), n - 1)],
            "p99": s[min(int(n * 0.99), n - 1)],
            "min": s[0],
            "max": s[-1],
            "std": statistics.stdev(s) if n > 1 else 0.0,
        }
        print(f"\n{'='*60}\n  {self.name} ({n} samples)\n{'='*60}")
        for k, v in stats.items():
            print(f"  {k:>5}: {v:.3f}" if k != "count" else f"  {k:>5}: {v}")
        p50_ok = stats["p50"] <= self.target_p50
        p95_ok = stats["p95"] <= self.target_p95
        print(f"\n  P50 <= {self.target_p50}s: {'PASS' if p50_ok else 'FAIL'}")
        print(f"  P95 <= {self.target_p95}s: {'PASS' if p95_ok else 'FAIL'}\n{'='*60}\n")
        return stats

    def assert_targets(self) -> None:
        stats = self.report()
        assert stats["p50"] <= self.target_p50, f"p50 {stats['p50']} > {self.target_p50}"
        assert stats["p95"] <= self.target_p95, f"p95 {stats['p95']} > {self.target_p95}"
