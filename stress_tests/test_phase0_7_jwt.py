"""Phase 0.7 — JWT access + refresh token lifecycle.

Needs:
    STRESS_TEST_BACKEND_URL
    STRESS_TEST_LOGIN_EMAIL / STRESS_TEST_LOGIN_PASSWORD   — a valid user
"""
from __future__ import annotations

import os
import time

import httpx
import jwt
import pytest


LOGIN_EMAIL = os.getenv("STRESS_TEST_LOGIN_EMAIL")
LOGIN_PASSWORD = os.getenv("STRESS_TEST_LOGIN_PASSWORD")
BACKEND_URL = os.getenv("STRESS_TEST_BACKEND_URL", "http://localhost:8000")


def _login() -> dict:
    if not (LOGIN_EMAIL and LOGIN_PASSWORD):
        pytest.skip("Set STRESS_TEST_LOGIN_EMAIL/PASSWORD for 0.7 tests")
    with httpx.Client(base_url=BACKEND_URL, timeout=15) as c:
        r = c.post("/api/auth/login", json={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD})
        r.raise_for_status()
        return r.json()


def test_access_token_ttl_is_one_hour():
    data = _login()
    payload = jwt.decode(data["access_token"], options={"verify_signature": False})
    ttl = payload["exp"] - time.time()
    # 1h ± a few seconds
    assert 3400 <= ttl <= 3700, f"Access token TTL {ttl:.0f}s should be ~3600"


def test_login_returns_refresh_token():
    data = _login()
    assert data.get("refresh_token"), "login response missing refresh_token"
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 3600


def test_refresh_rotates_and_revokes_old():
    data = _login()
    old_refresh = data["refresh_token"]

    with httpx.Client(base_url=BACKEND_URL, timeout=15) as c:
        r2 = c.post("/api/auth/refresh", json={"refresh_token": old_refresh})
        assert r2.status_code == 200
        new_refresh = r2.json()["refresh_token"]
        assert new_refresh != old_refresh

        # Replay of the revoked refresh must fail AND burn the chain.
        r3 = c.post("/api/auth/refresh", json={"refresh_token": old_refresh})
        assert r3.status_code == 401

        # The freshly-rotated token should now also be dead (chain burn).
        r4 = c.post("/api/auth/refresh", json={"refresh_token": new_refresh})
        assert r4.status_code == 401


def test_logout_revokes_refresh():
    data = _login()
    refresh = data["refresh_token"]
    with httpx.Client(base_url=BACKEND_URL, timeout=15) as c:
        out = c.post("/api/auth/logout", json={"refresh_token": refresh})
        assert out.status_code == 200
        after = c.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert after.status_code == 401


def test_expired_access_token_rejected():
    from jwt import encode
    secret = os.getenv("JWT_SECRET_KEY")
    if not secret:
        pytest.skip("Set JWT_SECRET_KEY to sign an expired token for this test")
    expired = encode(
        {"sub": "tests@example.com", "role": "user", "exp": int(time.time()) - 10},
        secret,
        algorithm="HS256",
    )
    with httpx.Client(base_url=BACKEND_URL, timeout=10) as c:
        r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_voice_token_has_longer_ttl():
    """Phase 0.7G: /api/auth/voice-token returns a 6h token with scope=voice."""
    data = _login()
    with httpx.Client(base_url=BACKEND_URL, timeout=10) as c:
        r = c.post(
            "/api/auth/voice-token",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "voice"
        assert body["expires_in"] == 6 * 3600
        payload = jwt.decode(body["access_token"], options={"verify_signature": False})
        assert payload.get("scope") == "voice"
