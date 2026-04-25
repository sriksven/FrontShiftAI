# Resilience Policy Matrix

Single source of truth for *which* retry/timeout/circuit-breaker pattern
applies to *which* kind of outbound call. New integrations pick a policy
name and stop there — they don't reinvent the wheel, and we avoid the
"scattered ad-hoc handling" failure mode that Phase 6 exists to fix.

Implementation: `backend/utils/resilience.py` exposes a `@resilient(policy=…)`
decorator plus a `get_policy(name)` lookup. Policies are named constants —
changing a timeout once updates everything that references that policy.

## The matrix

| Policy key          | Call type                              | Timeout | Retries | Backoff                 | Circuit breaker | Idempotency key |
| ------------------- | -------------------------------------- | ------- | ------- | ----------------------- | --------------- | --------------- |
| `external_llm`      | Mercury, Groq, OpenAI chat completions | 8s      | 3       | exponential (1→2→4s)    | per provider    | n/a (read-only) |
| `external_search`   | Brave Search, Deepgram STT/TTS (HTTP)  | 5s      | 2       | exponential             | per provider    | n/a             |
| `internal_db`       | SQLAlchemy queries from FastAPI        | 2s      | 1       | fast-fail               | ✗               | n/a             |
| `voice_tool`        | Voice worker → backend HTTP            | 8s      | 2       | linear (1s × attempt)   | ✗               | **required**    |
| `user_facing_http`  | Browser → backend (client-side fetch)  | 10s     | 0       | — (user retries)        | —               | for mutations   |
| `gcs_sync`          | ChromaDB tar download/sync             | 300s    | 3       | exponential             | ✗               | — (re-syncable) |
| `livekit_chain`     | STT/TTS provider fallback chains       | 5s      | 0       | handled by LiveKit SDK  | ✓               | —               |

## Why each choice?

- **External LLM APIs need circuit breakers.** Repeated retries against a
  downed provider waste seconds of every request before the fallback chain
  trips over to the next provider. A breaker short-circuits the call after
  3 consecutive failures and skips the provider for 60s.
- **External LLMs don't need idempotency keys.** Inference has no persistent
  side effect, so retrying is safe.
- **Internal DB queries fast-fail.** Retrying a DB that's already under load
  makes it worse. The right move is to bubble up and let the client retry.
- **Voice tool calls require idempotency keys** because they *mutate* state
  (POST `/api/pto/chat` creates rows) *and* we retry them. The combination
  is only safe with a key reused across retries — see Phase 0.5.
- **User-facing chains don't auto-retry.** A user clicking again is a
  stronger signal than a silent client retry — they know whether they
  actually want the action.
- **GCS sync re-runs are idempotent by construction** (`rsync`). No key needed.
- **LiveKit chains use LiveKit's `FallbackAdapter`** — we don't layer our own
  retries on top; we just configure the per-stage circuit breaker.

## Circuit-breaker mechanics

Three states: **closed** (pass-through) → **open** (fail-fast) → **half-open**
(allow one probe). Defaults:

- 3 consecutive failures → open
- 60s cooldown → half-open
- Success in half-open → closed
- Failure in half-open → re-open, reset the 60s cooldown

Breakers are **keyed per-call-target** (e.g. `mercury`, `groq`, `openai`,
`brave`). Mercury being down does not trip Groq.

State is exposed as a Prometheus gauge (`circuit_breaker_state{provider="..."}`)
once Phase 7 lands.

## Enforcement

A CI grep test ([`.github/workflows/pre_commit_tenant_check.yml`] pattern)
should forbid direct `httpx.post`/`requests.post`/etc. outside
`backend/utils/resilience.py` and a short allow-list of legacy callers that
have their own orchestration (LLM generator fallback chain, voice
BackendClient, Modal session endpoints).

Use `@resilient(policy="external_llm")` at the call site.
