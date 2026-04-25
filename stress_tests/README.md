# Stress Tests

End-to-end and soak tests for the FrontShiftAI backend, organized by plan
phase. These hit a *running* backend — they are deliberately not part of
`pytest` default collection in CI for the backend unit tests.

## Running

```bash
# Install (once)
pip install -r stress_tests/requirements.txt

# Start the backend and voice session server in separate terminals,
# then export a valid JWT for a test user:
export STRESS_TEST_BACKEND_URL=http://localhost:8000
export STRESS_TEST_JWT=<access_token>

# Run one phase
pytest stress_tests/test_phase0_resilience.py -v

# Run all phase-0 tests
pytest stress_tests/ -v -k phase0
```

## Files

- `conftest.py` — shared fixtures: `backend_url`, `auth_headers`, `http_client`, `LatencyReport`.
- `test_phase0_resilience.py` — 0A–0H critical-resilience fixes.
- `test_phase0_5_idempotency.py` — Idempotency-Key semantics.
- `test_phase0_6_tenancy.py` — cross-tenant access denial, listener behaviour.
- `test_phase0_7_jwt.py` — access/refresh TTL, rotation, theft detection.
