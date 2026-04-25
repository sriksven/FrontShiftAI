# External Call-Site Audit — Phase 6.5C

This table enumerates every outbound HTTP/subprocess call in the Python code
and its assigned resilience policy. Migration is rolling — not every site
needs the `@resilient` decorator today, but each one must have a documented
rationale.

| # | Call site | File:line | Policy | Migration status | Notes |
|---|---|---|---|---|---|
| 1 | Brave Search | `backend/agents/website_extraction/tools.py:42` | `external_search` | ✅ migrated | Reference implementation. Wrapped via `_brave_get`; breaker key `brave`. |
| 2 | Mercury chat completions | `chat_pipeline/rag/generator.py:~304` | `external_llm` | ⏳ deferred to Phase 6B | Already has 429-aware retry + exponential backoff after 0H. Migration to `@resilient` will land with the circuit-breaker-integrated fallback chain in Phase 6B. |
| 3 | Groq chat completions | `chat_pipeline/rag/generator.py:~394` | `external_llm` | ⏳ deferred to Phase 6B | Same as Mercury; the fallback-chain rewrite is one coherent change. |
| 4 | OpenAI chat completions | `chat_pipeline/rag/generator.py` `_call_openai_api` | `external_llm` | ⏳ deferred to Phase 6B | 429 handling added in Phase 0H; breaker adoption = Phase 6B. |
| 5 | Local Ollama (llama.cpp HTTP) | `backend/agents/utils/llm_client.py:210` | `external_llm` | ⏳ deferred to Phase 6B | Migrated together with the others. |
| 6 | Mercury via llm_client.py | `backend/agents/utils/llm_client.py:246` | `external_llm` | ⏳ deferred to Phase 6B | Same scope. |
| 7 | LLM judge API | `chat_pipeline/evaluation/judge_client.py:160` | `external_llm` | 🟡 optional | Evaluation tooling, not a hot path; wrap if the evaluator starts running in CI. |
| 8 | GCS sync (ChromaDB tar) | `chat_pipeline/rag/data_loader.py:162` | `gcs_sync` | 🟡 Phase 4C | Phase 4 already schedules this migration; noted here for completeness. |
| 9 | Voice → backend (`BackendClient.post_with_retry`) | `voice_pipeline/scripts/main.py` | `voice_tool` | ⚠ parallel impl | Already has idempotency + linear backoff matching the policy. Decorator port would be redundant; the plan explicitly notes "voice_tool: own infra, no breaker". Keep as-is. |
| 10 | Internal SQLAlchemy queries | backend/* | `internal_db` | ⚠ n/a | Policy is fast-fail. SQLAlchemy provides its own timeout via `pool_pre_ping` + `statement_timeout` (not yet set). Tracked under Phase 6C (NullPool → QueuePool). |
| 11 | User-facing HTTP (axios) | `frontend/src/services/api.js` | `user_facing_http` | ✅ compliant | 10s timeout matches policy; no retries; refresh-on-401 is orthogonal. |
| 12 | LiveKit STT/TTS chains | `voice_pipeline/utils/config.py`, `scripts/main.py` | `livekit_chain` | ⚠ n/a | LiveKit's `FallbackAdapter` handles this inside the library; we just pick the provider list. |

## Net result

- **1/6 external HTTP call sites migrated** (Brave search) as the reference implementation.
- **5 LLM call sites intentionally deferred** to Phase 6B. They already have
  correct 429 / retry behaviour after Phase 0H; the full migration belongs
  with the circuit-breaker overhaul that replaces the hand-rolled fallback chain.
- **Voice, GCS, and internal DB** are out of scope per the policy matrix.
- **Frontend** already matches the `user_facing_http` policy.

## Reviewer checklist for new PRs

Before merging a PR that adds a new outbound call:

1. Pick a policy from `docs/resilience_policy.md` (or add one).
2. Wrap the innermost network call with `@resilient(policy="...", breaker_key="<target>")`.
3. Bubble `CircuitOpenError` to a sensible user-facing fallback.
4. If the call mutates external state, add an `Idempotency-Key` (see Phase 0.5).
