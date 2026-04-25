# FrontShiftAI — System Design

**Framework**: [Hello Interview's Delivery Framework](https://www.hellointerview.com/learn/system-design/in-a-hurry/delivery) — Requirements → Core Entities → API → Data Flow → High-Level Design → Deep Dives.

**Purpose**: Build a precise mental model of the system as it exists today. Flag the exact components the `plan.md` optimization work will touch, so changes are anchored in architectural context rather than isolated edits.

---

## 1. Requirements

System design starts by separating *what the system does* (functional) from *how well it does it* (non-functional). Skipping the non-functional requirements is the most common failure mode — it's how you end up optimizing latency for a system that doesn't actually need low latency, or over-engineering a system that needs to be simple.

### 1.1 Functional Requirements

**Primary users** (ranked by frequency of interaction):

1. **Employees (USER role)** — deskless workforce workers asking questions and taking actions
   - Should be able to ask policy questions in natural language ("What's our bereavement leave policy?") via **chat or voice**
   - Should be able to request PTO conversationally ("I need 3 days off next week") and get balance information back
   - Should be able to open HR tickets ("I want to talk to someone about my benefits")
   - Should be able to see their conversation history

2. **Company Admins (COMPANY_ADMIN role)** — HR staff managing one organization
   - Should be able to review/approve PTO requests
   - Should be able to manage HR tickets assigned to their queue
   - Should be able to upload company policy documents (handbooks) that become searchable
   - Should be able to configure PTO balances, holidays, blackout dates

3. **Super Admins (SUPER_ADMIN role)** — platform operators
   - Should be able to provision new companies (multi-tenant onboarding)
   - Should be able to query across all companies (cross-tenant visibility)

**Out of scope** (explicit — system design docs benefit from stating non-goals):
- Payroll calculations, benefits enrollment, direct deposit — not an HRIS
- Manager hierarchy, org chart, reporting chains
- Real-time collaboration between employees
- Mobile-native apps (web-only, mobile browser only)

### 1.2 Non-Functional Requirements

The **top 5** constraints, ranked by how much they shape architecture:

1. **Multi-tenant isolation is non-negotiable** — a single cross-tenant data leak ends the product. Every database query, every vector search, every cache key must be scoped by `company_id`/`company`. This is a *consistency + security* requirement and it outweighs everything else, including latency.

2. **Voice latency target: sub-1.5s end-to-end** (currently ~3.4s, documented in `plan.md`). Deskless workers with dirty hands won't wait 3 seconds. Chat tolerates more (2-3s is fine). This asymmetry means **voice needs its own optimized path**, not just a faster general backend.

3. **Availability > strict consistency for reads; strict consistency for writes**. A stale policy answer ("last week's dress code") is acceptable for a few seconds; a lost PTO request or a ticket assigned to the wrong company is not. This CAP trade-off pushes toward eventually-consistent caching on the RAG side and strong consistency on the transactional (PostgreSQL) side.

4. **Horizontal scalability to ~19 tenants today, ~200 expected**. Not "Twitter-scale" — but each tenant has its own document corpus and its own PTO/ticket volume. Traffic is bursty and office-hours-shaped (9am-6pm local). Must scale to zero during off-hours (cost sensitivity — this is a student-run project at ~$15/month).

5. **Fault tolerance against external LLM provider outages**. The system depends on Mercury, Groq, OpenAI, and Deepgram — four third-party APIs in the critical path. A Mercury outage shouldn't take down the whole system. Provider fallback chains are load-bearing architecture, not a nice-to-have.

**Secondary constraints** (shape details, not architecture):
- JWT-based stateless auth (no session DB)
- GDPR-style compliance is not a hard requirement but shouldn't be actively violated (no plaintext passwords, no unbounded data retention)
- Hosted entirely on GCP with zero-trust secrets (no `.env` files committed)

### 1.3 Capacity Estimation

Done during design only when it drives a decision. Here are the numbers that actually matter:

- **Active tenants**: 19 companies, ~5-50 employees each = **~500 total users**
- **Peak concurrent voice sessions**: ~10 (sub-1% of users at any moment — voice is still a new interface)
- **Chat messages/day/tenant**: ~20-100, call it **~1,500/day total across all tenants**
- **RAG query peak rate**: ~5 QPS (extremely modest — a single Cloud Run instance handles this easily)
- **Document corpus per tenant**: 1-10 PDFs, ~50-500 chunks each → **~10,000 total vectors across all tenants**
- **ChromaDB index size**: ~10K vectors × 384 dims × 4 bytes ≈ **15MB** — fits comfortably in memory on any Cloud Run instance

**What this tells us**: We are *not* scale-constrained. The latency problem is a *per-request* problem (slow LLM calls, sequential stages, no streaming), not a *throughput* problem. Don't solve a scaling problem that doesn't exist.

---

## 2. Core Entities

The data model is already stable — no need to re-derive it. These are the canonical entities referenced by `backend/db/models.py`:

| Entity | Primary Key | Tenant Scope | Notes |
|---|---|---|---|
| **Company** | `name` (string) | self | Also identified by `email_domain` (unique index) |
| **User** | `email` | `company` FK | Role enum: SUPER_ADMIN / COMPANY_ADMIN / USER |
| **PTOBalance** | auto | `company` + `email` + `year` (unique constraint) | `total_days`, `used_days`, `pending_days`; `remaining_days` is a computed property |
| **PTORequest** | UUID | `company` (indexed) | status: PENDING/APPROVED/DENIED/CANCELLED |
| **HRTicket** | UUID | `company` (indexed) | status + category + urgency + `queue_position` |
| **CompanyHoliday** | UUID | `company` | recurring or one-off |
| **CompanyBlackoutDate** | UUID | `company` | date range where PTO is forbidden |
| **Conversation** | UUID | `company` + `user_email` | chat history parent |
| **Message** | UUID | `conversation_id` FK | role + content + `agent_type` + JSON metadata |
| **Task** | UUID | n/a (platform-level) | background job tracking |

**Plus two "entities" that aren't in PostgreSQL:**

| Entity | Where it lives | Tenant Scope |
|---|---|---|
| **DocumentChunk** (RAG) | ChromaDB | metadata filter `company` field |
| **VoiceSession** (ephemeral) | Modal + LiveKit | derived from JWT at session creation |

**Multi-tenancy observation**: Every durable entity has a `company` column (or an implicit one via foreign key to `User`). This is application-layer multi-tenancy — there's no PostgreSQL Row-Level Security, no separate database per tenant. The isolation depends entirely on WHERE clauses being correct. This is a **principled choice with a known failure mode**: a single forgotten `.filter(company=...)` = cross-tenant leak. The plan doesn't change this, but it's worth knowing.

---

## 3. API / System Interface

The system has **three distinct API surfaces**, each with different users, protocols, and trust models.

### 3.1 Public HTTP API (Backend — Cloud Run)

REST over HTTPS, JWT-authenticated via `Authorization: Bearer <token>`. Hosted at `https://frontshiftai-backend-...run.app`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/login` | none | Exchange email+password for JWT |
| GET | `/api/auth/me` | JWT | Decode own token (no DB hit) |
| POST | `/api/chat/message` | JWT | Unified agent router — accepts `{message, conversation_id?}`, returns `{response, agent_used, conversation_id, metadata}` |
| GET | `/api/chat/conversations` | JWT | List own conversations |
| GET | `/api/chat/conversations/{id}/messages` | JWT | Get full thread |
| DELETE | `/api/chat/conversations/{id}` | JWT | Delete conversation |
| POST | `/api/rag/query` | JWT | Direct RAG query (skips intent routing) — `{query, top_k}` → `{answer, sources, timings}` |
| POST | `/api/pto/chat` | JWT | Direct PTO agent call |
| POST | `/api/hr_ticket/chat` | JWT | Direct HR ticket agent call |
| GET | `/api/admin/company-admins` | super_admin | Platform admin endpoints |
| POST | `/api/admin/add-company-admin` | super_admin | Provision company admin |
| GET | `/health` | none | DB liveness check |
| GET | `/health/ready` | none | Deep readiness (embedding model, ChromaDB, reranker all loaded) |

**Key design decision — user identity comes from the JWT, never the request body.** `get_current_user` dependency (`backend/api/auth.py:56-64`) is the only source of truth for `{email, company, role, name}`. This closes the common "pretend to be someone else" vulnerability that happens when request bodies carry identity.

**Token TTL** (current): 1 year (`auth.py:23` → `60 * 24 * 365` minutes). A stolen token is valid for a year with no revocation path. This is a security hole at any scale.

**Planned** (plan.md Phase 0.7): **60-minute access tokens + 30-day refresh tokens with rotation**. Industry-standard shape:
- Access token is short-lived and stateless (JWT, no DB check)
- Refresh token is long-lived, stored hashed in `refresh_tokens` table, revocable on logout
- Rotation on use: every refresh issues a new refresh token and revokes the old one; reusing a revoked token invalidates the entire chain (theft detection)
- Voice pipeline uses a separate ~6-hour scoped token per session — the voice worker's lifetime is shorter than its token's TTL, so no refresh flow needed inside the worker

### 3.2 Voice Session API (Modal — serverless)

Hosted at `https://<modal-app>.modal.run/session`. The frontend calls this, not the backend.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/session` | JWT-in-body | Create LiveKit room, spawn voice worker, return room credentials |
| GET | `/health` / `/health/deep` | none | Modal worker liveness |

The response includes a LiveKit token that the browser uses to connect directly to LiveKit's WebRTC servers. The backend is not in the audio path — audio goes **browser ↔ LiveKit ↔ Modal worker**.

### 3.3 Internal Voice → Backend API

The voice worker (Modal) is itself a client of the public backend API. It uses the *same* `/api/rag/query` and other endpoints, authenticated with the user's JWT passed from the browser through LiveKit room metadata. This is the critical path for voice latency — every tool call the LLM makes is an internal HTTP call from Modal to Cloud Run.

**This is the surface the plan.md optimizations target most heavily**: connection pooling (Phase 1A), streaming endpoint (Phase 2), prefetch endpoint (Phase 5B), voice-specific params like `max_tokens` and `generation_backend` (Phase 1D, 1E).

---

## 4. Data Flow

Two canonical flows worth tracing end-to-end — a voice query (the hardest) and a PTO request (the most transactional). Each step is numbered so you can map it to components in the high-level diagram.

### 4.1 Voice RAG Query

User asks *"What's our remote work policy?"* through the web voice button.

1. **Browser → Modal `/session`**: user clicks voice button, frontend POSTs to Modal's web API, sends JWT. Modal creates a LiveKit room, spawns `voice_worker_for_room` (Modal function), returns room URL + LiveKit token. *(~500ms today; ~2s on cold start)*
2. **Browser ↔ LiveKit WebRTC**: browser establishes audio stream to LiveKit server
3. **Modal worker joins room**: the `VoiceAgent` subclass (`voice_pipeline/scripts/main.py:340+`) connects as a participant, reads user JWT from room metadata, initializes `BackendClient` with that JWT
4. **STT (Deepgram)**: user speaks; audio streams to Deepgram Nova-2; partial transcripts stream back, then a final transcript fires *"what's our remote work policy"*  *(~400-600ms after user stops talking)*
5. **VAD (Silero)**: in parallel, Voice Activity Detection decides the user is done speaking (currently 500ms silence, `plan.md` Phase 1B drops to 300ms)
6. **LLM tool decision (OpenAI GPT-4o-mini)**: the voice agent's LLM receives the transcript, decides to call the `query_info` function tool *(~200-400ms for first token)*
7. **Tool call: Modal worker → Backend `/api/rag/query`**: HTTP POST with the user's JWT. Today this creates a fresh `httpx.AsyncClient` per call (`main.py:96-102`); Phase 1A makes it a persistent client. *(~50-150ms connection overhead today)*
8. **Backend authenticates**: `get_current_user` decodes JWT, extracts `company` (`auth.py:56-64`)
9. **Backend → ChromaDB retrieval**: `pipeline._execute_retrieval()` queries ChromaDB with the question embedding; the `where` clause filters by `company`. The `resolve_company_filter()` step (`data_loader.py:244-285`) currently scans 10K records as a fallback — Phase 4A/B caches this *(~200-400ms)*
10. **Backend → Mercury/Groq LLM generation**: `generation()` builds the prompt with retrieved chunks + query, calls Mercury by default (Phase 1E switches voice to Groq) *(~300-800ms today, ~100-200ms with Groq)*
11. **Backend response**: JSON `{answer, sources, timings}` returned to Modal worker
12. **TTS (Deepgram Aura)**: the voice agent feeds the answer text to Deepgram TTS; audio streams back *(~200-300ms first byte, then streams)*
13. **Modal worker → LiveKit → Browser**: audio chunks stream through LiveKit to the user's speakers

**Total today: ~3.4s.** The bulk of the time is in steps 7-11 (the backend call). Streaming (Phase 2) overlaps step 10 and step 12 so TTS starts as soon as the first LLM tokens arrive, compressing the perceived latency.

### 4.2 PTO Request (Chat)

User types *"I need 3 days off next week"* into the chat UI.

1. **Browser → Backend `/api/chat/message`** with JWT
2. **Intent detection** (`unified_agent.py:57-162`): keyword match on "pto", "time off" → routes to PTO agent without hitting the LLM (keyword path is ~1-2ms; LLM fallback path is ~200-400ms)
3. **PTO Agent LangGraph state machine** (`backend/agents/pto/agent.py:40-103`):
   - `parse_intent` — LLM extracts dates, reason, intent
   - `validate_dates` — ensures start ≤ end, in the future
   - `check_balance` — queries `PTOBalance` table by `(email, year, company)` → returns `remaining_days`
   - `check_conflicts` — cross-checks against existing `PTORequest` rows (PENDING/APPROVED) + `CompanyHoliday` + `CompanyBlackoutDate`
   - `create_request` — inserts `PTORequest` row with UUID, status=PENDING
   - `generate_response` — LLM drafts conversational response with balance info
4. **Backend persists**: `Conversation` (if new) + user `Message` + assistant `Message` rows
5. **Backend response**: `{response, agent_used: "pto", conversation_id, metadata: {request_id, balance_info}}`

The PTO flow is the canonical **LangGraph state machine** pattern — each node is a pure function of state, edges are conditional transitions, failures route to `generate_response` with error context. This design makes the agent's reasoning auditable (each state transition is loggable) and the business rules testable in isolation.

### 4.3 Data Pipeline (Offline)

Runs periodically (Airflow DAG in prod, `python scripts/pipeline_runner.py` locally). **Not in the hot path** — produces the ChromaDB index that the backend consumes.

1. `download_data.py` — fetch PDFs from URLs into `data/raw_pdfs/`
2. `pdf_parser.py` — OCR (Tesseract + EasyOCR) → Markdown in `data/processed_pdfs/`
3. `preprocess.py` — clean/normalize text
4. `chunker.py` — split into ~500-token chunks with overlap, write JSONL
5. `validate_data.py` — quality checks + anomaly email alerts
6. `data_bias.py` — fairness metrics
7. `store_in_chromadb.py` — embed (all-MiniLM-L6-v2) + insert into ChromaDB at `data/vector_db/`
8. **Sync to GCS**: tar.gz the ChromaDB directory, push to `gs://frontshiftai-data/chroma_db.tar.gz`

The backend pulls this tar.gz on startup (`ensure_chroma_store()` in `chat_pipeline/rag/data_loader.py:119-197`) and extracts it locally. This is the mechanism behind the **30-50 second cold start**.

---

## 5. High-Level Design

The system is a **multi-component distributed architecture** with one synchronous request path (chat) and one bidirectional-streaming path (voice). Here's the complete topology:

### 5.1 Component Diagram (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            USER DEVICES                                 │
│  ┌──────────────┐          ┌──────────────┐                             │
│  │  React SPA   │          │  Browser mic │                             │
│  │  (Cloud Run  │          │  (WebRTC)    │                             │
│  │   + Nginx)   │          │              │                             │
│  └──────┬───────┘          └──────┬───────┘                             │
└─────────┼─────────────────────────┼─────────────────────────────────────┘
          │ HTTPS                   │ WebRTC (audio)
          │ Bearer JWT              │
          ▼                         ▼
┌──────────────────┐      ┌───────────────────────────────────────────────┐
│  BACKEND API     │      │            LIVEKIT CLOUD                      │
│  (Cloud Run)     │      │    (WebRTC SFU — audio routing only)          │
│  FastAPI         │      └──────────────────┬────────────────────────────┘
│  4vCPU / 4GB     │                         │
│  min=0 max=10    │                         ▼
│                  │      ┌────────────────────────────────────────────┐
│  /api/auth       │      │          MODAL (serverless)                │
│  /api/chat       │◀─────│  voice_worker_for_room  (2vCPU / 4GB)      │
│  /api/rag        │ JWT  │  - LiveKit agent (STT→LLM→TTS)             │
│  /api/pto        │      │  - BackendClient (HTTP to Backend API)     │
│  /api/hr_ticket  │      │  web_api (1vCPU / 2GB) — /session endpoint │
│  /api/admin      │      └────┬──────────┬──────────┬─────────────────┘
│  /health         │           │          │          │
└──┬──────┬────┬───┘           ▼          ▼          ▼
   │      │    │         ┌─────────┐ ┌─────────┐ ┌──────────┐
   │      │    │         │Deepgram │ │ OpenAI  │ │Deepgram  │
   │      │    │         │  STT    │ │GPT-4o-  │ │  TTS     │
   │      │    │         │ Nova-2  │ │  mini   │ │Aura-2    │
   │      │    │         └─────────┘ └─────────┘ └──────────┘
   │      │    │
   │      │    ▼ ChromaDB (persistent volume, tar.gz from GCS on startup)
   │      │   ┌──────────────────────┐
   │      │   │  VECTOR STORE        │
   │      │   │  all-MiniLM-L6-v2    │
   │      │   │  ~10K vectors × 384d │
   │      │   │  Filter: company     │
   │      │   └──────────────────────┘
   │      │
   │      ▼ Unix socket (Cloud SQL Auth Proxy)
   │     ┌──────────────────────┐
   │     │  POSTGRESQL 15       │
   │     │  (Cloud SQL)         │
   │     │  db-f1-micro         │
   │     │  NullPool (!)        │
   │     │                      │
   │     │  Users, Companies,   │
   │     │  PTO*, HRTicket,     │
   │     │  Conversations,      │
   │     │  Messages, Tasks     │
   │     └──────────────────────┘
   │
   ▼ HTTPS (fallback chain)
  ┌──────────┐ ┌──────┐ ┌────────┐ ┌───────┐
  │ Mercury  │→│ Groq │→│ OpenAI │→│ Local │
  │(primary) │ │      │ │        │ │Ollama │
  └──────────┘ └──────┘ └────────┘ └───────┘

                       ┌────────────────┐
         Offline ─────▶│  DATA PIPELINE │──▶ gs://frontshiftai-data/
         (Airflow)     │  Download/OCR/ │    chroma_db.tar.gz
                       │  Chunk/Embed   │    (synced to Backend on startup)
                       └────────────────┘

                  ┌──────────────────────┐
         Observe: │  W&B                 │
                  │  FrontShiftAI_Prod   │  (production metrics)
                  │  FrontShiftAI_Agents │  (evaluation metrics)
                  └──────────────────────┘
```

### 5.2 Why These Specific Components

Each boundary in the diagram reflects a deliberate separation-of-concerns decision. Understanding *why* the boundary exists tells you when it's safe to cross it and when it isn't.

**Backend API vs. Voice Worker (Modal)**. Why not run the voice agent inside the backend? Two reasons:

1. **Blast radius**. LiveKit agents are long-lived processes (up to 1 hour per session). Running them inside Cloud Run would mean instances can't scale down while any voice session is active, breaking the cost model.
2. **Failure isolation**. A misbehaving voice session (stuck STT, hung TTS) shouldn't exhaust the backend's workers. Modal gives each session its own container, each with independent failure.

**LiveKit vs. direct WebRTC to the worker**. LiveKit acts as a Selective Forwarding Unit (SFU) — the browser and the Modal worker never establish a direct P2P connection. This gives us (a) NAT traversal for free, (b) the ability to add a second participant (e.g., a supervisor listening in) without re-plumbing, and (c) LiveKit's recording/logging infrastructure. The cost is an extra network hop, but WebRTC hops through a well-provisioned SFU are cheap (~20-50ms).

**ChromaDB as a file-based embedded DB, not a managed service**. We could use Pinecone or a hosted Chroma. We chose file-based because:
- Tenant count is small (19); the index fits in RAM
- Zero network hop between backend process and vector search (the vector search is literally a function call, not an HTTP request)
- Operational simplicity — no third service to monitor
The cost is the 30-50s cold start while downloading the tar.gz from GCS. This is the **single biggest ChromaDB-related decision** and it directly drives why `min_instances=0` hurts us at 9am Monday when everyone logs in.

**PostgreSQL via NullPool**. Cloud Run terminates idle TCP connections aggressively. The NullPool (`backend/db/connection.py:35-40`) means every query opens a fresh connection, trading per-query latency for connection-exhaustion safety. Plan.md Phase 6C revisits this with `QueuePool` — the correct answer is a bounded pool, not no pool.

**Four-provider LLM fallback chain** (`chat_pipeline/rag/generator.py:451-519`): Mercury → Groq → OpenAI → Local Ollama. This exists because a single-provider outage on a demo day is a product-killing event. The fallback is sequential with exponential backoff (2s → 4s → 8s), which means **a Mercury outage today adds up to ~14s to every request before falling through** — Plan.md Phase 6B adds a circuit breaker to fix this.

### 5.3 Critical Request Path Timing (Voice RAG Today)

This is the budget sheet for the voice latency problem. Every number is wall-clock on a warm system.

| Stage | Time | Notes |
|---|---|---|
| VAD silence detection | 500ms | Silero default, tuneable to 300ms (Phase 1B) |
| STT first final transcript | 400-600ms | Deepgram Nova-2, streaming-capable |
| Voice agent LLM decides tool call | 200-400ms | OpenAI GPT-4o-mini, not streaming today |
| **HTTP: Modal → Backend** | **50-150ms** | **New client per call (Phase 1A)** |
| Backend: JWT decode | 1-2ms | |
| Backend: ChromaDB retrieval | 200-400ms | Includes `resolve_company_filter` scan (Phase 4A/B) |
| Backend: LLM generation (Mercury) | 300-800ms | Groq is 100-200ms (Phase 1E) |
| Backend: DB writes (user+assistant msgs) | 20-50ms | |
| **HTTP: Backend → Modal** | **50-150ms** | Same pooling issue |
| TTS first audio byte | 200-300ms | Deepgram Aura, streams thereafter |
| WebRTC delivery | 20-50ms | LiveKit SFU hop |
| **Total** | **~3.4s** | |

The plan's sub-1.5s target requires three things simultaneously:
1. **Shrink the backend portion** — caching + Groq + fewer tokens (~500-1000ms saved)
2. **Overlap the backend and TTS stages** — streaming endpoint so TTS starts before generation finishes (~300-500ms saved)
3. **Eliminate the HTTP pooling overhead** (~100-300ms saved)

None of these alone hits the target. All three together do.

---

## 6. Designing for Scale — Chokepoints and Thresholds

The §1.3 capacity estimate is correct: FrontShiftAI isn't scale-*constrained* today. But "not constrained today" is different from "safe to ignore." A well-designed system at low scale should:

- **Not foreclose scaling paths** — the architecture can be incrementally upgraded without a rewrite
- **Be correct at any scale** — idempotency, bounded growth, no instance-local state that affects correctness
- **Know its breaking points** — we can name, for each component, the specific threshold at which it fails

This section does the third. For every major subsystem, what is the threshold, what's the upgrade path, and what is the earliest warning signal that we're approaching the threshold?

### 6.1 The Chokepoint Table

| # | Subsystem | Works today because... | Breaks at... | Upgrade path | Earliest warning signal |
|---|---|---|---|---|---|
| 1 | **ChromaDB from GCS tar.gz** | 10K vectors × 384d ≈ 15MB; download ~10-20s | ~100K+ vectors (~100 tenants, or larger per-tenant corpora); tar.gz download becomes minutes | (a) ChromaDB Server mode as a sidecar; (b) managed Chroma Cloud / Pinecone / Weaviate; (c) per-region ChromaDB replicas | Cold start > 60s; GCS download > 30s |
| 2 | **Single ChromaDB collection, metadata-filtered** | `where: {company: X}` is fast on 10K vectors | ~1M vectors — metadata filter degrades toward linear scan | Per-tenant collections (`frontshift_<tenant>`); or per-tenant ChromaDB clients | Retrieval p95 > 500ms; CPU spikes on query |
| 3 | **PostgreSQL with NullPool** | Light query load, connections cheap to reopen | ~100 concurrent queries — connection storm exhausts Cloud SQL (db-f1-micro tops at 25 connections) | (a) QueuePool (Phase 6C); (b) pgbouncer in front; (c) read replicas for chat history reads | Cloud SQL "connection limit reached" alerts; 503s under load |
| 4 | **Application-layer multi-tenancy** | Small team, careful code review catches forgotten `.filter(company=...)` | ~5 engineers / ~50 tenants — the statistical probability of a forgotten filter becomes high | PostgreSQL Row-Level Security policies (`CREATE POLICY ... USING (company = current_setting('app.company'))`); or separate DB per tenant | Any cross-tenant bug in prod (immediate hard-fail signal) |
| 5 | **In-process caches** (RAGPipeline OrderedDict, `@lru_cache`, LLM TTLCache) | `min_instances=0` means usually one instance serves a traffic burst | `min_instances >= 1` with autoscaling: cache hit rate drops from ~40% to ~5%; **latency increases as you scale up** | Shared cache layer: Redis (GCP Memorystore) or Memcached; cache warming on instance start | Latency p95 goes *up* when Cloud Run adds instances (counterintuitive — this is the signature of cache fragmentation) |
| 6 | **Non-durable LangGraph state** | Each HTTP request runs the state machine from scratch; <2s turns complete in one request | **Today** — multi-turn conversations can't resume across sessions; workflows can't wait for external events (admin approval, scheduled reminders). Not a scale chokepoint, a **feature chokepoint** that applies now. | LangGraph `PostgresSaver` checkpointer — persist state per `thread_id` (conversation_id). Temporal/Airflow only when workflows exceed hours/days. | Feature requests like "notify user when PTO is approved," "resume my PTO request from earlier," "remind admin after 24h" — all unblocked by durable checkpointing |
| 7 | **Modal voice workers spawned per session** | ~10 concurrent sessions, Modal cold start ~7-12s amortized over 10-min session | ~100+ concurrent sessions — Modal cold start queue saturates; users see "spawning" errors | Pre-warmed worker pool (Kubernetes pods with a min replica count); or long-lived worker with session multiplexing | Modal session creation p95 > 3s; worker spawn error rate > 1% |
| 8 | **Single region (us-central1)** | Tenant base is North America | EU/APAC tenants — cross-ocean WebRTC adds 150-300ms; voice feels broken | Multi-region Cloud Run + regional LiveKit clusters; Cloud SQL with read replicas per region | Tenant onboarding in non-NA markets; voice latency p95 > 2s for non-NA users |
| 9 | **Multi-step agentic workflows** (LLM → tool → LLM → tool → …) | We don't have them yet — voice agent makes exactly one tool call per turn | The moment a product feature requires LLM reasoning over 2+ tool calls (e.g., "search policy → cross-reference with my PTO balance → suggest best dates") | Streaming intermediate state to the user ("Looking up policy… checking balance…"); breaking workflows into user-visible steps; considering structured planning frameworks (LangGraph supervisor pattern) | Feature requests for multi-step agent reasoning; LLM turns that approach the 10s voice-turn limit. **Note**: single-step streaming is already addressed by plan.md Phase 2 + extension — that's not this chokepoint. |
| 10 | **Unbounded Conversations/Messages table growth** | 1500 messages/day × 365 days = ~500K rows per year; fits in db-f1-micro | ~10M rows (hard to say exactly — depends on query patterns) — chat history queries slow down | Soft-delete + archival to Cloud Storage; or TTL on `created_at`; or sharding by `company` | Chat history `GET /conversations/{id}/messages` p95 > 500ms |

### 6.2 Principles That Should Already Be True at Low Scale

These are cheap-now-expensive-later properties. The system has some of them; some it doesn't.

**Principle 1: No instance-local state that affects correctness.**
- ✓ Today: the in-memory caches are *performance-only* — a cache miss produces the same answer, slower
- ⚠ Phase 0E (fix cache race condition) is correctness-critical because `OrderedDict` mutations during concurrent reads can raise `KeyError`, which *is* observable by the user
- **Not broken, worth watching.**

**Principle 2: Idempotent writes.**
- ✗ Today: PTO creation, HR ticket creation, and message persistence are **not idempotent**. Retrying the same request creates duplicates.
- ⚠ **This was a bug we would have introduced in Phase 0D.** The retry wrapper for voice tool calls would double-create PTO requests on transient errors:
  ```
  Voice agent → Backend: POST /api/pto/chat (create request)
  Backend: creates PTO row, commits, returns 201
  Network error on response path — voice agent sees ConnectError
  Phase 0D retry → Backend: creates a SECOND PTO row
  ```
- ✓ **Fix in plan.md Phase 0.5**: idempotency keys. Client generates a UUID per logical operation; backend stores `(idempotency_key, company, response)` in the `idempotency_records` table for 24h; on duplicate key, return cached response. The Phase 0D retry wrapper reuses the same key across retries — that's the point.
- This is now a dedicated Phase 0.5 (2 days), scheduled immediately after Phase 0 and before any other work that depends on retries being safe.

**Principle 3: Bounded growth on every durable entity.**
- ✗ Today: `Conversations` and `Messages` grow forever. No TTL, no archival.
- ✗ `Task` table grows forever. Also no cleanup.
- The fix is cheap now (add a daily cron job that moves messages older than 90 days to Cloud Storage), and expensive later (migrating a 100GB table is painful).
- **Not in plan.md today. Worth adding.**

**Principle 4: Observability before optimization.**
- ⚠ Today: production logs exist, but without correlation IDs you can't trace a voice turn from STT → backend → ChromaDB → LLM → TTS.
- Phase 6A in plan.md addresses this. **Correct priority** — it unblocks future scale debugging.

**Principle 5: Separate read and write paths when cheap.**
- ✓ Today: reads and writes both hit the primary DB. Fine at current scale.
- At the next scale tier (~10x traffic), the easy win is a read replica for `Conversations`/`Messages` queries (read-heavy) while keeping `PTORequest`/`HRTicket` writes on primary.
- **Not needed now, but the schema already supports it** — no denormalization that would make a read/write split painful.

**Principle 6: Failure modes should be fail-closed, not fail-open.**
- ✗ Today: SQLite fallback on Postgres outage (fail-open — accepts writes that are lost). Server starts even if ChromaDB warmup fails (fail-open — claims healthy, all queries fail).
- Phase 0A and 0B fix both. **These are correctness issues at any scale.**

### 6.3 What This Means for Plan Sequencing

Looking at plan.md through the scale + feature-chokepoint lens drives these priorities:

1. ✓ **Idempotency keys** — now Phase 0.5 in plan.md. Retry wrapper (Phase 0D) is unsafe without them.

2. ✓ **Durable LangGraph checkpointing** — now Phase 5.5 in plan.md (~2 days). Not a scale issue — a *feature capability* issue. Unlocks multi-turn resumability, admin-approval workflows, and workflow replay for debugging. Worth doing now because LangGraph supports it natively (`PostgresSaver`) and the cost is a checkpoint table + `thread_id` wiring.

3. ✓ **Streaming extended to PTO/HR agents** — now Phase 2 extension in plan.md (~1 day). Node-by-node status streaming ("Parsing your request... Checking balance... Creating request...") makes a 2-3s operation feel instant. Different from the multi-step agentic streaming of Chokepoint #9 — this is applying the Phase 2 pattern to existing single-step workflows.

4. **Watch Phase 3A (`keep_warm=1`) carefully in production.** Once you have a warm instance always alive, the in-process caches become the dominant perf factor. If you later add `max_instances` scaling, Chokepoint #5 will bite. The first warning signal will be latency going *up* under load rather than down. If that happens, the fix is Redis — not more instances.

5. **Keep `min_instances=0` for now unless demo cost isn't a concern.** The `keep_warm=1` math works for Modal ($2/day) but not for the Cloud Run backend (4vCPU/4GB is ~$4/day per warm instance). For a student-run project, prefer scheduled warming (Cloud Scheduler pings `/health` during business hours) over a permanent warm instance.

Everything else in plan.md is **correctly scoped** for the current scale — it solves the right problems without introducing scale hazards. The key distinction the original chokepoint table missed: **async / streaming / durable state are implementation techniques** applied based on feature requirements (should mostly be done now); **event-driven pub/sub architecture is an operational pattern** that scales with organizational complexity (defer until multi-party workflows exist).

---

## 7. Deep Dives

The plan.md optimization and resilience work concentrates on six specific architectural surfaces. Each gets its own deep dive so you see exactly what's being changed and why the change makes sense given the rest of the system.

### 7.1 Deep Dive: Voice → Backend HTTP Surface (Phase 1A, Phase 2)

**What's there today**: `BackendClient` (`voice_pipeline/scripts/main.py:80-112`) opens a new `httpx.AsyncClient` context manager for each call:

```python
async def post(self, path, payload, timeout=120):
    async with httpx.AsyncClient(base_url=self.base_url, ...) as client:
        resp = await client.post(path, json=payload)
```

**The system design principle at stake**: *connection lifecycle*. HTTP connections over TLS cost ~50-150ms to establish (TCP handshake + TLS handshake + protocol negotiation). If your call pattern is "open → single request → close," you pay that cost every call. The Keep-Alive mechanism and TCP connection reuse exist specifically to amortize this cost across many requests.

**Why the current code is wrong**: The voice agent makes 3-5 backend calls per user turn (retrieval, balance check, ticket check, etc.). At 50-150ms each, that's 150-750ms of pure handshake overhead per turn — with zero functional benefit.

**What Phase 1A does**: A single persistent `httpx.AsyncClient` with `limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)` — the connection stays open, subsequent calls skip the handshake.

**Why the limits matter**: This is the *connection pool* configuration. Without limits, you can leak connections into Cloud Run's socket table. With too-tight limits, concurrent calls serialize and create their own latency problem. The `max_keepalive_connections=5` is the right order of magnitude for a voice agent making 3-5 concurrent calls.

**Deeper: why Phase 2 (streaming) is architecturally different**. Connection pooling is a *reuse* optimization. Streaming is a *pipelining* optimization — it changes the shape of the request/response from "send → wait → receive full" to "send → receive tokens as they generate." The existing `/api/rag/query` endpoint is `POST ... → RAGQueryResponse` (a single JSON object). The new `/api/rag/query/stream` endpoint uses Server-Sent Events (SSE) over HTTP to emit:
- `event: sources` (after retrieval completes, ~300-400ms in)
- `event: token` × N (as the LLM generates)
- `event: done` (final timings)

This is the same pattern OpenAI uses for `stream=True`. The voice agent consumes the stream and feeds tokens to TTS as they arrive, which means **TTS synthesis and LLM generation run concurrently instead of sequentially**. This is where the 300-500ms comes from — not new speed, but eliminated waiting.

### 7.2 Deep Dive: RAG Pipeline Internals (Phase 1D, 1E, 4, 5A)

**Where things live** (`chat_pipeline/rag/pipeline.py:188-355`):

```
RAGPipeline.run(query, top_k, company_name, stream=False, ...)
├── cache check — OrderedDict LRU, 32 entries (line 202)
├── _execute_retrieval(...)  — vector search, returns docs + metadata
│   ├── load_data_company(company_name)
│   │   ├── get_collection()              [@lru_cache(1)]
│   │   └── resolve_company_filter()      [NOT cached today — Phase 4B]
│   └── vector_retrieval(...)              ChromaDB query
├── generation(query, documents, metadatas, stream, ...)
│   ├── _select_prompt_template()
│   ├── _prepare_context()                  token truncation via tiktoken
│   └── stream_response()                   fallback chain (Mercury→Groq→OpenAI→Local)
└── cache update (if not streaming and not cache hit)
```

**Architectural observation**: the pipeline is deliberately config-driven via `chat_pipeline/configs/rag.yaml`. The retriever (vector vs. BM25), reranker (on/off), and generation backend (mercury/groq/openai) can all be swapped without code changes. Plan.md Phase 1E leverages this — voice passes `generation_backend: "groq"` as a config override; chat keeps Mercury default. Two clients of the same pipeline, two different configs.

**The caching layers in detail** (this is where Phase 4 acts):

| Cache | Where | What it holds | Current behavior |
|---|---|---|---|
| `RAGPipeline._cache` | `pipeline.py:202` | `(query, company, config) → (answer, metadata)` | OrderedDict LRU, 32 entries. **Not thread-safe** (Phase 0E fixes) |
| `_load_company_index` | `data_loader.py:68` | `company_index.json` → dict | `@lru_cache(maxsize=1)` — fine |
| `get_collection` | `data_loader.py:212` | ChromaDB handle | `@lru_cache(maxsize=1)` — fine |
| `_get_all_companies` | `data_loader.py:228` | List of company names | **Not cached** — scans 10K records every call (Phase 4A fixes) |
| `resolve_company_filter` | `data_loader.py:244` | company_name → where clause | **Not cached** — runs on every query (Phase 4B fixes) |
| LLM agent client | `backend/agents/utils/llm_client.py:29` | `(messages, params) → response` | TTLCache, 100 items, 5min TTL |

**Why `resolve_company_filter` is a surprising hotspot**. Look at `data_loader.py:244-285`: it tries the in-memory company index first (fast), then peeks at the ChromaDB collection (slow — 200 records), then calls `_get_all_companies()` (very slow — 10K records). In production, if the `company_index.json` is ever missing or out of date, *every query* falls through to the slowest path. This is a classic **performance cliff**: fast 99% of the time, catastrophically slow when a precondition breaks. Phase 4A/B caches the fallback path so the cliff becomes a small bump.

**Why Phase 1D (voice max_tokens=256) is more than a prompt tuning**. Mercury/Groq generation time scales nearly linearly with output token count. A 1024-token response takes ~4x longer to generate than a 256-token response. Voice responses need to be short anyway (you can't listen to 4 paragraphs), so capping tokens isn't a product compromise — it's aligning the technical constraint with the product constraint. Phase 5A (voice prompt template) compounds this by telling the LLM to produce spoken-style 2-3 sentence answers, which naturally stays under 256 tokens.

### 7.3 Deep Dive: LangGraph State Machines (PTO + HR)

The PTO agent (`backend/agents/pto/agent.py:40-103`) is a **deterministic state machine over typed state**:

```
START → parse_intent ──────(request_pto)──→ validate_dates
                      └────(check_balance)──→ check_balance_only → generate_response
                      └────(view_requests)──→ list_requests → generate_response
                      └────(general)────────→ generate_response

validate_dates → (valid) → check_balance → (sufficient) → check_conflicts
              └→ (invalid) → generate_response       └→ (insufficient) → generate_response

check_conflicts → (clean) → create_request → generate_response
               └→ (conflict) → generate_response
```

**Why this matters architecturally**. The alternative is a monolithic prompt: "You are a PTO agent. Here are the user's request, their balance, the company holidays. Respond." That works ~80% of the time and fails unpredictably on edge cases. LangGraph forces the reasoning into discrete, independently testable nodes:

- `validate_dates_node` is a pure function of `(start_date, end_date)` → has no LLM call
- `check_balance_node` is a pure DB read → has no LLM call
- `check_conflicts_node` is a pure DB read → has no LLM call
- Only `parse_intent_node` and `generate_response_node` call the LLM

The LLM is used **only at the boundary** — for parsing unstructured input and for generating unstructured output. All the business logic is deterministic code. This is the **"LLM at the edges, code in the middle"** pattern, and it's the reason the PTO agent is reliable. A more naive design would be cheaper to write and catastrophically harder to debug.

#### 7.3.1 Current Gap: Non-Durable State (addressed by Phase 5.5)

The state machine is async (`graph.ainvoke`) but **state is not persisted between turns**. Each HTTP request re-hydrates an empty state, runs `parse_intent` from scratch, and discards everything when the request ends. Three concrete problems this creates:

**Problem 1: multi-turn conversations can't resume.**
```
User:    I'd like to request some time off
Agent:   Sure — which dates?
[user's browser crashes, reconnects 30 seconds later]
User:    Next week Thursday and Friday
Agent:   I don't see an active PTO request. Can you start over?
```
The "I'd like to request time off" context lived in the request-scoped state and is gone. The user starts over.

**Problem 2: workflows can't wait for external events.**
Today's PTO approval is two disconnected paths:
- `POST /api/pto/chat` creates a `PTORequest` row with `status=PENDING` and returns
- Separately, `POST /api/admin/pto/{id}/approve` (later, maybe hours later) sets `status=APPROVED`
- Separately again, some future chat session with the user might notice the status changed and surface it

What's missing is a *single workflow* that says "create → suspend until review event → resume → notify user." Today that's implemented as polling, a status column, and hope. A durable workflow models the waiting explicitly.

**Problem 3: no replay for debugging.**
When an agent returns a weird response, you can't reconstruct "what state did it go through to produce this?" because state was transient. You have to reproduce the bug from scratch.

#### 7.3.2 The Fix: LangGraph PostgresSaver Checkpointer

LangGraph has first-class support for state persistence via the `langgraph.checkpoint.postgres.PostgresSaver`. The shape of the change (Phase 5.5):

```python
from langgraph.checkpoint.postgres import PostgresSaver
from db.connection import engine

checkpointer = PostgresSaver(engine=engine)
graph = workflow.compile(checkpointer=checkpointer)

# Invocation now takes a thread_id — the conversation_id is a natural choice
await graph.ainvoke(
    new_input,
    config={"configurable": {"thread_id": conversation_id}},
)
```

The checkpointer creates a `workflow_checkpoints` table (managed by LangGraph) that stores state snapshots per `thread_id` after every node execution. On the next invocation with the same `thread_id`, the state machine resumes from the last checkpoint instead of starting empty.

**What this unlocks immediately:**
- **Multi-turn state carries forward**: second turn sees `parse_intent` result from turn 1, can skip re-parsing
- **Resume after disconnect**: browser crash → reconnect → same `thread_id` → conversation picks up where it left off
- **Workflow replay**: `SELECT * FROM workflow_checkpoints WHERE thread_id = 'X' ORDER BY created_at` gives you every state transition for a conversation

**What it unlocks later (without further architecture changes):**
- **"Wait for approval" pattern**: add a `wait_for_review` node that checkpoints state and returns a `PENDING` response. When admin approves, `POST /api/admin/pto/{id}/approve` triggers `graph.ainvoke(resume_input, config={thread_id: original_thread})` — the workflow resumes from the wait node, generates a notification, updates the user.
- **Timeouts**: add a scheduled job that finds conversations in `wait_for_review` state for >24h, resumes them with a `timeout` input, and emits an admin reminder.

#### 7.3.3 What This Is *Not*

Durable checkpointing is **not**:
- A message bus (no inter-service pub/sub — state transitions happen inside one service)
- A full workflow engine like Temporal (no deterministic replay guarantees across code changes, no multi-language SDK)
- An excuse to make turns arbitrarily long (turns should still complete in <3s; checkpointing is about *resumability*, not *duration*)

This is a deliberate choice. At 19 tenants with 1500 messages/day, adding Temporal or Cloud Pub/Sub is operational overhead without matching benefit. LangGraph + PostgresSaver covers 95% of the durable-workflow use cases with zero new infrastructure — the checkpoint table lives in the Postgres we already have.

#### 7.3.4 Streaming Node Outputs (Phase 2 Extension)

Separately but related: LangGraph supports streaming node-level outputs via `graph.astream_events()`. Plan.md Phase 2 (streaming RAG) extends to cover PTO/HR agents — each node emits a `{status: "parsing_dates"}`, `{status: "checking_balance"}`, `{status: "creating_request"}` event. Frontend/voice agent renders these as typing indicators or short spoken phrases ("Let me check your balance…"). Not faster, but perceived as instant because the user sees continuous progress instead of a 2-second silent wait.

Streaming and checkpointing compose cleanly: the stream reports progress, the checkpointer persists the state. Both are tools for making long-ish workflows feel responsive and reliable.

**Plan.md doesn't touch the agents directly** — but Phase 6B (circuit breaker) affects the LLM boundary nodes, because when Mercury is down, every `parse_intent_node` call burns 14s of retries before falling through. A circuit breaker shifts this to "fast-fail on Mercury, skip straight to Groq" after 3 consecutive failures.

### 7.4 Deep Dive: Deployment Topology and Cold Start

**Three distinct runtime environments, each with its own startup cost profile**:

1. **Backend (Cloud Run)** — 4vCPU, 4GB, min=0 max=10
   - Container pull: ~5-10s
   - Python + deps import: ~2-3s
   - **GCS → ChromaDB tar.gz download: ~10-20s**
   - **ChromaDB extract: ~5-10s**
   - **Embedding model load (all-MiniLM-L6-v2): ~5-15s**
   - Reranker load (cross-encoder, if enabled): ~3-10s
   - Total: **~30-50s**

2. **Voice Worker (Modal)** — 2vCPU, 4GB, no keep_warm
   - Container pull: ~3-5s
   - Python + LiveKit plugins import: ~2-3s
   - Silero VAD model load: ~500-1000ms
   - Provider clients (Deepgram, OpenAI, Cartesia) init: ~1-2s
   - Total: **~7-12s**

3. **Frontend (Cloud Run)** — 512MB, static Nginx serving React bundle — <1s startup

**The cold start problem in detail**: min_instances=0 means every idle period longer than Cloud Run's idle timeout (~15 min) results in a cold start on the *next* request. For the backend, this user experiences 30-50s before their first response. For voice, it's 7-12s until the agent is listening.

**Plan.md Phase 3A/3B (keep_warm=1)** is not a latency optimization — it's a **warm-up amortization** optimization. It costs ~$0.06/hour for Modal + ~$0.03/hour for the API = ~$2/day for the worker alone. For the backend, the correct approach is `min-instances=1` during business hours (configurable via Cloud Scheduler) rather than permanently paying for a warm instance. This is a *time-of-day scaling* pattern, appropriate for a system with predictable office-hours traffic.

**The deeper architectural constraint**: the 30-50s backend cold start is largely driven by the **ChromaDB-from-GCS** bootstrap. If the product needed to scale to 500 tenants with a 500MB index, this approach would break (GCS download time dominates). The alternative is **ChromaDB Server mode** (a separate persistent service, possibly a sidecar or a managed ChromaDB Cloud). Plan.md doesn't propose this — at 19 tenants, the file-based approach is correct. But it's the first thing to reconsider when tenant count crosses ~100.

### 7.5 Deep Dive: Multi-Tenancy and Data Isolation (Phase 0.6)

A failure here is unrecoverable — you can't "un-leak" tenant data. This is the highest-priority correctness surface in the system.

#### 7.5.1 The State Today — "Trust the Developer"

Three places tenant isolation is enforced, all by convention:

1. **PostgreSQL queries**: every ORM query must include `.filter(Model.company == user.company)`. Across 87 call sites. Zero framework enforcement.
2. **ChromaDB queries**: `resolve_company_filter` → `where={"company": "..."}` metadata filter. Same convention-based enforcement.
3. **Super-admin bypass**: e.g., `backend/api/rag.py:51` — `rag_company_filter = company_name if current_user["role"] != "super_admin" else None`. Scattered across multiple files; intentional but hard to audit.

**Why this is fragile at any team size ≥ 1**: a single forgotten `.filter(company=...)` = cross-tenant leak. Static analysis won't catch it (Python is dynamically typed). Code review catches some, not all. Tests catch it only if you specifically write cross-tenant negative tests — which today's test suite does not.

#### 7.5.2 The Canonical Fix — PostgreSQL Row-Level Security (Rejected)

The textbook answer is Postgres RLS: `CREATE POLICY ... USING (company = current_setting('app.current_company'))`. Auth middleware runs `SET LOCAL app.current_company = 'AcmeCorp'` per request. Forgetting `.filter()` in ORM code becomes harmless — Postgres enforces it.

**Why we rejected RLS for this project**:
- Doesn't cover ChromaDB (no RLS equivalent for vector stores)
- Breaks SQLite dev fallback (RLS is Postgres-only)
- Harder to debug — errors come back as generic Postgres permission failures
- Couples the system to Postgres for tenant safety; portable ORM abstractions break

RLS is the right call for a system with raw SQL everywhere or regulatory compliance requirements. Neither applies here (we verified: only 2 `text(...)` raw SQL call sites, both in health checks).

#### 7.5.3 The Chosen Fix — App-Layer Strong (Phase 0.6)

Six pieces working together, giving coverage equivalent to RLS for our actual threat surface (ORM + ChromaDB):

**(a) SQLAlchemy `before_compile` event listener + ContextVar** (`backend/db/tenant_context.py`, new)

Every ORM query is silently augmented with `WHERE company = :current_company` before hitting the DB. Forgetting `.filter()` becomes harmless. The ContextVar is request-scoped and async-safe.

```python
@event.listens_for(Query, "before_compile", retval=True)
def _auto_filter_by_company(query):
    if _bypass_filter.get(): return query
    company = _current_company.get()
    if company is None:
        raise RuntimeError("Tenant context not set — queries must run inside a request scope")
    for desc in query.column_descriptions:
        model = desc.get("type")
        if model and hasattr(model, "company"):
            query = query.filter(model.company == company)
    return query
```

Strict mode (raise on missing context) fails fast. A stray query run during a background job without a tenant context explodes at development time, not silently leaks at 3am.

**(b) Explicit `bypass_tenant_filter(reason, actor)` context manager for super-admin**

Cross-tenant access is no longer the default behavior of a super-admin session. It's an **opt-in per query**, logged with reason and actor:

```python
with bypass_tenant_filter(reason="list all companies for dashboard", actor=user["email"]):
    all_companies = db.query(Company).all()
```

One `grep bypass_tenant_filter` shows every cross-tenant access point in the codebase. Audit-friendly.

**(c) `TenantScopedRetriever` wrapping ChromaDB** (`chat_pipeline/rag/tenant_scoped_retriever.py`, new)

The retriever exposes `query(query_text, company, top_k)` — no method to query without `company`. You can't bypass it because the API signature doesn't let you:

```python
class TenantScopedRetriever:
    def query(self, query_text: str, company: str, top_k: int = 5):
        if not company:
            raise ValueError("company is required")
        return self._collection.query(
            query_texts=[query_text], n_results=top_k,
            where={"company": company})
```

All RAG code migrates to this wrapper. Raw `collection.query()` is no longer used anywhere except inside `TenantScopedRetriever`.

**(d) Cross-tenant integration tests** (`stress_tests/test_phase0_6_tenancy.py`)

Parameterized tests that log in as Company A, create a resource, log in as Company B, try to GET/DELETE that resource. Assert 403/404. One test per mutation endpoint. This is **defense-in-depth** — even if the event listener fails open, the tests catch leaks before deploy.

**(e) Audit logging** — every `bypass_tenant_filter()` invocation produces a structured log entry `{event: "tenant_bypass", actor, reason, timestamp}`. If there's ever a breach investigation, the breadcrumbs are in Cloud Logging.

**(f) ChromaDB metadata validator** (startup hook) — scan up to 1000 chunks at server boot. If any chunk has a missing or empty `company` field, fail startup. Prevents a data pipeline bug from reaching production.

**(g) Pre-commit hook blocking `db.execute(text(...))`** outside the two allowed files. Raw SQL bypasses the event listener; we don't allow it. If you need raw SQL, it requires explicit PR approval and documentation.

#### 7.5.4 Coverage Comparison

| Property | Before (convention only) | After (app-layer strong) | Postgres RLS |
|---|---|---|---|
| Forgotten `.filter()` in ORM code | ✗ leak | ✓ auto-filtered | ✓ auto-filtered |
| Raw SQL bypass | ✗ leak | ✓ (pre-commit blocks) | ✓ enforced by DB |
| ChromaDB bypass | ✗ leak | ✓ (wrapper API) | ✗ (Chroma has no RLS) |
| Super-admin scope leak | ✗ (scattered bypasses) | ✓ (explicit context manager, logged) | ✓ (role-based policies) |
| Data pipeline mislabeled chunk | ✗ (undetected) | ✓ (startup validator) | ✗ (same problem — RLS doesn't validate data) |
| SQLite dev ergonomics | ✓ works | ✓ works | ✗ breaks |
| Debuggability | High | High (Python traceback) | Low (generic DB error) |

**Verdict**: for this threat surface, app-layer strong is strictly superior to RLS. ChromaDB coverage alone is a decisive advantage — RLS does nothing for vector leaks.

#### 7.5.5 Residual Risks

Even with all six pieces, two risks remain:

1. **The event listener itself**. It's ~30 lines of code that every query flows through. A bug here affects everything. Mitigation: the strict-mode "raise on missing context" makes bugs loud, not silent; integration tests verify the listener works.

2. **ChromaDB ingestion correctness**. If the data pipeline writes a chunk with the wrong `company` metadata, the wrapper dutifully filters by the wrong company forever. Mitigation: startup validator checks labels are present; plan.md Phase 0.6E adds a "chunk → source document → expected company" cross-check at ingestion.

These are the things to monitor in Grafana once Phase 7 is live — specifically, alerts on any query that hits the "bypass" path unexpectedly, and per-tenant document count anomalies in the RAG pipeline dashboard.

### 7.6 Deep Dive: Fault Tolerance and the Resilience Policy Matrix (Phase 0, 6, 6.5)

**Currently present**:
- Retries with exponential backoff on external LLM calls (`chat_pipeline/rag/generator.py:273-304`)
- Provider fallback chain (Mercury → Groq → OpenAI → Local)
- LiveKit FallbackAdapter for STT/TTS (Deepgram → AssemblyAI; Deepgram → Cartesia)
- Health check endpoints (`/health`, `/health/ready`)
- Task-level retries in LLM client (`backend/agents/utils/llm_client.py:124-129`, via tenacity)

**Currently missing** (the Phase 0, 6, 6.5 work):
- **No circuit breakers anywhere**. When Mercury is down, every request spends ~14s on retries before falling to Groq. A circuit breaker would detect the first 3 failures and skip Mercury for 60s.
- **No rate-limit handling** on Mercury/OpenAI (only Groq has it, hardcoded 10s sleep)
- **No retries on voice tool calls** — a single transient network blip crashes the tool
- **Silent SQLite fallback** — Cloud SQL outage → production silently switches to ephemeral SQLite → data written is lost on container restart
- **Server starts even if ChromaDB warmup fails** — the `/health` check passes (DB is fine) but every RAG query fails (no vectors loaded)
- **Thread-unsafe caches and singletons** — `RAGPipeline._cache` and `get_llm_client()` both have race conditions

**The principle**: fault tolerance is a cross-cutting concern, not a feature. But the antidote is *not* "add retries + circuit breakers everywhere." Over-engineered resilience is a real failure mode — it slows debugging, adds surface area, and hides real bugs behind retries.

#### 7.6.1 The Resilience Policy Matrix (Phase 6.5)

Different call types need different patterns. Plan.md Phase 6.5 formalizes this in `backend/utils/resilience.py` as a single source of truth:

| Call type | Timeout | Retry | Exp. backoff | Circuit breaker | Idempotency key |
|---|---|---|---|---|---|
| **External LLM API** (Mercury, Groq, OpenAI) | 8s | 3x | ✓ | ✓ per provider | N/A |
| **External search** (Brave, Deepgram) | 5s | 2x | ✓ | ✓ | N/A |
| **Internal DB queries** | 2s | 1x | ✗ fast-fail | ✗ | N/A |
| **Voice → Backend tool calls** | 8s | 2x | 1s linear | ✗ (own infra) | ✓ REQUIRED |
| **User-facing HTTP chains** | 10s | ✗ let user retry | — | — | ✓ for mutations |
| **GCS sync (data pipeline)** | 300s | 3x | ✓ | ✗ | ✗ (re-syncable) |
| **LiveKit provider chains** | 5s | ✗ (LiveKit handles) | — | ✓ | — |

**Why the asymmetry**:

- **External APIs** need circuit breakers because repeated retries against a down provider waste budget and delay fallback. They *don't* need idempotency keys because they're read-only (LLM inference has no persistent side effect).
- **Internal DB queries** fast-fail because retrying a DB that's under load makes it worse. The right response is "bubble up, let the request fail, let the client retry" — not to hammer the DB.
- **Voice tool calls** use idempotency keys because they mutate state via POST requests *and* we retry them — the combination is only safe with keys (Phase 0.5).
- **User-facing chains** don't retry because the user retrying is better signal than the client silently retrying (the user knows if they actually wanted the action).
- **GCS sync** doesn't need idempotency because re-sync is itself an idempotent operation (`rsync`).
- **LiveKit chains** don't retry ourselves because LiveKit's FallbackAdapter already handles this inside the library.

#### 7.6.2 The Implementation Pattern

```python
@resilient(policy="external_llm")
async def call_mercury(prompt: str) -> str:
    # Timeout=8s, 3 retries with exp backoff, mercury circuit breaker applied
    async with httpx.AsyncClient() as c:
        r = await c.post(MERCURY_URL, json={"prompt": prompt})
        return r.json()["text"]

@resilient(policy="internal_db")
def load_user(email: str) -> User:
    # Timeout=2s, 1 retry, fast-fail
    return db.query(User).filter_by(email=email).one()
```

One decorator per call site. Changing a policy (e.g., bumping LLM timeout to 10s) updates behavior everywhere automatically. Compliance is checkable via grep (Phase 6.5 test verifies no unwrapped `httpx`/`requests` calls outside the resilience module).

#### 7.6.3 The Circuit Breaker Mechanics

Three states: **closed** (calls pass through), **open** (calls fail fast without attempting), **half-open** (one test call after cooldown). Canonical thresholds:
- 3 consecutive failures → open
- 60s cooldown → half-open
- One success in half-open → closed; one failure → open for another 60s

**Per-provider**, not global. Mercury being down doesn't trip the Groq breaker. State is exposed as a Prometheus gauge (`circuit_breaker_state{provider="mercury"}`) so the Grafana resilience dashboard shows it live.

---

### 7.7 Deep Dive: Observability Architecture (Phase 7)

**Why this is its own deep dive**: observability is not a nice-to-have add-on. It's the substrate that makes everything else debuggable. Without it, "latency went up yesterday" is a guessing game; with it, the answer is a timestamped graph.

#### 7.7.1 The Stack

- **Instrumentation**: `prometheus_client` library inline in backend + voice pipeline code. `/metrics` endpoint on each service.
- **Collection**: Grafana Agent (sidecar/subprocess) scrapes `/metrics` endpoints, ships to Grafana Cloud via `remote_write`.
- **Storage**: Grafana Cloud free tier — 10K metrics, 50GB logs, 14-day retention. $0/mo.
- **Visualization**: Grafana dashboards provisioned as JSON in `grafana/dashboards/`.
- **Alerting**: Grafana Alertmanager, with SLO-based burn-rate alerts.

**Why Grafana Cloud free tier over alternatives**:
- Self-hosting Prometheus + Grafana on GCE costs ~$25/mo, requires persistent storage, and is operational overhead for a student project
- Google Managed Prometheus (GMP) is fine but locks you to GCP for observability
- Grafana Cloud gives you industry-standard tooling (PromQL, Grafana dashboards — exactly what you'd see at any tech company) at zero cost for this scale

#### 7.7.2 The Four Golden Signals (per Google's SRE book)

Every service instruments these four. Nothing else matters if these aren't in place:

1. **Latency** — per endpoint, p50/p95/p99 histograms
2. **Traffic** — request rate by endpoint, by tenant
3. **Errors** — error rate by endpoint, by error class (4xx vs 5xx)
4. **Saturation** — CPU, memory, DB connection pool fill, request queue depth

Plus domain-specific metrics:

- **Voice**: per-stage histograms (VAD, STT, LLM TTFT, TTS TTFB), end-to-end latency histogram, active session gauge
- **RAG**: retrieval latency, generation latency per backend, cache hit rate, documents returned
- **LLM providers**: latency per provider, failure rate per error class, circuit breaker state
- **Tenants**: every metric above gets a `company` label (detects noisy-neighbor patterns)

#### 7.7.3 SLOs Before Dashboards

This is the part most people skip. Dashboards are pretty but useless without explicit targets. Plan.md Phase 7E defines SLOs in `grafana/slo.yaml`:

```yaml
- name: voice_p95_latency
  target: 1.5  # seconds
  indicator: histogram_quantile(0.95, voice_e2e_latency_seconds)
  error_budget_days: 30
  burn_rate_alerts: [1x, 6x, 14.4x]  # standard multi-window multi-burn-rate
```

SLOs have two outputs:
- **The current value** (is the 30-day p95 actually <1.5s?)
- **The remaining error budget** (0.5% × 30 days = 3.6 hours allowed over target; how many have we used?)

Error-budget-based alerting is superior to threshold alerting because it distinguishes between "one slow hour on a Tuesday night" (nobody cares) and "steadily burning budget all week" (page someone).

#### 7.7.4 The Six Dashboards

Each dashboard answers one question:

1. **service_health** — "Is the system healthy right now?" (four golden signals)
2. **voice_pipeline** — "Where is voice latency going?" (per-stage breakdown)
3. **rag_pipeline** — "Is retrieval or generation the bottleneck?" (per-backend comparison)
4. **tenants** — "Is one tenant causing problems for others?" (noisy-neighbor detection)
5. **stress_test** — "At what load does the system degrade?" (live Locust + system metrics)
6. **resilience** — "Which providers are healthy? Which circuits are open?" (circuit breaker states, retry rates)

The **stress_test** dashboard is particularly important for plan.md Phases 1-6: the planned stress tests will write their load pattern to Prometheus, and you can watch system p95 latency climb in real-time as load increases. This is the graph you need to verify "Phase 1 saved 500ms p95 under load" rather than just "Phase 1 saved 500ms p95 in a cold test."

#### 7.7.5 Label Cardinality Discipline

One subtle failure mode worth calling out explicitly: **label cardinality explosion**. Prometheus creates a separate time series for every combination of label values. Labels like `user_id` or `request_id` with unbounded values will crash Prometheus.

Rules enforced in Phase 7 tests:
- `company` label is bounded (19 tenants today, ~200 expected max) — safe
- `endpoint` label is bounded (~20 endpoints) — safe
- `status` label is bounded (5-6 HTTP status classes) — safe
- `user_id`, `request_id`, `conversation_id`, `ticket_id` — **NEVER** used as labels; they go into logs + traces instead

Test in `test_phase7_observability.py::test_label_cardinality_bounded` verifies this at CI time.

#### 7.7.6 Correlation IDs as the Bridge Between Metrics and Logs

Metrics answer "how often and how slow." Logs answer "what happened to *this* specific request." Correlation IDs (`X-Request-ID`) tie them together.

Every request gets a UUID at the ingress (or reuses one from the client). It's propagated through:
- Log lines (via contextvar)
- All downstream HTTP calls (including Modal → Backend)
- ChromaDB query logs
- LLM provider call logs

When voice latency spikes on the dashboard, you take a sample request ID from the slow trace, grep the logs with it, and see the exact sequence of operations that made it slow. Without correlation IDs, you're guessing.

---

## 8. What This Means for the Plan

Now that the architecture is in focus, `plan.md`'s phases map cleanly onto the components above. The ordering is **correctness first, observability before optimization, latency last** — because you cannot optimize what you cannot measure.

| Plan Phase | Component Touched | Principle Applied | Deep Dive |
|---|---|---|---|
| **Phase 0 (Critical Resilience)** | DB, cache, singleton, auth, startup | Fail-closed over fail-open; thread-safety; validate preconditions | §7.6 |
| **Phase 0.5 (Idempotency)** | Mutation endpoints; voice tool calls | Safe retries require idempotency keys; isolated per tenant | §6.2 Principle 2 |
| **Phase 0.6 (Multi-Tenancy Hardening)** | All ORM queries; ChromaDB retriever; admin endpoints | Defense in depth; enforce at API shape not by convention | **§7.5** |
| **Phase 0.7 (JWT Refresh Tokens)** | Auth subsystem; frontend axios; voice session tokens | Short-lived access + revocable refresh + rotation-on-use | §3.1 |
| **Phase 6.5 (Resilience Matrix)** | All external call sites | Different patterns for different call types; single source of truth | §7.6.1 |
| **Phase 7 (Observability) — runs before Phase 1** | Backend + voice pipeline instrumentation; Grafana dashboards; SLOs | Four Golden Signals; label cardinality discipline; correlation IDs bridge metrics↔logs | **§7.7** |
| **[BASELINE MEASUREMENT WEEK]** | — | Capture "before" metrics so Phase 1-6 has comparisons | — |
| **Phase 1 (Quick Wins)** | Voice → Backend HTTP surface; RAG generation | Connection pooling; config-driven overrides; voice-specific path | §7.1, §7.2 |
| **Phase 2 (Streaming)** | RAG API surface; Voice tool calls | Pipelining — overlap generation with consumption | §7.1 |
| **Phase 3 (Infra)** | Modal deployment; Cloud Run deployment | Warm-up amortization; post-deploy health verification | §7.4 |
| **Phase 4 (Caching + Data Pipeline Resilience)** | RAG data_loader; data pipeline stages | Cache the slow path; atomic batch writes; checkpointing | §7.2 |
| **Phase 5 (Voice Fast Path)** | RAG prompt templates; prefetch endpoint; voice agent | Output-shape optimization; speculative prefetch on partial input | §7.2 |
| **Phase 6 (Observability Cleanup)** | Circuit breakers integrated with Grafana; connection pooling | Circuit breaking via resilience matrix (6.5); bounded pools | §7.6 |

**Scaling-aware additions** (from §6):
- ✓ **Idempotency keys** — now Phase 0.5, not an afterthought
- **Watch Chokepoint #5** (instance-local cache fragmentation) when Phase 3A (`keep_warm=1`) goes live. The signature is counterintuitive — p95 latency goes *up* when Cloud Run adds instances
- Prefer **scheduled warming** over permanent `min_instances=1` for the Cloud Run backend

**Where this leaves you**: you now have the architecture model in your head, and you can make the plan.md changes knowing *why* each change makes sense given the rest of the system, not just *that* it saves X milliseconds. That's the difference between optimizing a system and tuning a knob.

**Total scope**: ~30 working days (~6 weeks solo). This is no longer "make voice faster" — it's productionizing the platform end to end: correctness, security, observability, and then latency, in that order.

---

## Appendix — Citations

Every specific claim here has a code citation. Spot-check any of these:

- Models & enums: `backend/db/models.py:1-241`
- JWT config & auth: `backend/api/auth.py:18-64`
- Intent detection: `backend/api/unified_agent.py:57-162`
- RAG pipeline orchestration: `chat_pipeline/rag/pipeline.py:188-355`
- Company filter resolution: `chat_pipeline/rag/data_loader.py:244-285`
- LLM generation fallback chain: `chat_pipeline/rag/generator.py:451-519`
- PTO state machine: `backend/agents/pto/agent.py:40-103`
- Voice worker (Modal): `voice_pipeline/modal_deploy.py:62-116`
- Voice BackendClient: `voice_pipeline/scripts/main.py:80-112`
- Voice tool call (RAG): `voice_pipeline/scripts/main.py:362-435`
- ChromaDB bootstrap from GCS: `chat_pipeline/rag/data_loader.py:119-197`
- Data pipeline runner: `data_pipeline/scripts/pipeline_runner.py`
- Monitoring middleware: `backend/monitoring/middleware.py:12-56`
- Cloud Run deploy: `.github/workflows/deploy-backend.yml`
