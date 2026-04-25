# FrontShiftAI

![FrontShiftAI App](docs/images/frontend.png)

**AI Concierge for the Deskless Workforce**

![Python](https://img.shields.io/badge/Python-3.12+-blue) ![React](https://img.shields.io/badge/React-18.2-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.104-green) ![Docker](https://img.shields.io/badge/Docker-Enabled-blue) ![GCP](https://img.shields.io/badge/GCP-Cloud%20Run-orange)

> This repository is a personal continuation of a project originally built as team coursework at Northeastern University. It is currently undergoing a production-readiness overhaul — see **[Production Readiness Roadmap](#production-readiness-roadmap)** below for the in-progress plan.

---

## Video Demo


[![Watch the Demo](https://img.youtube.com/vi/PLACEHOLDER/0.jpg)](https://drive.google.com/drive/folders/1-BDy_7jMf0nWLNDfPSK6pi_NX8RqyYpC?usp=sharing)

---

## Abstract

**FrontShiftAI** is an enterprise-grade, multi-agent artificial intelligence platform designed to bridge the "HR gap" for deskless workers—nurses, construction foremen, and field technicians who lack access to traditional corporate portals. By functioning as a **24/7 Operations Concierge**, the system ingests thousands of pages of static organizational handbooks and transforms them into an interactive, actionable voice and chat interface.

Unlike standard chatbots, FrontShiftAI operates as a **Multi-Tenant System of Agents**. It doesn't just answer questions; it intelligently orchestrates complex workflows—verifying PTO balances, scheduling HR meetings, and searching the open web for real-time operational data—all while maintaining strict data isolation across 19+ distinct organizations.

---

## Deployment Access

| Component | Provider | URL |
|-----------|----------|-----|
| **Frontend App** | **Vercel** ( Recommended) | [https://frontshiftai.vercel.app/](https://frontshiftai.vercel.app/) |
| **Frontend App** | Cloud Run | [https://frontshiftai-frontend-vvukpmzsxa-uc.a.run.app](https://frontshiftai-frontend-vvukpmzsxa-uc.a.run.app) |
| **Backend API** | Cloud Run | [https://frontshiftai-backend-vvukpmzsxa-uc.a.run.app/docs](https://frontshiftai-backend-vvukpmzsxa-uc.a.run.app/docs) |

---

## Key Features

### 1. Multi-Agent Intelligence

The system isn't just a chatbot; it's a squad of specialized agents coordinated by a central brain:

*   **Unified Router**: Automatically understands if a user is asking a policy question, requesting time off, or reporting a grievance and routes it to the right expert.
*   **RAG Agent (The Librarian)**: Uses advanced Retrieval-Augmented Generation to search company PDFs. It cites its sources (page numbers and links) so users can trust the answer.
*   **PTO Agent (The HR Assistant)**: A transactional agent that can:
    *   Check live leave balances.
    *   Understand natural language requests ("I need next Friday off").
    *   Validate requests against holidays and blackout dates.
    *   Book the time off in the database.
*   **HR Ticket Agent (The Support Rep)**: Handles complex inquiries that require human intervention. It categorizes issues (Payroll, Benefits, etc.), assigns priority, and schedules meetings.
*   **Website Extraction Agent (The Researcher)**: If the handbook doesn't have the answer (e.g., "What are the office hours?"), it automatically searches the company's public website for real-time info.

### 1.1 LLM Architecture & Resiliency

To ensure 99.9% uptime and low latency, we employ a robust fallback strategy across different model providers:

| Component | Main LLM | Backup Chain (in order) |
| :--- | :--- | :--- |
| **LLM Decider** (Routing) | **Groq**<br>(Llama 3.1 8B Instant) | 1. Mercury<br>2. OpenAI (GPT-4o-mini)|
| **Agentic Flow** (PTO/HR) | **Groq**<br>(Llama 3.1 8B Instant) | 1. Mercury<br>2. OpenAI (GPT-4o-mini)|
| **RAG Model** (Generator) | **Mercury**<br>(Custom Model) | 1. Groq<br>2. OpenAI (GPT-4o-mini)|

### 2. Multi-Tenant Architecture

*   **One System, Many Companies**: A single deployment serves multiple organizations (Crouse Medical, TechCorp, RetailCo).
*   **Data Isolation**: Each company's data (documents, users, tickets) is strictly segregated.
*   **Dynamic Branding**: The UI adapts to the user's company context.

### 3. Super Admin & Company Management

*   **Self-Service Onboarding**: Super Admins can add new companies instantly.
    *   *Input*: Company Name, Domain, Handbook PDF URL.
    *   *Automation*: The system automatically downloads the PDF, runs OCR, chunks the text, generates embeddings, and rebuilds the vector index—all in the background.
    *   *Consistency*: The new index is synced to Google Cloud Storage (GCS) so all API instances update automatically.
*   **Bulk Management**: Tools to bulk-delete users or remove entire companies cleanly.

###  4. Enterprise-Grade Operations

*   **Model Registry**: We version-control our AI "brains". We can rollout v2 and rollback to v1 instantly if issues arise.
*   **Monitoring**: Real-time dashboards (Weights & Biases) track token usage, latency, and user feedback (thumbs up/down).
*   **CI/CD**: Automated GitHub Actions for testing backend/frontend and retraining RAG models.

### 5. Real-Time Voice Interface

*   **Hands-Free Interaction**: Enables nurses and field technicians to query policy and request PTO via natural conversation while working.
*   **Latency**: ~3.4s end-to-end today (Deepgram STT/TTS + LLM + TTS), with a sub-1.5s p95 target in the active roadmap (streaming, connection pooling, Groq-first voice path).
*   **Secure Authentication**: Voice sessions are fully authenticated via short-lived (1h) JWTs bridged to a 6h voice-scoped token so long conversations don't expire mid-turn.

---

## Documentation

Detailed guides for every subsystem are available in their respective directories:

| Documentation | Description | Link |
|---------------|-------------|------|
| **Data Pipeline** | Ingestion logic, OCR, Embedding generation | [Data Pipeline README](./data_pipeline/README.md) |
| **ML Pipeline** | RAG evaluation, DeepEval framework | [Chat Pipeline README](./chat_pipeline/README.md) |
| **Voice Pipeline** | Real-time voice orchestration, LiveKit integration, and Modal deployment | [Voice Pipeline README](./voice_pipeline/README.md) |
| **Backend Architecture** | API internals, LangGraph agents, and Services | [Backend README](./backend/README.md) |
| **Frontend Architecture** | Component hierarchy, Design System, UX flows | [Frontend README](./frontend/README.md) |
| **Cloud Deployment** | Infrastructure setup, CI/CD, Secrets, and Cost Analysis | [Deployment README](./deployment/README.md) |
| **Monitoring** | W&B tracking, Cloud Logging, and Alerts | [Monitoring README](./monitoring/README.md) |

---

## Repository Structure

```
FrontShiftAI/
├── .github/
│   └── workflows/
│       ├── deploy-backend.yml      # 🚀 Production Deployment (Cloud Run)
│       ├── deploy-frontend.yml     # 🚀 Frontend Deployment
│       └── model_deploy.yml        # 🤖 ML Pipeline CI/CD Tests
│
├── backend/
│   ├── api/                        # 📡 REST API Layer (FastAPI)
│   │   ├── admin.py                # Admin Dashboard Endpoints
│   │   ├── auth.py                 # JWT Authentication & Login
│   │   ├── unified_agent.py        # 🧠 Main Chat Router (The "brain" of the chat)
│   │   ├── health.py               # Kubernetes/Cloud Run Health Checks
│   │   └── ... (agent specific routes)
│   │
│   ├── agents/                     # 🤖 LangGraph Agent Definitions
│   │   ├── pto/                    # PTO Transactional Logic
│   │   ├── hr_ticket/              # HR Ticketing Workflow
│   │   ├── website_extraction/     # Brave Search Integration
│   │   └── evaluation/             # DeepEval Agent Tests
│   │
│   ├── db/                         # 💾 Database Layer
│   │   ├── models.py               # SQLAlchemy Tables (Users, Tickets, PTO)
│   │   ├── session.py              # PostgreSQL Session Management
│   │   └── seed.py                 # Initial Data Factory
│   │
│   ├── services/                   # ⚙️ Shared Business Logic
│   │   ├── auth_service.py         # Password Hashing & Token Gen
│   │   └── rag_service.py          # Vector Search Abstraction
│   │
│   ├── monitoring/                 # 📊 Observability
│   │   └── middleware.py           # W&B Request Interceptor
│   │
│   └── main.py                     # 🏁 APPLICATION ENTRY POINT
│                                   # - Initializes FastAPI
│                                   # - Warmups (Preloads ChromaDB & Models)
│                                   # - Mounts Middlewares (CORS, Monitoring)
│
├── frontend/                       # ⚛️ React Application (Vite)
│   ├── src/
│   │   ├── components/             # UI Building Blocks
│   │   ├── services/               # Axios API Clients
│   │   ├── hooks/                  # Custom React Hooks
│   │   └── App.jsx                 # Main Router & Layout
│
├── data_pipeline/                  # 🏭 Data Factory (ETL)
│   ├── data/
│   │   ├── raw/                    # 📥 Input: PDF Handbooks
│   │   └── vector_db/              # 📤 Output: ChromaDB Artifacts
│   ├── scripts/
│   │   ├── ingestion.py            # OCR & Text Extraction
│   │   ├── embedding.py            # HuggingFace Vectorization
│   │   └── validation.py           # Data Quality Checks
│   └── airflow/                    # (Optional) DAG Definitions
│
├── chat_pipeline/                  # 🔬 Research Lab (MLOps)
│   ├── rag/                        # Core RAG Algorithms
│   │   ├── retriever.py            # Semantic Search Logic
│   │   └── generator.py            # LLM Prompt Engineering
│   └── evaluation/                 # Test Suites
│       └── test_groundedness.py    # Hallucination Detection
│
├── deployment/                     # ☁️ Infrastructure as Code
│   ├── README.md                   # 📖 Deployment Guide & Specs
│   ├── Dockerfile.backend          # Multi-stage Python Build
│   ├── Dockerfile.frontend         # Nginx Static Build
│
├── voice_pipeline/                 # 🎙️ Real-Time Voice Agent
│   ├── scripts/                    # 🏁 Main Logic & Entrypoints
│   │   └── main.py                 #    - Voice Worker Entrypoint
│   ├── utils/                      # 🛠️ Helpers (Metrics, Logging)
│   ├── configs/                    # 📝 Provider Chains (YAML)
│   └── modal_deploy.py             # 🚀 Serverless Deployment Def
│
├── docker-compose.yml              # 🐳 Local Development Orchestration
└── system_diagram.png              # 📐 Architecture Visualization
```

---



## Cloud Deployment Architecture

The system is architected as a serverless microservices application on **Google Cloud Platform (GCP)**.

### Infrastructure Components
For a detailed breakdown of all resources, see the [Deployment Guide](./deployment/README.md#cloud-resources-inventory).

| Component | Service | Role |
|-----------|---------|------|
| **Compute** | **Cloud Run** | Runs the Backend (FastAPI) and Frontend (React/Nginx) containers. Scales to zero to minimize costs. |
| **Database** | **Cloud SQL** | Managed PostgreSQL 15 instance storing user data, PTO requests, ticket queues, and relational state. |
| **Vector Store** | **ChromaDB + GCS** | Local vector database loaded into memory from Google Cloud Storage on container startup. |
| **Security** | **Secret Manager** | Injects API keys (`GROQ`, `MERCURY`) and DB credentials at runtime. |
| **Backups** | **Automated** | Daily backups of SQL data (3 AM UTC); Immutable artifact versioning for Vector DBs. |

### System Diagram
![System Architecture Diagram](./docs/images/system_diagram.png)

---

## Technical Stack

### Core Application
- **Backend Language**: Python 3.12 (AsyncIO)
- **Frontend Framework**: React 18 (Vite, TypeScript, Tailwind CSS)
- **API Framework**: FastAPI
- **Database**: PostgreSQL 15 (SQLAlchemy ORM)

### AI & Machine Learning
- **Orchestration**: LangGraph (Multi-Agent State Machine)
- **Vector Database**: ChromaDB (Persistence via GCS)
- **Embeddings**: HuggingFace (`all-MiniLM-L6-v2`)
- **LLM Providers**: 
  - **Inception/Mercury**: Primary Generation
  - **Groq (Llama 3)**: Fallback & Reasoning
- **Evaluation**: DeepEval, Weights & Biases

### Voice Pipeline
- **Orchestration**: LiveKit (WebRTC)
- **Compute**: Modal (Serverless Python)
- **STT**: Deepgram Nova 2 (Fallback: AssemblyAI)
- **TTS**: Deepgram Aura (Fallback: Cartesia)
- **VAD**: Silero VAD

### DevOps & Infrastructure
- **Containerization**: Docker (Multi-stage builds)
- **CI/CD**: GitHub Actions (Workload Identity Federation)
- **Cloud**: GCP (Cloud Run, Cloud SQL, GCS, Secret Manager)

---

## Data Usage

The system acts as a knowledge engine for organizational data.
1.  **Ingestion**: `data_pipeline` scripts ingest raw PDF handbooks (~50-100 pages each).
2.  **Processing**: OCR (Tesseract) extracts text, which is then chunked into 500-token semantic segments.
3.  **Vectorization**: Chunks are embedded and stored in a shared **ChromaDB** index, partitioned by `company_id`.
4.  **Storage**: The final vector artifact (~500MB) is compressed and stored in Cloud Storage, ensuring stateless application containers.

---

## Monitoring & Testing

### Monitoring Strategy
We utilize a dual-layer strategy to ensure reliability:
1.  **Infrastructure Level**: Google Cloud Monitoring tracks container latency, error rates (5xx), and CPU/Memory usage.
2.  **Application Level**: **Weights & Biases (W&B)** traces every LLM interaction, logging:
    - Token consumption and Cost per request.
    - Agent routing accuracy (Did the router pick the right tool?).
    - User feedback signals (Thumbs up/down).

### Testing Protocol
- **Unit Tests**: `pytest` suite ensuring agent logic validity (`backend/tests`).
- **Integration Tests**: Dockerized flows verifying SQL and Vector DB connectivity.
- **Bias Analysis**: Automated scripts in `data_pipeline` measuring response handling across different demographic terminologies.

---

## Fairness & Bias Mitigation

We actively monitor for **Representation Bias** to ensure equitable performance across all 19+ tenant organizations, regardless of their size or industry.

### 1. Data Bias Analysis
- **Metric**: Gini Coefficient of Handbook Volume.
- **Current Score**: **0.250** (Low Imbalance).
- **Finding**: While some companies have larger handbooks (e.g., *Buchheit Logistics* with ~13% of total data), the distribution remains healthy. No company suffers from "data poverty" (<10 chunks).

### 2. Performance Proxies & Mitigation strategies
| Risk Type | Indicator | Mitigation Strategy |
| :--- | :--- | :--- |
| **Retrieval Confusion** | Large Handbooks (>80 chunks) | **Adaptive Retrieval**: Increased `top_k` (6-8) to filter "distractor" chunks. |
| **Hallucination** | Sparse Handbooks (<20 chunks) | **Confidence Thresholds**: Strict gating; Agent returns "I don't know" rather than inventing policy. |
| **Term Frequency** | "Harassment" tag dominance (151x) | **Tag Enrichment**: Fine-tuning extraction agents to recognize niche policy categories. |

*For a detailed report, see [Company Bias Analysis](./docs/company_bias_analysis.md).*

---

## Cost Analysis (Monthly Estimate)

Designed for student-budget constraints (Free Tier capable).

| Service | Configuration | Est. Monthly Cost |
|---------|---------------|-------------------|
| **Cloud SQL** | `db-f1-micro` | ~$10.00 |
| **Cloud Run** | Scale-to-Zero | ~$2.00 |
| **Cloud Storage** | Standard (<1GB) | < $0.10 |
| **LLM APIs** | Groq/Mercury (Free Tiers) | $0.00 |
| **Total** | | **~$12.10** |

---

##  Installation & Contributing

### Prerequisites
- Python 3.10+
- Node.js 18+
- Docker (Optional but recommended)
- `gcloud` CLI (for deployment)

### Quick Start (Local Development)

1.  **Clone the Repo**
    ```bash
    git clone https://github.com/Raghavgali/FrontShiftAI.git
    cd FrontShiftAI
    ```

2.  **Backend Setup**
    ```bash
    cd backend
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    
    # Create .env file with your keys
    cp .env.example .env
    
    python main.py
    ```

3.  **Frontend Setup**
    ```bash
    cd frontend
    npm install
    npm run dev
    ```

4.  **Visit**: `http://localhost:5173` (Vite) to interact with the local Concierge.

### Contributing
This is a personal project; external contributions aren't expected, but if you find a real bug, feel free to open an issue. Local testing conventions:
- Run `pytest backend/tests -v` before any change that touches the API or DB layer.
- Run `pytest stress_tests/ -v` to exercise the Phase 0 resilience, idempotency, tenancy, and JWT test suites (see `stress_tests/README.md` for env vars).

---

## Production Readiness Roadmap

The codebase was originally a team coursework submission; it is now being hardened along a structured plan (see [`plan.md`](./plan.md) and [`system_design.md`](./system_design.md)) before further feature work. Status as of this README:

| Phase | Focus | Status |
|-------|-------|--------|
| **0 — Critical Resilience** | Block SQLite fallback in prod, fail-fast on ChromaDB/warmup failure, thread-safe caches + LLM singleton, voice tool retries with graceful fallback, 429 `Retry-After` for Mercury/OpenAI | ✅ Complete |
| **0.5 — Idempotency Keys** | `IdempotencyRecord` table, FastAPI dependency on mutation endpoints (`/api/pto/chat`, `/api/hr-tickets/chat`, `/api/chat/message`), voice agent auto-generates and reuses keys across retries, daily Celery cleanup | ✅ Complete |
| **0.6 — Multi-Tenant Hardening** | Request-scoped `ContextVar` + SQLAlchemy `before_compile` listener auto-filters by `company`; `bypass_tenant_filter()` ctx manager with audit log; `TenantScopedRetriever` for ChromaDB; startup validator fails fast if any chunk lacks a `company` label; GH Actions blocks raw SQL outside allow-list | ✅ Complete |
| **0.7 — JWT Refresh Tokens** | Access-token TTL 1y → 1h; `RefreshToken` stored as SHA-256 hash; rotation-on-use with chain-burn theft detection; `/refresh`, `/logout`, `/voice-token` endpoints; frontend Axios interceptor auto-refreshes on 401 and replays the original request | ✅ Complete |
| **6.5 — Resilience Policy Matrix** | Single source of truth for timeout / retry / backoff / circuit-breaker per call type; `@resilient(policy=…)` decorator with per-key circuit breaker; Brave Search migrated as the reference implementation | ✅ Complete |
| **7 — Prometheus + Grafana Observability** | Backend + voice-pipeline instrumentation (four golden signals), correlation IDs, SLO definitions, six provisioned dashboards, Locust → Prometheus export | ⏳ Up next |
| **1–5 — Latency & Streaming** | HTTP pooling, VAD tuning, `max_tokens` + Groq voice path, SSE streaming extended to PTO/HR agents, caching, prefetch | ⏳ Deferred (after Phase 7 baseline) |
| **5.5 — Durable LangGraph Checkpointing** | `PostgresSaver` checkpointer for multi-turn resume + admin-approval workflows | ⏳ Deferred |

Tests for completed phases live in [`stress_tests/`](./stress_tests/); policy docs in [`docs/resilience_policy.md`](./docs/resilience_policy.md) and [`docs/resilience_audit.md`](./docs/resilience_audit.md).

---

## License
Originally developed as a coursework project at Northeastern University (MLOps Group 9); this repository is a personal continuation by Raghav Gali. License: **TBD** — treat as all-rights-reserved until a formal license file is added.
