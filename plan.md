# FrontShiftAI Production Readiness Plan: Resilience + Observability + Latency + Workflow Durability

## Context

This plan solves five problems, in deliberate order:

1. **Correctness & Security**: 30 fault tolerance gaps identified (8 critical), idempotency violations in retry paths, weak multi-tenant isolation, 1-year JWT TTL.
2. **Observability**: No Prometheus/Grafana instrumentation today. Can't measure optimizations without a baseline.
3. **Resilience**: Systematic retries, circuit breakers, and exponential backoff applied via a resilience policy matrix — not scattered ad-hoc.
4. **Latency + Perceived Latency**: Voice pipeline at ~3.4s end-to-end; target sub-1.5s. Streaming extended beyond RAG to PTO/HR agents for continuous user feedback.
5. **Workflow Durability**: LangGraph state today is request-scoped — multi-turn conversations can't resume, workflows can't wait for admin approval. `PostgresSaver` checkpointer addresses this.

**Ordering principle**: correctness first (Phase 0 series), observability before optimization (Phase 7 moves up to run *before* Phases 1-6), latency work last so every optimization has a before/after Grafana graph. You cannot optimize what you cannot measure.

All stress tests follow the pattern in `backend/agents/test_agents/benchmark_agents.py`: async Python, percentile reporting (p50/p95/p99), PASS/FAIL assertions. Every stress test also **emits Prometheus metrics** so the results appear live on Grafana dashboards, not just in pytest output.

---

## New Files

| File | Purpose |
|------|---------|
| `stress_tests/requirements.txt` | httpx, pytest-asyncio, locust, prometheus-client |
| `stress_tests/conftest.py` | Shared fixtures: backend URL, JWT, LatencyReport, Prometheus emitter |
| `stress_tests/test_phase0_resilience.py` | Critical resilience fix validation |
| `stress_tests/test_phase0_5_idempotency.py` | Idempotency key behavior under retry |
| `stress_tests/test_phase0_6_tenancy.py` | Cross-tenant access attempts (negative tests) |
| `stress_tests/test_phase0_7_jwt.py` | Access/refresh token lifecycle |
| `stress_tests/test_phase1_quick_wins.py` | HTTP pooling, VAD, timeout, max_tokens, Groq benchmarks |
| `stress_tests/test_phase2_streaming.py` | SSE streaming endpoint latency and throughput |
| `stress_tests/test_phase3_infra.py` | Modal cold start, deployment health, worker recovery |
| `stress_tests/test_phase4_caching.py` | Company metadata cache, pipeline cache thread-safety |
| `stress_tests/test_phase5_voice_path.py` | Voice prompt, prefetch, voice agent resilience |
| `stress_tests/test_phase5_5_checkpointing.py` | Multi-turn resume, thread isolation, tenant-scoped checkpoints, cleanup |
| `stress_tests/test_phase6_5_resilience_matrix.py` | Resilience matrix compliance per call type |
| `stress_tests/test_phase7_observability.py` | Prometheus endpoint scraping, metric presence, label cardinality |
| `stress_tests/test_e2e_latency.py` | Full pipeline benchmark + fault injection |
| `stress_tests/locustfile.py` | Sustained load test — exports metrics to Prometheus for live Grafana view |
| `backend/db/tenant_context.py` | ContextVar + SQLAlchemy event listener for tenant filtering |
| `backend/db/models.py` (edit) | Add `IdempotencyRecord` + `RefreshToken` models |
| `backend/api/idempotency.py` | Idempotency key middleware / decorator |
| `backend/observability/metrics.py` | Prometheus registry + instruments (counters, histograms) |
| `backend/observability/tracing.py` | Correlation ID generation + propagation |
| `chat_pipeline/rag/tenant_scoped_retriever.py` | Wrapper enforcing `company` arg on every ChromaDB query |
| `backend/agents/utils/checkpointer.py` | LangGraph `PostgresSaver` singleton (Phase 5.5) |
| `voice_pipeline/observability/metrics.py` | Prometheus metrics for STT/LLM/TTS stages |
| `grafana/dashboards/service_health.json` | Golden signals dashboard (provisioned) |
| `grafana/dashboards/voice_pipeline.json` | Per-stage voice latency dashboard |
| `grafana/dashboards/rag_pipeline.json` | RAG retrieval, generation, cache hit dashboard |
| `grafana/dashboards/tenants.json` | Per-company volume + latency (noisy-neighbor detection) |
| `grafana/dashboards/stress_test.json` | Live Locust load + system metrics overlay |
| `grafana/dashboards/resilience.json` | Circuit breaker state, retry counts, fallback usage |
| `grafana/slo.yaml` | SLO definitions consumed by alerting rules |
| `.github/workflows/pre_commit_tenant_check.yml` | Lint rule blocking `text(...)` raw SQL |

---

## Shared Stress Test Infrastructure: `stress_tests/conftest.py`

```python
import time, statistics, json, os, asyncio
import httpx, pytest

BACKEND_URL = os.getenv("STRESS_TEST_BACKEND_URL", "http://localhost:8000")
JWT_TOKEN = os.getenv("STRESS_TEST_JWT")

@pytest.fixture
def backend_url():
    return BACKEND_URL

@pytest.fixture
def auth_headers():
    assert JWT_TOKEN, "Set STRESS_TEST_JWT env var"
    return {"Authorization": f"Bearer {JWT_TOKEN}"}

@pytest.fixture
async def http_client(auth_headers):
    """Persistent httpx client — tests connection pooling benefit."""
    client = httpx.AsyncClient(
        base_url=BACKEND_URL, headers=auth_headers, timeout=15.0,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    yield client
    await client.aclose()

class LatencyReport:
    """Collects timings and prints percentile report with PASS/FAIL."""
    def __init__(self, name: str, target_p50: float, target_p95: float):
        self.name, self.times = name, []
        self.target_p50, self.target_p95 = target_p50, target_p95

    def record(self, seconds: float):
        self.times.append(seconds)

    def report(self) -> dict:
        s = sorted(self.times)
        n = len(s)
        stats = {
            "count": n, "avg": statistics.mean(s), "p50": statistics.median(s),
            "p95": s[int(n * 0.95)] if n >= 20 else s[-1],
            "p99": s[int(n * 0.99)] if n >= 100 else s[-1],
            "min": s[0], "max": s[-1],
            "std": statistics.stdev(s) if n > 1 else 0,
        }
        print(f"\n{'='*60}\n  {self.name} ({n} samples)\n{'='*60}")
        for k, v in stats.items():
            print(f"  {k:>5}: {v:.3f}s" if k != "count" else f"  {k:>5}: {v}")
        p50_ok = stats["p50"] <= self.target_p50
        p95_ok = stats["p95"] <= self.target_p95
        print(f"\n  P50 <= {self.target_p50}s: {'PASS' if p50_ok else 'FAIL'}")
        print(f"  P95 <= {self.target_p95}s: {'PASS' if p95_ok else 'FAIL'}\n{'='*60}\n")
        return stats

    def assert_targets(self):
        stats = self.report()
        assert stats["p50"] <= self.target_p50
        assert stats["p95"] <= self.target_p95
```

---

## Phase 0: Critical Resilience Fixes (Day 1 — before any latency work)

These are production safety issues. Deploying latency optimizations on top of these gaps is building speed on a cracked foundation.

### 0A. Prevent Silent SQLite Fallback
**File**: `backend/db/connection.py:18-27`

**Current**: If PostgreSQL is unreachable, silently falls back to ephemeral SQLite. All data lost on container restart.

**Change**: In production (`ENVIRONMENT=production`), raise an error instead of falling back. SQLite fallback only allowed in development/test.

```python
except Exception as e:
    if os.getenv("ENVIRONMENT") == "production":
        raise RuntimeError(f"PostgreSQL required in production but unavailable: {e}")
    logger.warning("PostgreSQL unavailable, falling back to SQLite (dev only)")
    # ... sqlite fallback ...
```

### 0B. Block Server Startup on ChromaDB/Embedding Failure
**File**: `backend/main.py:56-58, 101-105`

**Current**: ChromaDB and embedding warmup failures are caught and ignored — server appears healthy but all RAG queries fail.

**Change**: In the lifespan context manager, if ChromaDB or embedding warmup fails, raise so the server doesn't start. Cloud Run's health check will detect the failure and roll back.

```python
try:
    warmup_chromadb()
    warmup_embedding_model()
except Exception as e:
    logger.critical(f"Startup failed — RAG unavailable: {e}")
    raise  # Prevent server from starting in broken state
```

### 0C. Fix Health Check Connection Leak
**File**: `backend/api/health.py:14-17`

**Current**: `SessionLocal()` created without context manager. If query throws, `db.close()` never called.

**Change**: Use context manager:
```python
try:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "healthy"}
except Exception:
    raise HTTPException(status_code=503, detail="Database unreachable")
```

### 0D. Add Retries to Voice Tool Calls
**File**: `voice_pipeline/scripts/main.py:374-435`

**Current**: `query_info`, `website_search`, `request_pto`, `create_hr_ticket` have zero retries. One transient error and the tool raises.

**Change**: Add a retry wrapper with 2 attempts and 1s backoff. On final failure, return a graceful message instead of raising:

```python
async def _retry_post(self, path, payload, timeout, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            return await self.backend.post(path, payload, timeout=timeout)
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Tool call {path} failed after {max_retries+1} attempts: {e}")
                return {"answer": "I'm having trouble looking that up right now. Please try again.",
                        "sources": [], "error": True}
            await asyncio.sleep(1.0 * (attempt + 1))
```

### 0E. Fix RAG Pipeline Cache Race Condition
**File**: `chat_pipeline/rag/pipeline.py:202, 295-308, 385-388`

**Current**: `OrderedDict` cache read/written without lock. Concurrent async requests can corrupt the dict.

**Change**: Add `asyncio.Lock` (or `threading.Lock` if sync) around cache operations:

```python
def __init__(self, ...):
    self._cache = OrderedDict()
    self._cache_lock = threading.Lock()

# In run():
with self._cache_lock:
    cached = self._cache.get(cache_key)
    if cached is not None:
        self._cache.move_to_end(cache_key)
        ...
```

### 0F. Fix Global LLM Client Singleton Thread-Safety
**File**: `backend/agents/utils/llm_client.py:284-286`

**Current**: `get_llm_client()` has no lock — two threads can create two instances.

**Change**:
```python
_llm_lock = threading.Lock()
def get_llm_client() -> AgentLLMClient:
    global _llm_client
    if _llm_client is None:
        with _llm_lock:
            if _llm_client is None:
                _llm_client = AgentLLMClient()
    return _llm_client
```

### 0G. Validate Voice Agent Auth Before Starting Session
**File**: `voice_pipeline/scripts/main.py:596-627`

**Current**: If `wait_for_user_token()` times out, agent starts with service account token. Backend calls fail later with confusing auth errors.

**Change**: If no valid user token after timeout, send an apology message and exit the session gracefully instead of continuing in a broken state.

### 0H. Add 429 Rate Limit Handling to Mercury and OpenAI
**File**: `chat_pipeline/rag/generator.py:273-304` (Mercury), `383-421` (OpenAI)

**Current**: Only Groq has 429 handling (hardcoded 10s). Mercury and OpenAI treat 429 as generic error, burning retry attempts.

**Change**: Add `Retry-After` header parsing, fallback to exponential backoff on 429 for all providers.

#### Stress Test: `test_phase0_resilience.py`

```python
def test_no_sqlite_in_production(monkeypatch):
    """Production must never fall back to SQLite."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nonexistent")
    from backend.db.connection import init_db
    with pytest.raises(RuntimeError, match="PostgreSQL required"):
        init_db()

def test_health_check_no_connection_leak(client):
    """Health endpoint must not leak DB connections under repeated calls."""
    for _ in range(100):
        resp = client.get("/health")
        # Should not accumulate open connections
    # If we get here without pool exhaustion, pass
    resp = client.get("/health")
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_voice_tool_retry_on_transient_failure():
    """Voice tool calls should retry on transient errors and return graceful fallback."""
    call_count = 0
    async def failing_post(path, payload, timeout):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("Connection refused")
        return {"answer": "Test answer", "sources": []}

    # Mock BackendClient.post with failing_post
    # Assert: call_count == 3 (2 retries + 1 success)
    # Assert: result contains "answer" key

@pytest.mark.asyncio
async def test_rag_cache_thread_safety():
    """Concurrent cache operations should not corrupt the OrderedDict."""
    from chat_pipeline.rag.pipeline import RAGPipeline
    pipeline = RAGPipeline(cache_size=10)

    async def concurrent_query(i):
        # Simulate concurrent cache reads/writes
        result = pipeline.run(query=f"test query {i % 5}", top_k=3, company_name="TestCo")
        return result is not None

    results = await asyncio.gather(*[concurrent_query(i) for i in range(50)])
    assert all(results), "Some concurrent queries failed — possible cache corruption"

def test_voice_agent_rejects_invalid_token():
    """Voice agent should not start a session with an empty/invalid token."""
    # Verify that entrypoint exits gracefully when wait_for_user_token returns None
    pass  # Implementation depends on mock setup

def test_chromadb_failure_blocks_startup():
    """Server should not start if ChromaDB warmup fails."""
    # Monkeypatch chromadb to raise, verify FastAPI app fails to start
    pass
```

---

## Phase 0.5: Idempotency Keys (2 days)

**Why this is critical and must ship with Phase 0.** Phase 0D adds retries to voice tool calls. Without idempotency keys, a transient network error *after* a successful PTO-creation response triggers a retry that creates a duplicate PTO request. We're about to turn a transient glitch into a silent data-integrity bug.

### 0.5A. Add IdempotencyRecord Model
**File**: `backend/db/models.py`

```python
class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    key = Column(String, primary_key=True)  # UUID from client
    company = Column(String, index=True, nullable=False)
    endpoint = Column(String, nullable=False)
    response_body = Column(Text, nullable=False)  # JSON
    status_code = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    # TTL: records older than 24h purged by cron
```

### 0.5B. Idempotency Middleware
**File**: `backend/api/idempotency.py`

Dependency that checks for `Idempotency-Key` header on POST/PUT/DELETE. If present, looks up existing record scoped by `(key, company)`. On hit: returns cached response. On miss: proceeds, then stores response atomically before returning.

```python
async def idempotent(request: Request, current_user = Depends(get_current_user), db = Depends(get_db)):
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None  # Optional — or raise if you want to require it on mutations

    existing = db.query(IdempotencyRecord).filter_by(
        key=key, company=current_user["company"]).first()
    if existing:
        return JSONResponse(json.loads(existing.response_body),
                            status_code=existing.status_code)

    # Store on response (use middleware or background task)
    return {"key": key, "should_store": True}
```

### 0.5C. Apply to Mutation Endpoints
- `POST /api/pto/chat` (creates PTORequest)
- `POST /api/hr_ticket/chat` (creates HRTicket)
- `POST /api/chat/message` (persists Conversation + Messages)

### 0.5D. Voice Agent Generates Keys
**File**: `voice_pipeline/scripts/main.py`

Each tool call generates a UUID at the start and passes it in the `Idempotency-Key` header. The Phase 0D retry wrapper reuses the same key — that's the entire point.

```python
idempotency_key = str(uuid.uuid4())
headers = {"Idempotency-Key": idempotency_key}
return await self._retry_post(path, payload, timeout, headers=headers)
```

### 0.5E. Cleanup Cron
**File**: `backend/jobs/tasks.py`

Daily task: `DELETE FROM idempotency_records WHERE created_at < NOW() - INTERVAL '24 hours'`.

#### Stress Test: `test_phase0_5_idempotency.py`

```python
@pytest.mark.asyncio
async def test_duplicate_key_returns_cached_response(http_client):
    """Same idempotency key must return same response, NOT create a second record."""
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}

    resp1 = await http_client.post("/api/hr_ticket/chat",
        json={"message": "Need to discuss benefits"}, headers=headers)
    resp2 = await http_client.post("/api/hr_ticket/chat",
        json={"message": "Need to discuss benefits"}, headers=headers)

    assert resp1.status_code == resp2.status_code
    assert resp1.json()["ticket_id"] == resp2.json()["ticket_id"]  # SAME ticket

@pytest.mark.asyncio
async def test_retry_after_transient_error_does_not_duplicate():
    """Simulated: tool call succeeds DB-side, response lost, client retries. Must not duplicate."""
    # Mock a BackendClient that fails on first response but succeeds on retry
    # Assert only ONE PTORequest exists after both attempts
    pass

@pytest.mark.asyncio
async def test_different_keys_create_separate_records(http_client):
    """Different keys must create separate resources even with identical bodies."""
    payload = {"message": "Need leave"}
    r1 = await http_client.post("/api/hr_ticket/chat", json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())})
    r2 = await http_client.post("/api/hr_ticket/chat", json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r1.json()["ticket_id"] != r2.json()["ticket_id"]

@pytest.mark.asyncio
async def test_cross_tenant_key_isolation():
    """Same idempotency key used by different tenants must not collide."""
    # Tenant A uses key K, Tenant B uses key K
    # Must produce two separate responses, no cross-tenant lookup
    pass
```

---

## Phase 0.6: Multi-Tenant Isolation — App-Layer Strong Version (4 days)

**Why**: Today's isolation = "every developer must remember `.filter(company=...)` on 87 query sites." That's fragile at any team size >1. The app-layer strong approach uses SQLAlchemy event listeners + a tenant-scoped ChromaDB wrapper to make tenant leakage *much* harder to write.

### 0.6A. Tenant Context (ContextVar)
**File**: `backend/db/tenant_context.py` (new)

```python
from contextvars import ContextVar
from contextlib import contextmanager
from sqlalchemy import event
from sqlalchemy.orm import Query
import logging

logger = logging.getLogger(__name__)
_current_company: ContextVar[str | None] = ContextVar("current_company", default=None)
_bypass_filter: ContextVar[bool] = ContextVar("bypass_filter", default=False)

@event.listens_for(Query, "before_compile", retval=True)
def _auto_filter_by_company(query):
    if _bypass_filter.get():
        return query
    company = _current_company.get()
    if company is None:
        raise RuntimeError(
            "Tenant context not set — queries must run inside a request scope. "
            "If this is a super-admin or background job, use bypass_tenant_filter().")
    for desc in query.column_descriptions:
        model = desc.get("type")
        if model and hasattr(model, "company"):
            query = query.filter(model.company == company)
    return query

@contextmanager
def bypass_tenant_filter(reason: str, actor: str):
    logger.warning(f"TENANT FILTER BYPASS: actor={actor}, reason={reason}",
                   extra={"audit": True, "event": "tenant_bypass"})
    token = _bypass_filter.set(True)
    try:
        yield
    finally:
        _bypass_filter.reset(token)

def set_tenant_context(company: str, is_super_admin: bool = False):
    _current_company.set(company)
    _bypass_filter.set(is_super_admin)
```

### 0.6B. Middleware Wiring
**File**: `backend/main.py`

```python
@app.middleware("http")
async def tenant_context_middleware(request: Request, call_next):
    try:
        user = get_current_user_from_request(request)  # decode JWT
        set_tenant_context(user["company"], user["role"] == "super_admin")
    except Exception:
        pass  # Unauthenticated routes (/health, /api/auth/login) skip tenant filter
    return await call_next(request)
```

### 0.6C. Migrate Existing Query Sites
Remove now-redundant `.filter(Model.company == ...)` calls from the 87 identified sites. Event listener handles it automatically.

**Be careful with**: super-admin endpoints (`backend/api/admin.py`). These must wrap queries in `bypass_tenant_filter(reason=..., actor=user["email"])`.

### 0.6D. Tenant-Scoped ChromaDB Retriever
**File**: `chat_pipeline/rag/tenant_scoped_retriever.py` (new)

```python
class TenantScopedRetriever:
    def __init__(self, collection):
        self._collection = collection

    def query(self, query_text: str, company: str, top_k: int = 5):
        if not company or not isinstance(company, str):
            raise ValueError("company is required")
        return self._collection.query(
            query_texts=[query_text],
            n_results=top_k,
            where={"company": company},
        )
```

Migrate `chat_pipeline/rag/retriever.py` to accept a `TenantScopedRetriever` instead of raw collection. No method to "query without company" exists — the API enforces the constraint.

### 0.6E. ChromaDB Metadata Validator (Startup Check)
**File**: `backend/main.py` lifespan

```python
def validate_chromadb_tenant_labels(collection):
    sample = collection.peek(limit=1000)
    for meta in sample.get("metadatas", []):
        if not meta.get("company"):
            raise RuntimeError(f"Unlabeled ChromaDB chunk detected: {meta}")
```

Blocks startup if any chunk lacks a company label. Prevents a data pipeline bug from reaching production.

### 0.6F. Raw SQL Pre-Commit Lint
**File**: `.github/workflows/pre_commit_tenant_check.yml` (or `.pre-commit-config.yaml`)

Block `db.execute(text(` outside the two allowed files (`backend/db/connection.py`, `backend/api/health.py`) with a custom grep-based hook. If raw SQL is ever needed, require explicit PR approval.

#### Stress Test: `test_phase0_6_tenancy.py`

```python
@pytest.mark.parametrize("endpoint,method,body", [
    ("/api/pto/chat", "POST", {"message": "need time off"}),
    ("/api/hr_ticket/chat", "POST", {"message": "HR question"}),
    ("/api/chat/conversations/{id}", "GET", None),
    ("/api/chat/conversations/{id}", "DELETE", None),
])
def test_cross_tenant_access_denied(client, endpoint, method, body):
    """Company B cannot access Company A's resources."""
    a_token = login_as("user@company-a.com")
    resource_id = create_resource_as(a_token, endpoint, body)

    b_token = login_as("user@company-b.com")
    path = endpoint.format(id=resource_id) if "{id}" in endpoint else endpoint
    resp = client.request(method, path, headers={"Authorization": f"Bearer {b_token}"})
    assert resp.status_code in (403, 404), f"{endpoint} leaked across tenants!"

def test_forgotten_filter_is_auto_applied():
    """A query without explicit .filter(company=...) should still be scoped."""
    from backend.db.tenant_context import set_tenant_context
    from backend.db.models import PTORequest

    set_tenant_context("CompanyA")
    results = db.query(PTORequest).all()  # No manual filter!
    assert all(r.company == "CompanyA" for r in results)

def test_query_without_context_raises():
    """Queries outside a request scope must fail loudly."""
    with pytest.raises(RuntimeError, match="Tenant context not set"):
        db.query(PTORequest).all()

def test_super_admin_bypass_is_logged(caplog):
    """Super admin bypass must produce an audit log entry."""
    with bypass_tenant_filter(reason="list companies", actor="admin@group9.com"):
        db.query(Company).all()
    assert any("TENANT FILTER BYPASS" in r.message for r in caplog.records)

def test_chromadb_retriever_requires_company():
    """TenantScopedRetriever.query must reject missing company."""
    retriever = TenantScopedRetriever(collection=mock_collection)
    with pytest.raises(ValueError):
        retriever.query("test", company="", top_k=3)
    with pytest.raises(ValueError):
        retriever.query("test", company=None, top_k=3)
```

---

## Phase 0.7: JWT Access + Refresh Tokens (2 days)

**Why**: Current 1-year JWT TTL = stolen token is valid for a year with no revocation. Industry standard = short-lived access tokens + revocable refresh tokens.

### 0.7A. Shorten Access Token TTL
**File**: `backend/api/auth.py:23`

```python
ACCESS_TOKEN_EXPIRE_MINUTES = 60      # 1 hour (was 1 year)
REFRESH_TOKEN_EXPIRE_DAYS = 30        # 30 days
```

### 0.7B. Add RefreshToken Model
**File**: `backend/db/models.py`

```python
class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_email = Column(String, ForeignKey("users.email"), index=True, nullable=False)
    company = Column(String, nullable=False)  # for tenant scoping
    token_hash = Column(String, unique=True, nullable=False)  # store hash, not raw
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)
    rotated_from = Column(String, nullable=True)  # previous token id (for theft detection)
```

### 0.7C. Login Returns Both Tokens
**File**: `backend/api/auth.py`

```python
@router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = validate_credentials(request.email, request.password, db)
    access = create_access_token({"sub": user.email, "company": user.company,
                                  "role": user.role, "name": user.name})
    refresh = issue_refresh_token(user, db)
    return {"access_token": access, "refresh_token": refresh,
            "token_type": "bearer", "expires_in": 3600}
```

### 0.7D. Refresh Endpoint
**File**: `backend/api/auth.py`

```python
@router.post("/refresh")
def refresh(request: RefreshRequest, db: Session = Depends(get_db)):
    token_record = db.query(RefreshToken).filter_by(
        token_hash=hash_token(request.refresh_token)).first()

    if not token_record or token_record.revoked_at or token_record.expires_at < datetime.utcnow():
        raise HTTPException(401, "Invalid or expired refresh token")

    # ROTATION: revoke old, issue new. If old token is used again, all tokens in chain revoked (theft detection).
    token_record.revoked_at = datetime.utcnow()
    user = db.query(User).filter_by(email=token_record.user_email).one()
    new_access = create_access_token({"sub": user.email, "company": user.company, ...})
    new_refresh = issue_refresh_token(user, db, rotated_from=token_record.id)
    db.commit()
    return {"access_token": new_access, "refresh_token": new_refresh, "expires_in": 3600}
```

### 0.7E. Logout Endpoint
**File**: `backend/api/auth.py`

```python
@router.post("/logout")
def logout(request: LogoutRequest, db: Session = Depends(get_db)):
    token_record = db.query(RefreshToken).filter_by(
        token_hash=hash_token(request.refresh_token)).first()
    if token_record:
        token_record.revoked_at = datetime.utcnow()
        db.commit()
    return {"status": "logged out"}
```

### 0.7F. Frontend Axios Interceptor
**File**: `frontend/src/services/api.js` (or equivalent)

```javascript
axios.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401 && !error.config._retry) {
      error.config._retry = true;
      const { data } = await axios.post("/api/auth/refresh",
        { refresh_token: localStorage.getItem("refresh_token") });
      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token);
      error.config.headers.Authorization = `Bearer ${data.access_token}`;
      return axios(error.config);
    }
    return Promise.reject(error);
  }
);
```

### 0.7G. Voice Pipeline Token Handling
**File**: `voice_pipeline/scripts/main.py`

Voice worker has a different threat model (ephemeral, server-side). Use a **longer-lived service token** (6 hours) generated by the session-creation endpoint, or pass access token + refresh token and auto-refresh in the worker.

Pragmatic choice: session endpoint issues a voice-scoped access token (6h TTL, session-specific scope) that the worker uses directly. No refresh needed because the session is shorter than the TTL.

#### Stress Test: `test_phase0_7_jwt.py`

```python
@pytest.mark.asyncio
async def test_access_token_ttl_is_short(http_client):
    """Access token must expire within 2 hours."""
    login_resp = await http_client.post("/api/auth/login", json={"email": "...", "password": "..."})
    token = login_resp.json()["access_token"]
    payload = jwt.decode(token, options={"verify_signature": False})
    ttl = payload["exp"] - time.time()
    assert ttl < 7200, f"Access token TTL {ttl}s exceeds 2h (should be ~1h)"

@pytest.mark.asyncio
async def test_refresh_rotates_token(http_client):
    """Refresh must issue a new refresh token AND revoke the old one."""
    r1 = await http_client.post("/api/auth/login", json={...})
    old_refresh = r1.json()["refresh_token"]

    r2 = await http_client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert r2.status_code == 200
    new_refresh = r2.json()["refresh_token"]
    assert new_refresh != old_refresh

    # Using old refresh again must fail (rotation = revoke on use)
    r3 = await http_client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert r3.status_code == 401

@pytest.mark.asyncio
async def test_logout_revokes_refresh(http_client):
    """After logout, refresh token is unusable."""
    login = await http_client.post("/api/auth/login", json={...})
    refresh_token = login.json()["refresh_token"]
    await http_client.post("/api/auth/logout", json={"refresh_token": refresh_token})
    r = await http_client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_expired_access_token_rejected(http_client):
    """Calls with expired access token must return 401."""
    # Mint an expired token directly
    expired = jwt.encode({"sub": "u", "exp": time.time() - 100}, SECRET_KEY, algorithm="HS256")
    r = await http_client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
```

---

## Phase 1: Quick Wins — Latency + Resilience (~550-950ms saved)

### 1A. HTTP Connection Pooling in BackendClient
**File**: `voice_pipeline/scripts/main.py:80-112`

**Current** (line 96-102): New `httpx.AsyncClient` per call (50-150ms TCP+TLS overhead).

**Change**: Persistent `self._client` in `__init__`, reuse across all calls. Add `async def close()` for cleanup. Also fixes resource leak (Gap #21 from audit).

```python
# __init__:
self._client = httpx.AsyncClient(
    base_url=self.base_url, headers=self.headers, timeout=30.0,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)

# post():
async def post(self, path, payload, timeout=8):
    resp = await self._client.post(path, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

# close():
async def close(self):
    await self._client.aclose()
```

**Savings**: ~50-150ms per tool call

### 1B. Tune VAD Silence Duration
**File**: `voice_pipeline/configs/default.yaml:26-28`

**Change**:
```yaml
vad:
  provider: "silero"
  kwargs:
    min_silence_duration_ms: 300
    min_speech_duration_ms: 100
```

**Savings**: ~200ms per turn

### 1C. Reduce Tool Call Timeouts + Graceful Fallback
**File**: `voice_pipeline/scripts/main.py:377, 443, 453, 459`

**Change**: RAG `timeout=8`, website `timeout=10`, PTO/HR `timeout=10`. Combined with Phase 0D retry wrapper, tools now retry once then return a graceful message.

**Savings**: Prevents catastrophic tail latency

### 1D. Reduce max_tokens for Voice
**File**: `backend/schemas/rag.py`, `backend/api/rag.py:54`

**Change**: Add optional `max_tokens: int = 1024` to `RAGQueryRequest`. Voice agent sends 256. Backend passes as `streaming_overrides`.

**Savings**: ~100-300ms

### 1E. Use Groq as Primary Backend for Voice
**File**: `backend/schemas/rag.py`, `backend/api/rag.py`, `chat_pipeline/rag/generator.py:451`

**Change**: Add optional `generation_backend` to `RAGQueryRequest`. Voice sends `"groq"`. Mercury stays default for chat.

**Savings**: ~200-500ms

#### Stress Test: `test_phase1_quick_wins.py`

```python
@pytest.mark.asyncio
async def test_http_pooling_vs_no_pooling(backend_url, auth_headers):
    """Pooled client should be measurably faster than per-request clients."""
    ITERATIONS = 30
    payload = {"query": "What is the PTO policy?", "top_k": 3}

    no_pool = LatencyReport("No Pooling", target_p50=1.5, target_p95=3.0)
    for _ in range(ITERATIONS):
        start = time.time()
        async with httpx.AsyncClient(base_url=backend_url, headers=auth_headers, timeout=15) as c:
            await c.post("/api/rag/query", json=payload)
        no_pool.record(time.time() - start)

    pooled = LatencyReport("Pooled", target_p50=1.0, target_p95=2.5)
    async with httpx.AsyncClient(base_url=backend_url, headers=auth_headers, timeout=15,
                                  limits=httpx.Limits(max_connections=10)) as client:
        for _ in range(ITERATIONS):
            start = time.time()
            await client.post("/api/rag/query", json=payload)
            pooled.record(time.time() - start)

    no_pool.report(); pooled.report()
    improvement = statistics.mean(no_pool.times) - statistics.mean(pooled.times)
    assert improvement > 0.02, f"Pooling should save at least 20ms, saved {improvement*1000:.0f}ms"

def test_vad_config_loaded():
    """VAD configured with aggressive thresholds."""
    from voice_pipeline.utils.config import load_config
    config = load_config()
    vad_kwargs = config.livekit.vad.kwargs or {}
    assert vad_kwargs.get("min_silence_duration_ms", 500) <= 300

@pytest.mark.asyncio
async def test_groq_vs_mercury_generation(http_client):
    """Groq should be significantly faster than Mercury."""
    ITERATIONS = 20
    query = "What holidays does the company observe?"
    mercury = LatencyReport("Mercury", target_p50=2.0, target_p95=3.0)
    groq = LatencyReport("Groq", target_p50=1.0, target_p95=2.0)

    for _ in range(ITERATIONS):
        start = time.time()
        r = await http_client.post("/api/rag/query", json={"query": query, "top_k": 3, "generation_backend": "mercury"})
        mercury.record(r.json().get("generation_duration_seconds", time.time() - start))

        start = time.time()
        r = await http_client.post("/api/rag/query", json={"query": query, "top_k": 3, "generation_backend": "groq"})
        groq.record(r.json().get("generation_duration_seconds", time.time() - start))

    mercury.report(); groq.report()
    assert statistics.mean(mercury.times) / max(statistics.mean(groq.times), 0.001) > 1.3

@pytest.mark.asyncio
async def test_phase1_combined(http_client):
    """All Phase 1 changes combined should hit latency targets."""
    ITERATIONS = 30
    report = LatencyReport("Phase 1 Combined", target_p50=1.0, target_p95=1.8)
    for _ in range(ITERATIONS):
        start = time.time()
        r = await http_client.post("/api/rag/query", json={
            "query": "How many sick days do I get?", "top_k": 3,
            "max_tokens": 256, "generation_backend": "groq",
        })
        r.raise_for_status()
        report.record(time.time() - start)
    report.assert_targets()
```

---

## Phase 2: Streaming RAG Endpoint (~300-500ms saved)

### 2A. New SSE Streaming Endpoint
**File**: `backend/api/rag.py` — add `POST /api/rag/query/stream`

Returns: `event: sources` (after retrieval), `event: token` (each generation token), `event: done` (timings). Existing `/api/rag/query` untouched.

### 2B. Voice Agent Consumes SSE
**File**: `voice_pipeline/scripts/main.py:96-102` — add `stream_post()` to BackendClient
**File**: `voice_pipeline/scripts/main.py:362-435` — `query_info` uses streaming

### 2C. [Resilience] Handle Stream Interruption Gracefully
**File**: `voice_pipeline/scripts/main.py` (new), `chat_pipeline/rag/pipeline.py:324-340`

**Current gap**: If streaming fails mid-stream, user gets incomplete answer. Partial results may be cached.

**Change**:
- Add timeout to stream consumption (10s max)
- On stream interruption, return partial answer with a "I lost my train of thought, let me try again" fallback
- Never cache partial/failed generation results

### 2D. Extend Streaming to PTO and HR Agents (~1 day)

**Why**: Phase 2A-C streams only `/api/rag/query`. PTO/HR agent responses still return as a single JSON blob after all LangGraph nodes complete, which is a 2-3s silent wait for the user. LangGraph supports `graph.astream_events()` — each node emits its intermediate status as an event.

**Files**:
- `backend/api/pto_agent.py` — add `POST /api/pto/chat/stream` (SSE)
- `backend/api/hr_ticket_agent.py` — add `POST /api/hr_ticket/chat/stream` (SSE)
- `backend/agents/pto/agent.py` — expose `astream_events()` wrapper
- `backend/agents/hr_ticket/agent.py` — same
- `voice_pipeline/scripts/main.py` — PTO/HR tool calls consume stream
- `frontend/src/services/api.js` — consume SSE for chat UI typing indicators

**Change**: Each LangGraph node emits a `status` event. Example PTO flow produces:
```
event: status  data: {"stage": "parse_intent"}
event: status  data: {"stage": "validate_dates"}
event: status  data: {"stage": "check_balance", "remaining_days": 12}
event: status  data: {"stage": "check_conflicts"}
event: status  data: {"stage": "create_request", "request_id": "..."}
event: done    data: {"response": "Your PTO request for next Thursday and Friday has been submitted. You have 10 days remaining."}
```

**Voice agent uses the intermediate stages** to emit short spoken interjections ("Let me check your balance…") via a low-priority TTS channel, so the user hears continuous progress instead of 2s of silence. Frontend shows them as typing indicators.

**Important**: this is not latency improvement — it's perceived-latency improvement. P95 wall-clock stays the same; user-perceived responsiveness improves materially.

#### Stress Test additions: `test_phase2_streaming.py`

```python
@pytest.mark.asyncio
async def test_pto_stream_emits_node_status(http_client):
    """PTO streaming endpoint must emit status events for each LangGraph node."""
    stages_seen = []
    async with http_client.stream("POST", "/api/pto/chat/stream",
                                  json={"message": "I need 3 days off next week"}) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: status"):
                # parse next data: line
                pass
            if line.startswith("data:") and stages_seen and not stages_seen[-1].get("stage"):
                data = json.loads(line[5:])
                stages_seen.append(data)

    # Expect at least: parse_intent, validate_dates, check_balance
    stage_names = [s.get("stage") for s in stages_seen]
    assert "parse_intent" in stage_names
    assert "check_balance" in stage_names

@pytest.mark.asyncio
async def test_pto_stream_first_status_under_500ms(http_client):
    """First status event should arrive quickly (before full response)."""
    start = time.time()
    first_status_time = None
    async with http_client.stream("POST", "/api/pto/chat/stream",
                                  json={"message": "check my PTO balance"}) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: status"):
                first_status_time = time.time() - start
                break
    assert first_status_time is not None and first_status_time < 0.5
```

#### Stress Test: `test_phase2_streaming.py`

```python
@pytest.mark.asyncio
async def test_streaming_ttft(http_client):
    """Streaming should deliver first token faster than batch."""
    ITERATIONS = 20
    payload = {"query": "What is the bereavement leave policy?", "top_k": 3,
               "max_tokens": 256, "generation_backend": "groq"}

    batch = LatencyReport("Batch /query", target_p50=1.5, target_p95=2.5)
    stream = LatencyReport("Stream TTFT", target_p50=0.6, target_p95=1.2)

    for _ in range(ITERATIONS):
        start = time.time()
        r = await http_client.post("/api/rag/query", json=payload)
        batch.record(time.time() - start)

        start = time.time()
        async with http_client.stream("POST", "/api/rag/query/stream", json=payload) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event: token"):
                    stream.record(time.time() - start)
                    break

    batch.report(); stream.report()
    assert statistics.mean(batch.times) - statistics.mean(stream.times) > 0.2

@pytest.mark.asyncio
async def test_stream_interruption_handled(http_client):
    """Interrupted stream should not crash or cache partial results."""
    payload = {"query": "What benefits are offered?", "top_k": 3, "max_tokens": 256}

    # Read only first 2 tokens then close connection
    async with http_client.stream("POST", "/api/rag/query/stream", json=payload) as resp:
        count = 0
        async for line in resp.aiter_lines():
            if line.startswith("event: token"):
                count += 1
                if count >= 2:
                    break  # Simulate client disconnect

    # Next full query should NOT return a cached partial answer
    r = await http_client.post("/api/rag/query", json=payload)
    answer = r.json()["answer"]
    assert len(answer) > 50, "Answer seems truncated — partial result may have been cached"

@pytest.mark.asyncio
async def test_streaming_concurrent_sessions(http_client):
    """10 concurrent streaming sessions should all complete."""
    CONCURRENT = 10
    payload = {"query": "What benefits are offered?", "top_k": 3, "max_tokens": 256}

    async def stream_session():
        start = time.time()
        tokens = 0
        async with http_client.stream("POST", "/api/rag/query/stream", json=payload) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"): tokens += 1
                if line.startswith("event: done"): break
        return time.time() - start, tokens

    results = await asyncio.gather(*[stream_session() for _ in range(CONCURRENT)])
    times = [r[0] for r in results]
    assert max(times) < 5.0, f"Worst concurrent stream: {max(times):.1f}s"
```

---

## Phase 3: Infrastructure Resilience + Modal Cold Start

### 3A. Modal `keep_warm=1`
**File**: `voice_pipeline/modal_deploy.py:62-68, 123-130`

Add `keep_warm=1` to both `voice_worker_for_room` and `web_api`. Cost: ~$1.50/day.

### 3B. [Resilience] Worker Crash Recovery
**File**: `voice_pipeline/modal_deploy.py:104-116`

**Current gap**: `subprocess.run()` with no restart logic. Worker crash = frozen session.

**Change**: Wrap subprocess in retry loop (max 2 restarts). Add heartbeat timeout: if worker doesn't produce output for 60s, kill and restart.

### 3C. [Resilience] Post-Deployment Health Verification
**File**: `.github/workflows/deploy-backend.yml`

**Current gap**: Deploy to Cloud Run with no post-deploy verification.

**Change**: After `gcloud run deploy`, add a step that polls `/health/ready` for up to 60s. If it never returns 200, trigger rollback to previous revision.

```yaml
- name: Verify deployment health
  run: |
    for i in $(seq 1 12); do
      STATUS=$(curl -s -o /dev/null -w "%{http_code}" $SERVICE_URL/health/ready)
      if [ "$STATUS" = "200" ]; then echo "Healthy"; exit 0; fi
      sleep 5
    done
    echo "Health check failed — rolling back"
    gcloud run services update-traffic $SERVICE --to-revisions=LATEST=0
    exit 1
```

### 3D. [Resilience] Graceful Shutdown Handler
**File**: `backend/main.py`

**Current gap**: No SIGTERM handler. In-flight requests killed during deploy.

**Change**: Add shutdown handler in lifespan that drains active requests:

```python
@asynccontextmanager
async def lifespan(app):
    # startup...
    yield
    # shutdown: give in-flight requests 10s to complete
    logger.info("Shutting down — draining connections...")
    await asyncio.sleep(10)
```

#### Stress Test: `test_phase3_infra.py`

```python
import subprocess, time, json, os

MODAL_SESSION_URL = os.getenv("MODAL_SESSION_URL")

def test_cold_start_with_keep_warm():
    """With keep_warm=1, first request should be fast."""
    assert MODAL_SESSION_URL, "Set MODAL_SESSION_URL"
    times = []
    for i in range(5):
        start = time.time()
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{time_total}",
            "-X", "POST", MODAL_SESSION_URL, "-H", "Content-Type: application/json",
            "-d", json.dumps({"user_token": os.getenv("STRESS_TEST_JWT", "test")})],
            capture_output=True, text=True, timeout=30)
        times.append(float(r.stdout.strip()))
        time.sleep(1)

    assert times[0] < 5.0, f"Cold start {times[0]:.1f}s (keep_warm may not be active)"
    assert statistics.mean(times[1:]) < 2.0

@pytest.mark.asyncio
async def test_backend_health_ready(http_client):
    """Backend /health/ready should return 200 with all models loaded."""
    resp = await http_client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("embedding_model") == "loaded", "Embedding model not loaded"
    assert data.get("chromadb") == "ready", "ChromaDB not ready"

def test_deploy_rollback_on_unhealthy():
    """Deployment workflow should include health verification step."""
    import yaml
    with open(".github/workflows/deploy-backend.yml") as f:
        workflow = yaml.safe_load(f)
    steps = [s.get("name", "") for job in workflow.get("jobs", {}).values()
             for s in job.get("steps", [])]
    assert any("health" in s.lower() or "verify" in s.lower() for s in steps), \
        "Deploy workflow missing health verification step"
```

---

## Phase 4: Caching + Data Pipeline Resilience

### 4A. Cache `_get_all_companies()`
**File**: `chat_pipeline/rag/data_loader.py:228-242`

Add `@lru_cache(maxsize=1)`.

### 4B. Cache `resolve_company_filter()`
**File**: `chat_pipeline/rag/data_loader.py:244-285`

Module-level dict cache keyed by normalized company name.

### 4C. [Resilience] GCS Sync Retry
**File**: `chat_pipeline/rag/data_loader.py:160-184`, `backend/jobs/tasks.py:93-102`

**Current gap**: Single-attempt `gsutil` calls. Network blip = sync failure.

**Change**: Retry 3 times with 5s/10s/20s backoff. Verify downloaded file integrity (size check against source).

### 4D. [Resilience] Data Pipeline Checkpointing
**File**: `data_pipeline/scripts/pipeline_runner.py:34-69`

**Current gap**: No checkpointing, no resume. Stage 4 failure = re-run everything.

**Change**: Each stage writes a success marker file (`.stage_N_complete`). On restart, skip completed stages. Add `--resume` flag.

### 4E. [Resilience] PDF Download Retry + Partial File Cleanup
**File**: `data_pipeline/scripts/download_data.py:84-96`

**Current gap**: No retry, partial files left on disk and pass the "already exists" check.

**Change**: Download to `.tmp`, verify Content-Length, rename to final path. Retry 3x with backoff on failure.

### 4F. [Resilience] ChromaDB Atomic Batch Writes
**File**: `data_pipeline/scripts/store_in_chromadb.py:103-118`

**Current gap**: Single `collection.add()` with all chunks. Failure = unknown state. Re-run = duplicates.

**Change**: Add in batches of 500. Deduplicate by chunk hash before insert. Log which batches succeeded.

#### Stress Test: `test_phase4_caching.py`

```python
@pytest.mark.asyncio
async def test_company_filter_cached(http_client):
    """Repeated queries should benefit from company metadata cache."""
    ITERATIONS = 30
    payload = {"query": "What is the vacation policy?", "top_k": 3,
               "max_tokens": 256, "generation_backend": "groq"}

    await http_client.post("/api/rag/query", json=payload)  # warm cache

    report = LatencyReport("Cached company filter", target_p50=0.8, target_p95=1.5)
    for _ in range(ITERATIONS):
        start = time.time()
        r = await http_client.post("/api/rag/query", json=payload)
        r.raise_for_status()
        report.record(time.time() - start)
    report.assert_targets()

@pytest.mark.asyncio
async def test_pipeline_cache_hit(http_client):
    """Identical query should be a cache hit (<200ms)."""
    payload = {"query": "What is the dress code?", "top_k": 3, "max_tokens": 256}
    await http_client.post("/api/rag/query", json=payload)

    start = time.time()
    r = await http_client.post("/api/rag/query", json=payload)
    hit_time = time.time() - start
    assert r.json().get("cache_hit") is True
    assert hit_time < 0.2

@pytest.mark.asyncio
async def test_cache_no_corruption_under_load(http_client):
    """50 concurrent requests should not corrupt the pipeline cache."""
    queries = [f"test query {i % 5}" for i in range(50)]

    async def query(q):
        r = await http_client.post("/api/rag/query", json={"query": q, "top_k": 3})
        return r.status_code

    results = await asyncio.gather(*[query(q) for q in queries])
    assert all(r == 200 for r in results), f"Some requests failed: {[r for r in results if r != 200]}"
```

---

## Phase 5: Voice Fast Path + Voice Pipeline Resilience

### 5A. Voice Prompt Template
**File**: `chat_pipeline/rag/prompt_templates.py`

Short conversational prompt, 2-3 sentences, no markdown. Voice agent passes `template_key: "voice_prompt"`.

### 5B. Pre-fetch RAG on Partial STT
**File**: `backend/api/rag.py` — add `/api/rag/prefetch` (retrieval only)
**File**: `voice_pipeline/scripts/main.py` — background prefetch on partial STT

### 5C. [Resilience] WebSocket Reconnect Logic
**File**: `voice_pipeline/scripts/main.py:663-731`

**Current gap**: Connection drop = frozen session, no reconnect, no user notification.

**Change**: On session error, attempt reconnect with exponential backoff (1s, 2s, 4s, max 3 attempts). On final failure, send "session ended unexpectedly" via TTS before exiting.

### 5D. [Resilience] Metrics Fallback to File
**File**: `voice_pipeline/utils/metrics.py`, `voice_pipeline/modal_deploy.py:52-55`

**Current gap**: W&B failure = metrics silently dropped. No fallback.

**Change**: If W&B is unavailable, write metrics to `metrics.jsonl` in the session log directory. Log W&B unavailability as ERROR, not silent pass.

### 5E. [Resilience] Intent Detection Fallback Fix
**File**: `backend/api/unified_agent.py:151-161`

**Current gap**: When LLM intent detection fails, keyword heuristic routes non-question statements to HR Ticket. "I need vacation" → HR instead of PTO.

**Change**: Add PTO-specific keywords (`vacation`, `time off`, `leave`, `pto`, `sick day`) to the keyword matching before falling back to HR.

#### Stress Test: `test_phase5_voice_path.py`

```python
@pytest.mark.asyncio
async def test_voice_prompt_shorter(http_client):
    """Voice prompt should produce shorter answers than default."""
    query = "What are the company holidays?"
    default_lengths, voice_lengths = [], []

    for _ in range(15):
        r = await http_client.post("/api/rag/query", json={"query": query, "top_k": 3, "max_tokens": 1024})
        default_lengths.append(len(r.json()["answer"]))
        r = await http_client.post("/api/rag/query", json={
            "query": query, "top_k": 3, "max_tokens": 256, "template_key": "voice_prompt"})
        voice_lengths.append(len(r.json()["answer"]))

    assert statistics.mean(voice_lengths) < statistics.mean(default_lengths)

@pytest.mark.asyncio
async def test_prefetch_speed(http_client):
    """Prefetch (retrieval only) should be under 500ms."""
    report = LatencyReport("Prefetch", target_p50=0.3, target_p95=0.5)
    for _ in range(20):
        start = time.time()
        r = await http_client.post("/api/rag/prefetch", json={"query": "PTO policy?", "top_k": 3})
        r.raise_for_status()
        report.record(time.time() - start)
    report.assert_targets()

def test_intent_detection_pto_keywords():
    """PTO-related statements should route to PTO agent, not HR."""
    from backend.api.unified_agent import detect_intent
    pto_messages = ["I need vacation", "time off next week", "request pto", "sick day tomorrow"]
    for msg in pto_messages:
        result = detect_intent(msg)
        assert result["agent"] == "pto", f"'{msg}' routed to {result['agent']} instead of pto"
```

---

## Phase 5.5: Durable LangGraph Checkpointing (2 days)

**Why**: Today's LangGraph state is non-durable — it lives only for the duration of one HTTP request. Multi-turn conversations can't resume across sessions, workflows can't wait for external events (admin approval), and there's no replay path for debugging. LangGraph has first-class support for this via `PostgresSaver`; this phase wires it up.

**Addresses**: `system_design.md` §7.3.1 Problems 1-3 and Chokepoint #6 (reframed).

### 5.5A. Install and Configure Checkpointer
**File**: `backend/agents/utils/checkpointer.py` (new)

```python
from langgraph.checkpoint.postgres import PostgresSaver
from db.connection import get_engine

_checkpointer = None

def get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        engine = get_engine()
        _checkpointer = PostgresSaver(engine=engine)
        _checkpointer.setup()  # creates checkpoint tables if absent
    return _checkpointer
```

LangGraph manages the schema of `checkpoints` and `checkpoint_writes` tables internally. No manual DDL.

### 5.5B. Wire Checkpointer Into PTO and HR Agents
**File**: `backend/agents/pto/agent.py` and `backend/agents/hr_ticket/agent.py`

```python
from agents.utils.checkpointer import get_checkpointer

# In __init__ or wherever graph is compiled:
self.graph = workflow.compile(checkpointer=get_checkpointer())

# In execute():
async def execute(self, user_email, company, message, conversation_id):
    thread_id = f"pto-{conversation_id}"  # or similar stable key
    config = {"configurable": {"thread_id": thread_id}}
    return await self.graph.ainvoke(
        {"user_email": user_email, "company": company, "user_message": message},
        config=config,
    )
```

Stable `thread_id` is the crucial piece — use `conversation_id` so multiple turns within the same chat thread share state.

### 5.5C. Multi-Turn Resume Behavior
**File**: `backend/api/unified_agent.py`

When the router dispatches to the PTO agent for a conversation that has an existing checkpoint, the agent resumes from that checkpoint instead of starting over. The only code change needed is passing `conversation_id` through — LangGraph handles resumption automatically via the checkpointer.

### 5.5D. Admin Approval as a Suspend/Resume Workflow (optional, stub only)
**File**: `backend/agents/pto/agent.py`

Add a new `wait_for_review` node between `create_request` and `generate_response`. When reached, the node returns a partial state and the graph pauses. A new endpoint `POST /api/admin/pto/{id}/approve` triggers `graph.ainvoke(resume_input, config={thread_id: original})` to continue the workflow.

**Stub for now** — full implementation deferred to a follow-on feature. Phase 5.5 lays the foundation (checkpointer wired, state durable) but doesn't flip on the suspend/resume UX until we have admin-side UI ready.

### 5.5E. Tenant-Scoped Checkpoint Cleanup
**File**: `backend/jobs/tasks.py`

Checkpoints accumulate per conversation. Add a daily task: delete checkpoints whose `thread_id` corresponds to conversations older than 30 days (`Conversation.updated_at < NOW() - INTERVAL '30 days'`).

### 5.5F. Tenant Context and Checkpoints
**File**: `backend/agents/utils/checkpointer.py`

The `thread_id` format `{agent}-{conversation_id}` implicitly scopes by tenant because `Conversation.company` is already set. But add an explicit guard: before loading a checkpoint, verify the conversation belongs to the current tenant context (via the Phase 0.6 ContextVar). Prevents a bug where a malicious or broken `thread_id` reads another tenant's state.

#### Stress Test: `test_phase5_5_checkpointing.py`

```python
@pytest.mark.asyncio
async def test_multi_turn_resume(http_client):
    """PTO conversation state persists across turns."""
    # Turn 1: initiate request but don't specify dates
    r1 = await http_client.post("/api/chat/message",
        json={"message": "I'd like to request some PTO"})
    conversation_id = r1.json()["conversation_id"]

    # Turn 2: provide dates — agent should REMEMBER we're mid-PTO-request
    r2 = await http_client.post("/api/chat/message",
        json={"message": "next Thursday and Friday", "conversation_id": conversation_id})

    # Agent should have understood this as continuation, not new intent
    assert r2.json()["agent_used"] == "pto"
    assert "request" in r2.json()["response"].lower()

@pytest.mark.asyncio
async def test_checkpoint_isolated_per_thread(http_client):
    """Two conversations must not share state."""
    r1 = await http_client.post("/api/chat/message",
        json={"message": "I want PTO for next week"})
    r2 = await http_client.post("/api/chat/message",
        json={"message": "and next Friday too"})  # NEW conversation, no conversation_id

    # r2 must be a fresh intent, not continuation of r1
    assert r1.json()["conversation_id"] != r2.json()["conversation_id"]

def test_checkpoint_tenant_isolation():
    """Checkpoint reads fail if conversation belongs to another tenant."""
    # Create checkpoint as Tenant A
    # Attempt to resume with thread_id referencing Tenant A's conversation from Tenant B context
    # Assert RuntimeError or empty state returned
    pass

@pytest.mark.asyncio
async def test_checkpoint_cleanup_old_conversations(db_session):
    """Cleanup task removes checkpoints for conversations > 30 days old."""
    # Create old conversation + checkpoint
    # Run cleanup
    # Assert checkpoint gone, active conversation's checkpoint preserved
    pass
```

---

## Phase 6: Observability & Circuit Breakers (Medium Priority)

### 6A. Add Request Correlation IDs
**File**: `backend/monitoring/middleware.py`

Generate `X-Request-ID` on each request, propagate through all log entries. Voice agent passes session ID as correlation header.

### 6B. Circuit Breaker for LLM Providers
**File**: `chat_pipeline/rag/generator.py`, `backend/agents/utils/llm_client.py`

When Mercury is down, every request still tries it (3 retries x 2-8s backoff = 14s wasted). Add a circuit breaker: after 3 consecutive failures, skip Mercury for 60s and go straight to Groq.

```python
class CircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=60):
        self.failures = 0
        self.last_failure = 0
        self.threshold = failure_threshold
        self.timeout = recovery_timeout

    def is_open(self):
        if self.failures >= self.threshold:
            if time.time() - self.last_failure < self.timeout:
                return True  # Skip this provider
            self.failures = 0  # Try again (half-open)
        return False

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()

    def record_success(self):
        self.failures = 0
```

### 6C. [Resilience] NullPool → Connection Pooling
**File**: `backend/db/connection.py:35-40`

Replace `NullPool` with `QueuePool(pool_size=5, max_overflow=10)` for Cloud Run. Prevents connection exhaustion under traffic spikes.

#### Stress Test: `test_phase6_observability.py`

```python
@pytest.mark.asyncio
async def test_request_correlation_id(http_client):
    """Each response should include X-Request-ID header."""
    r = await http_client.post("/api/rag/query", json={"query": "test", "top_k": 3})
    assert "x-request-id" in r.headers, "Missing correlation ID header"

@pytest.mark.asyncio
async def test_circuit_breaker_skips_failed_provider(http_client):
    """After provider failure, next request should skip it (faster response)."""
    # Force Mercury failure, then verify next request is faster (skips Mercury)
    # This requires a way to inject provider failures — may need monkeypatching
    pass

@pytest.mark.asyncio
async def test_connection_pool_under_load(http_client):
    """50 concurrent DB-hitting requests should not exhaust connections."""
    async def health_check():
        r = await http_client.get("/health")
        return r.status_code

    results = await asyncio.gather(*[health_check() for _ in range(50)])
    assert all(r == 200 for r in results), f"Connection pool exhaustion: {sum(1 for r in results if r != 200)} failures"
```

---

## Phase 6.5: Resilience Policy Matrix (1 day)

**Why**: Phases 0 + 6 add retries, timeouts, circuit breakers in various places. Without a unifying policy, new integrations will drift back to ad-hoc handling. This phase formalizes *which pattern applies to which call type* and audits the codebase for compliance.

### 6.5A. Document the Matrix
**File**: `docs/resilience_policy.md` (new) and `system_design.md` §7.6

| Call type | Timeout | Retry | Exp. backoff | Circuit breaker | Idempotency key |
|---|---|---|---|---|---|
| **External LLM API** (Mercury, Groq, OpenAI) | 8s | 3x | ✓ | ✓ per provider | N/A (read-only) |
| **External search** (Brave, Deepgram) | 5s | 2x | ✓ | ✓ | N/A |
| **Internal DB queries** | 2s | 1x | ✗ fast-fail | ✗ | N/A |
| **Voice → Backend tool calls** | 8s | 2x | 1s linear | ✗ (own infra) | ✓ REQUIRED |
| **User-facing HTTP chains** | 10s | ✗ let user retry | — | — | ✓ for mutations |
| **GCS sync (data pipeline)** | 300s | 3x | ✓ | ✗ | ✗ (re-syncable) |
| **LiveKit provider chains** | 5s | ✗ (LiveKit handles) | — | ✓ | — |

### 6.5B. Resilience Helper Module
**File**: `backend/utils/resilience.py` (new)

Provides typed decorators matching the matrix:

```python
@resilient(policy="external_llm")
async def call_mercury(...): ...

@resilient(policy="internal_db")
def load_user(...): ...
```

Each policy applies the correct timeout + retry + backoff + circuit breaker. Changing a policy updates behavior everywhere.

### 6.5C. Audit and Migrate Call Sites
Grep for all external call sites (`httpx.post`, `requests.get`, `aiohttp`, etc.). Migrate each to use the appropriate `@resilient` decorator. Commit the audit as a table in the PR description.

#### Stress Test: `test_phase6_5_resilience_matrix.py`

```python
@pytest.mark.parametrize("call_type,expected_timeout_s,expected_retries", [
    ("external_llm", 8, 3),
    ("external_search", 5, 2),
    ("internal_db", 2, 1),
    ("voice_tool", 8, 2),
])
def test_policy_values_match_matrix(call_type, expected_timeout_s, expected_retries):
    from backend.utils.resilience import get_policy
    policy = get_policy(call_type)
    assert policy.timeout_s == expected_timeout_s
    assert policy.max_retries == expected_retries

def test_no_unprotected_external_calls():
    """No direct httpx/requests calls outside the resilience module."""
    # Run grep against backend/, fail if external call not wrapped in @resilient
    import subprocess
    result = subprocess.run(["grep", "-rn", r"httpx\.\(post\|get\|put\)", "backend/",
                             "--include=*.py"], capture_output=True, text=True)
    unwrapped = [l for l in result.stdout.splitlines()
                 if "resilience.py" not in l and "test_" not in l]
    assert not unwrapped, f"Unwrapped external calls: {unwrapped}"
```

---

## Phase 7: Prometheus + Grafana Observability (4 days) — RUNS BEFORE PHASE 1

**Why runs before latency phases**: You cannot optimize what you cannot measure. Every Phase 1-6 optimization needs a before/after graph, which requires instrumentation + dashboards to exist *first*. This phase establishes the baseline.

**Stack**: Grafana Cloud free tier (10K metrics, 50GB logs, 14-day retention = $0/mo). Instrument code with `prometheus_client`. Grafana Agent scrapes the `/metrics` endpoint and ships to Grafana Cloud. Dashboards are provisioned as JSON in `grafana/dashboards/`.

### 7A. Instrument the Backend
**File**: `backend/observability/metrics.py` (new)

```python
from prometheus_client import Counter, Histogram, Gauge, REGISTRY, generate_latest

# Golden Signals
http_requests_total = Counter(
    "http_requests_total", "HTTP requests",
    ["method", "endpoint", "status", "company"])
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request latency",
    ["method", "endpoint", "company"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10))

# RAG-specific
rag_retrieval_duration_seconds = Histogram(
    "rag_retrieval_duration_seconds", "ChromaDB retrieval latency",
    ["company"], buckets=(0.05, 0.1, 0.2, 0.5, 1, 2))
rag_generation_duration_seconds = Histogram(
    "rag_generation_duration_seconds", "LLM generation latency",
    ["backend", "company"], buckets=(0.1, 0.3, 0.5, 1, 2, 5))
rag_cache_hit_total = Counter(
    "rag_cache_hit_total", "RAG pipeline cache hits", ["company", "hit"])

# LLM provider metrics
llm_provider_latency_seconds = Histogram(
    "llm_provider_latency_seconds", "External LLM API latency",
    ["provider", "outcome"])
llm_provider_failures_total = Counter(
    "llm_provider_failures_total", "LLM provider failures",
    ["provider", "error_class"])
circuit_breaker_state = Gauge(
    "circuit_breaker_state", "Circuit breaker state (0=closed, 1=half-open, 2=open)",
    ["provider"])

# DB pool
db_pool_size = Gauge("db_pool_size", "Current DB connection pool size")
db_pool_checkedout = Gauge("db_pool_checkedout", "Checked-out connections")

# Tenant health
tenant_request_total = Counter(
    "tenant_request_total", "Requests per tenant", ["company", "agent"])
```

**Middleware** (`backend/observability/metrics.py`):
```python
@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    company = request.state.company if hasattr(request.state, "company") else "unknown"
    duration = time.time() - start
    http_requests_total.labels(method=request.method, endpoint=request.url.path,
                               status=response.status_code, company=company).inc()
    http_request_duration_seconds.labels(method=request.method,
                                         endpoint=request.url.path, company=company).observe(duration)
    return response

@app.get("/metrics")
def metrics():
    return Response(generate_latest(REGISTRY), media_type="text/plain")
```

### 7B. Instrument the Voice Pipeline
**File**: `voice_pipeline/observability/metrics.py` (new)

```python
voice_stt_duration_seconds = Histogram("voice_stt_duration_seconds", "STT latency")
voice_llm_ttft_seconds = Histogram("voice_llm_ttft_seconds", "LLM time to first token")
voice_tts_ttfb_seconds = Histogram("voice_tts_ttfb_seconds", "TTS time to first byte")
voice_vad_eou_delay_seconds = Histogram("voice_vad_eou_delay_seconds", "VAD end-of-utterance delay")
voice_e2e_latency_seconds = Histogram(
    "voice_e2e_latency_seconds", "End-to-end voice turn latency",
    buckets=(0.5, 1, 1.5, 2, 3, 5, 10))
voice_session_active = Gauge("voice_session_active", "Currently active voice sessions")
```

Integrate with existing metric hooks in `voice_pipeline/utils/metrics.py`. Write to both W&B (ML experiments) and Prometheus (ops monitoring).

### 7C. Correlation IDs
**File**: `backend/observability/tracing.py`

Every request gets `X-Request-ID` (generate if absent). Propagate to all downstream calls (LLM providers, ChromaDB logs, voice worker tool calls). Include in every log line via contextvar.

### 7D. Grafana Cloud Setup
1. Sign up at grafana.com (free tier, no credit card)
2. Install Grafana Agent on Cloud Run + Modal (or use GrafanaAgent container sidecar)
3. Configure scrape targets: `backend:8080/metrics`, `modal-web-api:/metrics`, per-worker `/metrics`
4. Set remote_write to Grafana Cloud Prometheus endpoint

### 7E. SLO Definitions
**File**: `grafana/slo.yaml` (new)

```yaml
slos:
  - name: voice_p95_latency
    target: 1.5  # seconds
    indicator: histogram_quantile(0.95, voice_e2e_latency_seconds)
    error_budget_days: 30
    burn_rate_alerts: [1x, 6x, 14.4x]  # standard multi-window multi-burn-rate

  - name: chat_p95_latency
    target: 2.5
    indicator: histogram_quantile(0.95, http_request_duration_seconds{endpoint="/api/chat/message"})

  - name: availability_per_tenant
    target: 0.995  # 99.5%
    indicator: sum(rate(http_requests_total{status!~"5.."}[5m])) by (company) /
               sum(rate(http_requests_total[5m])) by (company)
```

### 7F. Six Dashboards (Provisioned as JSON)

1. **`service_health.json`** — The Four Golden Signals. Traffic, errors, latency (p50/p95/p99), saturation (CPU, memory, DB pool). One panel per signal, filterable by endpoint.

2. **`voice_pipeline.json`** — Per-stage latency distributions (VAD, STT, LLM TTFT, TTS TTFB), end-to-end histogram, active session gauge, provider breakdown (Deepgram vs AssemblyAI).

3. **`rag_pipeline.json`** — Retrieval latency per company, generation latency per backend (Mercury/Groq/OpenAI), cache hit rate, documents returned distribution.

4. **`tenants.json`** — Per-company request rate, p95 latency, error rate. Detects noisy neighbors.

5. **`stress_test.json`** — Locust active users + request rate overlaid with system p95 latency. Shows the moment the system starts degrading under load.

6. **`resilience.json`** — Circuit breaker state per provider (green/yellow/red), retry count rate, fallback chain usage, rate of 429s per provider.

### 7G. Alert Rules
**File**: `grafana/alerts.yaml`

- Voice p95 > 1.5s for 5 min → Slack notification (the SLO breach alert)
- Circuit breaker open for >2min → PagerDuty (or email for student project)
- DB pool exhaustion (checkedout / size > 0.8) → Slack
- Tenant-specific error rate > 10% for 5 min → Slack with `company` label

### 7H. Locust → Prometheus Export
**File**: `stress_tests/locustfile.py` (edit)

Locust already exports Prometheus metrics via `locust-prometheus-exporter`. Add exporter, point Grafana Agent at Locust's metrics endpoint. Stress test runs now appear live on `stress_test.json` dashboard.

#### Stress Test: `test_phase7_observability.py`

```python
@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_prometheus_format(http_client):
    resp = await http_client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
    assert "# HELP" in resp.text  # Prometheus format marker
    assert "# TYPE" in resp.text

@pytest.mark.asyncio
async def test_request_increments_counter(http_client):
    before = get_counter_value(http_client, "http_requests_total",
                              labels={"endpoint": "/api/rag/query", "status": "200"})
    await http_client.post("/api/rag/query", json={"query": "test", "top_k": 3})
    after = get_counter_value(http_client, "http_requests_total",
                              labels={"endpoint": "/api/rag/query", "status": "200"})
    assert after == before + 1

def test_label_cardinality_bounded():
    """Label cardinality must stay bounded. Company label has ~200 possible values max."""
    from prometheus_client import REGISTRY
    for collector in REGISTRY.collect():
        for sample in collector.samples:
            # No labels that could explode (e.g., user_id, request_id)
            assert "user_id" not in sample.labels
            assert "request_id" not in sample.labels

def test_correlation_id_propagated(http_client):
    resp = await http_client.post("/api/chat/message",
        json={"message": "test"},
        headers={"X-Request-ID": "test-correlation-abc123"})
    assert resp.headers.get("X-Request-ID") == "test-correlation-abc123"
    # Also: verify it appears in logs (check log output)

def test_slo_query_returns_valid_range(grafana_api):
    """Voice p95 SLO query must return a numeric value between 0-10s."""
    result = grafana_api.query('histogram_quantile(0.95, voice_e2e_latency_seconds)')
    assert 0 < float(result) < 10
```

---

## E2E Validation: `test_e2e_latency.py`

```python
@pytest.mark.asyncio
async def test_e2e_voice_backend_latency(http_client):
    """Full voice backend path must be under target."""
    queries = ["PTO policy?", "Sick leave?", "Holidays?", "Dress code?", "401k match?",
               "Remote work?", "Expense reports?", "Parental leave?", "Vacation days?", "Bereavement?"]
    report = LatencyReport("E2E Voice Backend", target_p50=0.8, target_p95=1.2)
    for i in range(30):
        start = time.time()
        r = await http_client.post("/api/rag/query", json={
            "query": queries[i % len(queries)], "top_k": 3,
            "max_tokens": 256, "generation_backend": "groq"})
        r.raise_for_status()
        report.record(time.time() - start)
    report.assert_targets()

@pytest.mark.asyncio
async def test_e2e_concurrent_sessions(http_client):
    """10 concurrent voice agents should all respond within limits."""
    async def session(i):
        start = time.time()
        r = await http_client.post("/api/rag/query", json={
            "query": "What benefits are offered?", "top_k": 3,
            "max_tokens": 256, "generation_backend": "groq"})
        return time.time() - start

    times = await asyncio.gather(*[session(i) for i in range(10)])
    assert statistics.mean(times) < 2.0
    assert max(times) < 5.0
```

---

## Locust Load Test: `stress_tests/locustfile.py`

```python
from locust import HttpUser, task, between
import os, random

QUERIES = ["PTO policy?", "Sick days?", "Holidays?", "Dress code?", "401k match?"]

class VoicePipelineUser(HttpUser):
    wait_time = between(1, 3)
    def on_start(self):
        self.headers = {"Authorization": f"Bearer {os.getenv('STRESS_TEST_JWT', '')}",
                        "Content-Type": "application/json"}

    @task(5)
    def voice_rag(self):
        self.client.post("/api/rag/query", json={"query": random.choice(QUERIES),
            "top_k": 3, "max_tokens": 256, "generation_backend": "groq"},
            headers=self.headers, name="RAG (voice)")

    @task(3)
    def chat_rag(self):
        self.client.post("/api/rag/query", json={"query": random.choice(QUERIES), "top_k": 3},
            headers=self.headers, name="RAG (chat)")

    @task(1)
    def health(self):
        self.client.get("/health")
```

---

## Implementation Order (revised — correctness → observability → optimization)

**Why the new order**: Correctness bugs (Phase 0 series) get fixed before anything else. Observability (Phase 7) runs before the latency optimization phases so each optimization has a measurable before/after. Phase 6.5 (resilience matrix) ships before Phase 6 circuit breakers so they use the unified helper.

| Week | Day | Phase | What | Type |
|---|---|-------|------|------|
| 1 | 1-2 | **0A-0H** | Critical resilience fixes | Correctness |
| 1 | 3-4 | **0.5** | Idempotency keys | Correctness |
| 1 | 5 | **0.7** | JWT access + refresh tokens | Security |
| 2 | 6-9 | **0.6** | Multi-tenant hardening (RLS-equivalent app-layer) | Security |
| 2 | 10 | **6.5** | Resilience policy matrix + helper module | Resilience |
| 3 | 11-14 | **7** | Prometheus + Grafana instrumentation + dashboards + SLOs | Observability |
| 3 | 15 | **Baseline** | Capture "before" metrics across all endpoints | Measurement |
| 4 | 16-17 | **1A-1C** | HTTP pooling, VAD, timeouts | Latency |
| 4 | 18-19 | **1D-1E** | max_tokens, Groq switch | Latency |
| 4 | 20 | **3A-3D** | Modal keep_warm, worker recovery, deploy health, shutdown | Infra |
| 5 | 21-22 | **4A-4F** | Caching + data pipeline resilience | Latency + Resilience |
| 5 | 23-25 | **2A-2C** | Streaming RAG endpoint + interruption handling | Latency |
| 6 | 26 | **2D** | Extend streaming to PTO/HR agents (node-level status events) | Perceived latency |
| 6 | 27-28 | **5.5** | Durable LangGraph checkpointing (multi-turn resume, foundation for suspend/resume workflows) | Feature capability |
| 7 | 29-31 | **5A-5E** | Voice prompt, prefetch, reconnect, metrics fallback, intent fix | Latency + Resilience |
| 7 | 32 | **6A-6C** | Correlation IDs, circuit breaker, connection pooling | Resilience |
| 7 | 33 | **E2E** | Full benchmark + Locust sustained load test (live on Grafana) | Validation |

**Total: ~33 working days (~6.5 weeks).** Scope is now: critical resilience + idempotency + JWT refresh + multi-tenant hardening + resilience matrix + observability + streaming extensions + durable checkpointing + latency optimizations.

---

## SLOs (Service Level Objectives)

These replace the old "Latency Targets" — SLOs include an error budget, which tells you *how often* you're allowed to miss the target before alerting pages.

| SLO | Target | Measurement Window | Error Budget |
|---|---|---|---|
| **Voice end-to-end latency** | p95 < 1.5s | 30 days rolling | 0.5% of turns may exceed |
| **Chat RAG latency** | p95 < 2.5s | 30 days rolling | 0.5% |
| **Availability per tenant** | 99.5% | 30 days per tenant | ~3.6 hours/month down |
| **Voice session creation** | p95 < 3s (warm), < 8s (cold) | 30 days | 1% may exceed |
| **RAG retrieval** | p95 < 500ms | 30 days | 1% |
| **LLM generation (Groq)** | p95 < 800ms | 30 days | 1% |
| **Circuit breaker recovery** | <60s from open → half-open test | per incident | no budget (operational) |

**Phase-level latency targets (for stress tests)** — these are checkpoint gates on the way to the SLOs:

| Phase | Metric | P50 | P95 |
|-------|--------|-----|-----|
| 1 combined | Voice RAG (Groq + 256 tokens + pooled) | 1.0s | 1.8s |
| 2 | Streaming TTFT | 0.6s | 1.2s |
| 3 | Modal session (warm) | 2.0s | 5.0s |
| 4 | RAG with cached company filter | 0.8s | 1.5s |
| 5 | Prefetch (retrieval only) | 0.3s | 0.5s |
| **E2E** | **Full voice backend** | **0.8s** | **1.2s** |

---

## Correctness & Security Targets

| Concern | Target | Validation |
|---|---|---|
| SQLite production fallback | 0 occurrences | Phase 0A test |
| Forgotten tenant filters | Auto-applied by event listener | Phase 0.6 test suite |
| Duplicate tool-call writes after retry | 0 | Phase 0.5 idempotency tests |
| Access tokens live > 2 hours | 0 | Phase 0.7 test |
| Revoked refresh tokens accepted | 0 | Phase 0.7 test |
| Cross-tenant HTTP access attempts succeed | 0 | Phase 0.6 negative tests |
| ChromaDB chunks missing company label | 0 at deploy | Phase 0.6E validator |
| Unwrapped external calls (no resilience decorator) | 0 | Phase 6.5 grep test |

---

## Resilience Targets (operational)

| Gap | Metric | Target |
|-----|--------|--------|
| Tool call retry | Success rate on transient errors | >95% |
| Cache corruption | Errors under 50 concurrent requests | 0 |
| Worker crash recovery | Session recovery time | <10s |
| Circuit breaker | Time to skip downed provider | <1s after 3 failures |
| Connection pool | Failures under 50 concurrent DB queries | 0 |
| Graceful shutdown | In-flight request completion rate during deploy | >99% |

---

## Key Files Modified

| File | Changes | Phase |
|------|---------|-------|
| **Backend — Core** | | |
| `backend/main.py` | Block startup on warmup fail, tenant middleware, graceful shutdown, Prometheus middleware | 0B, 0.6B, 3D, 7A |
| `backend/db/connection.py` | Block SQLite in prod, QueuePool | 0A, 6C |
| `backend/db/tenant_context.py` *(new)* | ContextVar + SQLAlchemy event listener + bypass helper | 0.6A |
| `backend/db/models.py` | Add `IdempotencyRecord`, `RefreshToken` | 0.5A, 0.7B |
| `backend/api/health.py` | Fix connection leak | 0C |
| `backend/api/auth.py` | Short-TTL access + refresh endpoint + logout + rotation | 0.7A-E |
| `backend/api/rag.py` | `/query/stream`, `/prefetch`, voice params, idempotency | 1D/E, 2A, 5B, 0.5C |
| `backend/api/unified_agent.py` | Fix intent fallback keywords, idempotency, pass `conversation_id` as `thread_id` to agents | 5E, 0.5C, 5.5C |
| `backend/api/pto_agent.py` | Add `/api/pto/chat/stream` SSE endpoint | 2D |
| `backend/api/hr_ticket_agent.py` | Add `/api/hr_ticket/chat/stream` SSE endpoint | 2D |
| `backend/api/idempotency.py` *(new)* | Idempotency middleware | 0.5B |
| `backend/api/admin.py` | Wrap queries in `bypass_tenant_filter(reason, actor)` | 0.6C |
| `backend/schemas/rag.py` | Add max_tokens, generation_backend, template_key | 1D, 1E, 5A |
| **Backend — Agents & Utils** | | |
| `backend/agents/utils/llm_client.py` | Thread-safe singleton, resilience decorator | 0F, 6.5B |
| `backend/utils/resilience.py` *(new)* | Policy matrix implementation (timeouts + retries + circuit breakers) | 6.5B |
| `backend/agents/utils/checkpointer.py` *(new)* | LangGraph `PostgresSaver` singleton factory | 5.5A |
| `backend/agents/pto/agent.py` | Compile graph with checkpointer, accept `conversation_id`→`thread_id`, expose `astream_events()` | 5.5B, 2D |
| `backend/agents/hr_ticket/agent.py` | Same as PTO agent (checkpointer + streaming) | 5.5B, 2D |
| **Backend — Observability** | | |
| `backend/observability/metrics.py` *(new)* | Prometheus counters, histograms, gauges + middleware | 7A |
| `backend/observability/tracing.py` *(new)* | Correlation ID propagation | 6A, 7C |
| `backend/monitoring/middleware.py` | Integrate correlation ID | 6A |
| `backend/jobs/tasks.py` | Idempotency record cleanup cron, checkpoint cleanup for >30-day conversations | 0.5E, 5.5E |
| **Chat Pipeline (RAG)** | | |
| `chat_pipeline/rag/pipeline.py` | Thread-safe cache, no partial caching, Prometheus hooks | 0E, 2C, 7A |
| `chat_pipeline/rag/generator.py` | 429 handling, circuit breaker via resilience decorator, Groq override | 0H, 1E, 6.5B, 6B |
| `chat_pipeline/rag/data_loader.py` | GCS retry (resilient decorator), cache companies | 4A, 4B, 4C |
| `chat_pipeline/rag/tenant_scoped_retriever.py` *(new)* | ChromaDB wrapper enforcing company arg | 0.6D |
| `chat_pipeline/rag/retriever.py` | Use `TenantScopedRetriever` | 0.6D |
| `chat_pipeline/rag/prompt_templates.py` | Voice prompt | 5A |
| **Voice Pipeline** | | |
| `voice_pipeline/scripts/main.py` | HTTP pooling, streaming consumer, tool retries + idempotency keys, auth validation, reconnect, Prometheus | 1A, 2B, 0D, 0.5D, 0G, 5C, 7B |
| `voice_pipeline/configs/default.yaml` | VAD kwargs | 1B |
| `voice_pipeline/modal_deploy.py` | Worker restart, keep_warm, expose `/metrics` | 3A, 3B, 7A |
| `voice_pipeline/utils/metrics.py` | File fallback for W&B | 5D |
| `voice_pipeline/observability/metrics.py` *(new)* | Prometheus metrics for STT/LLM/TTS | 7B |
| **Data Pipeline** | | |
| `data_pipeline/scripts/pipeline_runner.py` | Checkpointing + resume | 4D |
| `data_pipeline/scripts/download_data.py` | Retry + partial cleanup | 4E |
| `data_pipeline/scripts/store_in_chromadb.py` | Batch writes + dedup, emit company label | 4F, 0.6E |
| **Frontend** | | |
| `frontend/src/services/api.js` | Axios interceptor for 401 → refresh | 0.7F |
| **Grafana / Ops** | | |
| `grafana/dashboards/*.json` *(new)* | 6 provisioned dashboards | 7F |
| `grafana/slo.yaml` *(new)* | SLO definitions | 7E |
| `grafana/alerts.yaml` *(new)* | Alert rules | 7G |
| **CI / Workflow** | | |
| `.github/workflows/deploy-backend.yml` | Post-deploy health check | 3C |
| `.github/workflows/pre_commit_tenant_check.yml` *(new)* | Block raw SQL outside allowed files | 0.6F |
