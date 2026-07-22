# CLAUDE.md — Staffing Portal project context

This file is auto-loaded by Claude Code. Read it before exploring the codebase.

## What this project is

**Staffing Portal** is an AI-powered web application for managing staffing demands and bench candidates. It provides:
- Web UI pages (bench management, demand management, match dashboard)
- Chat interfaces powered by Google ADK agents
- A React SPA for live agent and prompt configuration
- REST APIs consumed by all UIs

It is a **UI-only** extract from a larger `staffing-agent` project. Email processing, Gmail Pub/Sub, IMAP/SMTP, and GitHub PR automation have been deliberately removed. See `SPEC.md` and `ARCHITECTURE.md` for full details.

## How to run

```bash
# Local dev (Python)
pip install -r requirements.txt
cp .env.example .env   # fill in values
uvicorn app.main:app --reload --port 8000

# Docker (all services including Cosmos + Azurite emulators)
docker compose up
```

The React Agent Builder SPA requires a separate build step:
```bash
cd app/ui && npm install && npm run build
```
In dev mode run `npm run dev` (port 5173); the FastAPI backend serves the compiled build from `app/ui/dist/` at `/ui`.

## Directory map

```
app/
  main.py          — FastAPI app assembly; wires all routers + middleware
  lifespan.py      — Startup: pre-warms bench-match report cache
  config.py        — All env-var settings (Settings class + runtime flags)
  auth.py          — Session-cookie auth; NeedsLoginRedirect exception
  models.py        — Pydantic request/response schemas
  runners.py       — run_chat(): input guard → ADK runner → output filter
  routes/
    auth.py        — GET /login, POST /api/auth/login, GET /logout
    oidc.py        — /oidc/* — Azure AD OIDC SSO flow
    chat.py        — POST /api/*/chat — all chat endpoints
    manage.py      — GET /manage/bench, /manage/demand + bench/demand REST APIs
    report.py      — GET /report/dashboard + /api/report/* matching APIs
    admin.py       — /health, /metrics, feature flags, escalations, equivalencies
    agents.py      — /api/agents/* — agent config CRUD (Agent Builder backend)
    prompts.py     — /api/prompts/* — prompt content CRUD
  agents/
    dynamic_factory.py   — build_agent(name): reads Cosmos config → LlmAgent
    tool_registry.py     — TOOL_REGISTRY: maps tool names → Python callables
    staffing_agent/      — StaffingCoordinatorAgent (bench search, demand view)
    submit_demand/       — SubmitDemandAgent (demand intake via chat)
    submit_resume/       — SubmitResumeAgent (resume submission via chat)
    enquiry_agent/       — EnquiryAgent (read-only queries)
    placement_tracker/   — PlacementTrackerAgent (record placements)
    bench_roster/        — BenchRosterAgent
    bench_list_ingestor/ — BenchListIngestorAgent (CSV bulk load)
    demand_list_ingestor/— DemandListIngestorAgent (CSV bulk load)
    shared/              — skill_utils.py and other shared helpers
  services/
    job_service.py         — UploadJobService: async CSV upload job tracking
    escalation_store.py    — Escalation + FixTask CRUD in Cosmos
    equivalency_service.py — Skill equivalency lookups (Cosmos + TTL cache)
    conversation.py        — Conversation state management
    match_cache.py         — Match result caching
    semantic_cache.py      — Semantic similarity caching
    query_router.py        — Query routing
  security/
    input_guard.py   — Sanitises chat messages, enforces length limits
    output_filter.py — Masks PII in logged replies
    content_filter.py— Content filtering rules
  data/
    common_skills.json — Common skills list for bench row enrichment
  ui/                  — React 18 + Vite SPA (Agent Builder)
    src/App.tsx        — Root component: agents list + prompts list panels
    src/components/    — AgentList, AgentEditor, PromptList, PromptEditor, modals
    src/api/           — API client functions for /api/agents and /api/prompts
components/
  matchmaker.py        — MatchmakerEngine: deterministic skill-based scorer
conf/
  agents.json          — Default agent configs seeded to Cosmos on startup
  career_levels.json   — Career level (CL) mapping
  skill_equivalencies.json — Skill equivalency definitions (local fallback)
  runtime_flags.json   — Runtime feature flags (local fallback)
  prompt/              — Raw text prompt files for each agent (15 files)
  prompt_loader.py     — get_prompt(key): Cosmos with in-process cache
observability/
  cost_tracker.py      — Token usage / request counters
  feedback.py          — Per-turn feedback recording
  tracer.py            — Distributed tracing
bench-matches-dashboard.html — Standalone dashboard HTML served at /report/dashboard
tests/                 — Pytest test suite (7 files, no email tests)
```

## Key request flows

| User action | Route | Runner | Agent | Tools |
|---|---|---|---|---|
| Bench search chat | `POST /api/staffing/chat` | `run_chat()` | `StaffingCoordinatorAgent` | bench_search, demand tools |
| Submit demand chat | `POST /api/submit-demand/chat` | `run_chat()` | `SubmitDemandAgent` | cosmos_store, request_store |
| Submit resume chat | `POST /api/submit-resume/chat` | `run_chat()` | `SubmitResumeAgent` | blob_store, parse_resume |
| Enquiry chat | `POST /api/enquiry/chat` | `run_chat()` | `EnquiryAgent` | bench_search, demand tools |
| Placement chat | `POST /api/placement/chat` | `run_chat()` | `PlacementTrackerAgent` | intake_store |
| View dashboard | `GET /report/dashboard` | static file | — | — |
| Dashboard scores | `GET /api/report/bench-matches` | — | — | matching_report_tool |
| Manage bench | `GET /manage/bench` | server-rendered HTML | — | `/api/bench` REST |
| Agent Builder | `GET /ui` | React SPA | — | `/api/agents`, `/api/prompts` |

## Where to add things

- **New chat agent**: create `app/agents/<name>/agent.py`, register tools in `tool_registry.py`, add route in `app/routes/chat.py`, add agent config to `conf/agents.json`
- **New REST route**: add to the appropriate router file in `app/routes/`
- **New prompt**: add a file to `conf/prompt/` and reference it in `conf/prompt_files.py`
- **New tool**: add to `app/agents/<agent>/tools/` and register in `tool_registry.py`
- **New feature flag**: add to `_EXPOSED_FLAGS` in `app/routes/admin.py` and add the field to `Settings` in `app/config.py`

## What is intentionally absent

The following features were stripped from the parent `staffing-agent` project and must NOT be re-added here:
- Email processing (IMAP, SMTP, Gmail Pub/Sub, EmailPollerOrchestrator)
- GitHub PR automation (CodingRunner, MergeGate)
- Polling lock (PollingLock for distributed email poll deduplication)

## See also

- [SPEC.md](SPEC.md) — Functional specification: pages, APIs, data model
- [ARCHITECTURE.md](ARCHITECTURE.md) — Technical architecture: layers, patterns, Mermaid diagram
