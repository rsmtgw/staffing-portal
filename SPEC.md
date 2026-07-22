# SPEC.md — Staffing Portal Functional Specification

## Purpose

The Staffing Portal is an AI-powered web application for managing IT staffing operations. It enables:
- **Recruiters** to upload and manage bench candidates and open demands
- **Managers** to submit new staffing requests and search the bench via chat
- **Administrators** to configure AI agents, prompts, skill equivalencies, and feature flags
- **All users** to view a match dashboard scoring bench candidates against open demands

## User roles

| Role | Access |
|---|---|
| Authenticated user | All pages and chat interfaces |
| Admin | Feature flags, agent builder, skill equivalencies, escalations |
| Unauthenticated | Redirected to `/login` |

Authentication is session-cookie based. OIDC SSO (Azure AD) is optional — enable with `OIDC_ENABLED=true`.

---

## UI Pages

### `GET /login`
Login page. Username/password form. If OIDC is enabled, shows an SSO button.

### `GET /` (landing)
Home page with navigation cards linking to:
- `/report/dashboard` — Match Dashboard
- `/manage/bench` — Bench Management
- `/manage/demand` — Demand Management
- `/ui` — Agent Builder (React SPA)

### `GET /manage/bench`
Bench management UI. Features:
- **CSV upload** — upload a roster CSV; processing runs async (background thread); client polls `/api/upload-jobs/{job_id}` for progress
- **Search** — full-text search across name, skills, location
- **Table** — paginated list of candidates with Edit and Delete per row
- **Edit modal** — inline JSON patch of any candidate field
- **Bulk delete** — delete all bench candidates

Backend APIs called: `GET /api/bench`, `PUT /api/bench/{doc_id}`, `DELETE /api/bench/{doc_id}`, `DELETE /api/bench/all`, `POST /api/bench/upload-csv`, `GET /api/upload-jobs/{job_id}`

### `GET /manage/demand`
Demand management UI. Same pattern as bench management.

Backend APIs called: `GET /api/demands`, `PUT /api/demands/{role_id}`, `DELETE /api/demands/{role_id}`, `DELETE /api/demands/all`, `POST /api/demands/upload-csv`, `GET /api/upload-jobs/{job_id}`

### `GET /report/dashboard`
Serves `bench-matches-dashboard.html` — a standalone HTML file with all CSS and JavaScript inline. It:
- Calls `GET /api/report/bench-matches` to fetch pre-scored matches
- Supports SSE streaming via `GET /api/report/bench-matches/stream` for live progress
- Allows What-If re-scoring via `POST /api/report/what-if-match`
- Allows AI-powered analysis via `POST /api/report/ai-analyze`
- Shows fit tiers: Excellent / Good / Regular / No Match

### `GET /ui`
React SPA Agent Builder. Two panels:
- **Agents** — list, create, edit, delete agent configs (model, prompt key, tools, sub-agents, enabled toggle)
- **Prompts** — list, create, edit full prompt text content

---

## Chat Endpoints

All chat routes accept `POST` with body `{"message": "...", "session_id": "..."}` and return `{"reply": "...", "session_id": "..."}`.

| Endpoint | Agent | Typical use |
|---|---|---|
| `POST /api/staffing/chat` | StaffingCoordinatorAgent | Search bench, view demands, check open roles |
| `POST /api/submit-demand/chat` | SubmitDemandAgent | Submit a new staffing demand via conversation |
| `POST /api/submit-resume/chat` | SubmitResumeAgent | Submit a candidate resume via conversation |
| `POST /api/enquiry/chat` | EnquiryAgent | Read-only queries about demands or candidates |
| `POST /api/placement/chat` | PlacementTrackerAgent | View open roles, record a fulfilment |

All chat routes go through `run_chat()` in `app/runners.py`, which:
1. Applies input guard (length limit, sanitisation)
2. Invokes the ADK runner with a timeout (`CHAT_TIMEOUT_SECONDS`, default 180 s)
3. Applies output filter (PII masking in logs)

---

## REST APIs

### Bench candidates
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/bench` | List candidates (pagination + search) |
| `PUT` | `/api/bench/{doc_id}` | Merge-patch a candidate |
| `DELETE` | `/api/bench/{doc_id}` | Delete one candidate |
| `DELETE` | `/api/bench/all` | Delete all candidates |
| `POST` | `/api/bench/upload-csv` | Upload CSV; returns `job_id` |

### Demands
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/demands` | List demands (pagination + search) |
| `PUT` | `/api/demands/{role_id}` | Merge-patch a demand |
| `DELETE` | `/api/demands/{role_id}` | Delete one demand |
| `DELETE` | `/api/demands/all` | Delete all demands |
| `POST` | `/api/demands/upload-csv` | Upload CSV; returns `job_id` |

### Upload jobs
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/upload-jobs/{job_id}` | Poll job status (progress, result, errors) |
| `POST` | `/api/upload-jobs/{job_id}/pause` | Pause job |
| `POST` | `/api/upload-jobs/{job_id}/resume` | Resume job |
| `POST` | `/api/upload-jobs/{job_id}/stop` | Stop job |
| `GET` | `/api/upload-jobs` | List active jobs |

### Matching report
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/report/bench-matches` | All matches (two-tier cache) |
| `GET` | `/api/report/bench-matches/stream` | SSE progress stream |
| `POST` | `/api/report/what-if-match` | Re-score one demand × candidate |
| `POST` | `/api/report/ai-analyze` | AI GPT-4.1 re-score vs rule engine |
| `POST` | `/api/report/interaction-feedback` | Log dashboard interactions |
| `POST` | `/api/report/cache/clear` | Invalidate match cache |

### Admin
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/metrics` | Token usage counters |
| `POST` | `/feedback` | Per-turn feedback |
| `GET` | `/api/admin/feature-flags` | List feature flags |
| `POST` | `/api/admin/feature-flags/{flag}` | Set a flag value |
| `GET/POST/DELETE` | `/api/admin/equivalencies/*` | Skill equivalency management |
| `GET/POST` | `/api/escalations/*` | Escalation records |
| `GET` | `/api/fix-tasks` | FixTask records |

### Agents and Prompts
| Method | Path | Description |
|---|---|---|
| `GET/POST/PUT/DELETE` | `/api/agents/*` | Agent config CRUD |
| `GET` | `/api/agents/tools` | List registered tools |
| `POST` | `/api/agents/reload` | Rebuild all runners |
| `GET/POST/PUT` | `/api/prompts/*` | Prompt content CRUD |
| `POST` | `/api/prompts/reload` | Reload prompts from Cosmos |

---

## Data Model

### Bench Candidate (Cosmos `bench_roster` container)
| Field | Type | Description |
|---|---|---|
| `id` | string | Unique candidate ID (CAND-XXXX) |
| `name` | string | Full name |
| `current_role` | string | Current job title |
| `skills` | list[string] | Skill list |
| `primary_skills` | list[string] | Highest-weight skills |
| `secondary_skills` | list[string] | Medium-weight skills |
| `location` | string | City/region |
| `career_level` | string | Career level (e.g. CL7, CL8) |
| `availability_date` | string | When available (ISO date) |
| `years_experience` | int | Years of experience |

### Demand / Role (Cosmos `staffing_requests` container)
| Field | Type | Description |
|---|---|---|
| `role_id` | string | Unique role ID (ROLE-XXXX) |
| `role_title` | string | Job title |
| `client` | string | Client name |
| `primary_skills` | list[string] | Must-have skills |
| `secondary_skills` | list[string] | Nice-to-have skills |
| `location` | string | Work location |
| `career_level` | string | Required career level |
| `start_date` | string | Engagement start |
| `status` | string | open / filled / cancelled |

### Match Score
| Field | Description |
|---|---|
| `fit_tier` | Excellent / Good / Regular / No Match |
| `overall_score` | Weighted score (0–100) |
| `primary_match` | Matched primary skills |
| `secondary_match` | Matched secondary skills |
| `missing_skills` | Skills in demand not found on bench |

---

## Matching Algorithm

Implemented in `components/matchmaker.py` (`MatchmakerEngine`):

1. **Skill extraction** — normalise candidate skills using `skill_equivalencies.json` and `skill_utils.py`
2. **Skill matching** — for each demand skill check candidate profile:
   - Primary skill match → weight **2.0**
   - Secondary skill match → weight **1.5**
   - Other skill match → weight **1.0**
   - Embedding similarity fallback (if `SKILL_EMBED_ENABLED=true`)
3. **Score calculation** — `overall_score = Σ(weight × matched) / Σ(weight × total_demand_skills) × 100`
4. **Fit tier** — Excellent ≥ 80, Good ≥ 60, Regular ≥ 40, No Match < 40
5. **Caching** — L1 (in-process dict) + L2 (Cosmos `report-cache` container); pre-warmed on startup
