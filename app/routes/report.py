"""
app/routes/report.py
--------------------
Reporting endpoints — deterministic, zero-LLM-cost reports.

  GET /api/report/bench-matches
      Score all bench candidates against all open demands.
      Returns structured JSON grouped by demand.

Query parameters:
  fit_filter — comma-separated fit tiers to include (default: Excellent,Good,Regular)
               valid: Excellent, Good, Regular, No Match
  role_id    — restrict to a single demand (e.g. ROLE-003)
  page       — 1-based page number (default: 1)
  page_size  — demands per page (default: 100, max: 500)
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app.auth import require_api_auth, require_html_auth
from app.models import (
    AiAnalyzeRequest,
    InteractionFeedbackRequest,
    InteractionFeedbackResponse,
    SkillProfileEntry,
    WhatIfMatchRequest,
    WhatIfMatchResponse,
)

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["reports"])

# ── Two-tier scoring cache ────────────────────────────────────────────────────
# L1: in-process dict (instant, lost on restart)
# L2: Cosmos DB report_cache container (survives restarts / scale-to-zero)
#
# On read:  check L1 → check L2 → score from scratch
# On write: write L1 + L2 simultaneously
# On clear: wipe L1 + L2

_cache: dict[str, dict] = {}          # key → full scored result (L1)
_REPORT_CACHE_VERSION = "v2"


# The cache always stores ALL tiers (Excellent/Good/Regular/No Match).
# fit_filter is applied at display time so changing the filter never triggers
# a rescore — only role_id scoping affects what gets computed.

def _cache_key(role_id: str) -> str:
    """Return a stable string key — role_id only (fit_filter is a display filter)."""
    scope = role_id.strip().lower() or "__all__"
    return f"{_REPORT_CACHE_VERSION}:{scope}"


def _apply_fit_filter(full: dict, fit_filter: str) -> dict:
    """Return a shallow copy of *full* with demands filtered to the requested tiers."""
    wanted = {t.strip() for t in fit_filter.split(",") if t.strip()}
    if not wanted or wanted >= {"Excellent", "Good", "Regular", "No Match"}:
        return full  # no filtering needed
    include_no_match = "No Match" in wanted
    candidate_tiers = wanted - {"No Match"}
    filtered_demands = []
    for d in full.get("demands", []):
        candidates = [c for c in (d.get("candidates") or []) if c.get("fit") in candidate_tiers]
        if candidates:
            filtered_demands.append({**d, "candidates": candidates})
        elif include_no_match and not any(
            c.get("fit") in {"Excellent", "Good", "Regular"} for c in (d.get("candidates") or [])
        ):
            filtered_demands.append({**d, "candidates": []})
    out = dict(full)
    out["demands"] = filtered_demands
    return out


def _get_cached(key: str) -> dict | None:
    # L1 hit
    if key in _cache:
        return _cache[key]
    # L2 hit (Cosmos — persists across restarts)
    try:
        from app.agents.submit_demand.tools.cosmos_store import load_report_cache
        result = load_report_cache(key)
        if result is not None:
            _cache[key] = result   # promote to L1
            _logger.info("[report_cache] L2 Cosmos hit for key=%r — promoted to L1", key)
            return result
    except Exception as exc:
        _logger.debug("[report_cache] Cosmos read error (non-fatal): %s", exc)
    return None


def _set_cached(key: str, result: dict) -> None:
    _cache[key] = result   # L1
    try:
        from app.agents.submit_demand.tools.cosmos_store import save_report_cache
        save_report_cache(key, result)   # L2
    except Exception as exc:
        _logger.warning("[report_cache] Cosmos write error (non-fatal): %s", exc)


def _to_skill_profile_entries(skill_profile: list[dict]) -> list[SkillProfileEntry]:
    entries: list[SkillProfileEntry] = []
    for item in skill_profile:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill", "")).strip()
        if not skill:
            continue
        try:
            prof = int(item.get("proficiency", 2))
        except (TypeError, ValueError):
            prof = 2
        prof = max(1, min(5, prof))
        source = str(item.get("source") or "csv")
        entries.append(SkillProfileEntry(skill=skill, proficiency=prof, source=source))
    return entries


def _build_demand_skill_analysis(demand: dict) -> dict:
    from app.agents.shared.skill_utils import extract_skill_name as _extract_skill_name, parse_demand_skill_column as _parse_demand_skill_column

    intake = demand.get("staffing_intake") or {}
    skill_analysis = dict(demand.get("skill_analysis") or {})
    raw_skills = intake.get("skills") or []
    top_primary = demand.get("primary_skill", "") or ""
    top_secondary_raw = demand.get("secondary_skills") or demand.get("role_secondary_skill", "")
    top_other_raw = demand.get("other_skills") or demand.get("role_other_skills", "")
    top_secondary = _parse_demand_skill_column(top_secondary_raw)
    top_other = _parse_demand_skill_column(top_other_raw)
    computed = {
        "primary_skill": (
            _extract_skill_name(raw_skills[0]) if raw_skills else top_primary
        ),
        "secondary_skills": (
            [_extract_skill_name(s) for s in raw_skills[1:3]] if len(raw_skills) > 1 else top_secondary
        ),
        "other_skills": [_extract_skill_name(s) for s in raw_skills[3:]] if len(raw_skills) > 3 else top_other,
        "inferred_skills": skill_analysis.get("inferred_skills") or [],
        "accenture_level": skill_analysis.get("accenture_level") or intake.get("career_level") or demand.get("career_level") or "",
        "demand_capability": skill_analysis.get("demand_capability") or "",
        "seniority_indicator": skill_analysis.get("seniority_indicator") or "",
    }
    if skill_analysis:
        merged = dict(skill_analysis)
        for key, value in computed.items():
            existing = merged.get(key)
            if existing in (None, "", []):
                merged[key] = value
        return merged
    return computed


def _candidate_for_matching(candidate: dict, edited_profile: list[SkillProfileEntry] | None) -> dict:
    out = dict(candidate)
    if edited_profile is None:
        return out

    profile = [{"skill": e.skill, "proficiency": e.proficiency} for e in edited_profile]
    out["skill_profile"] = profile
    out["skills"] = [e["skill"] for e in profile]
    return out


def _infer_additional_skills(candidate: dict) -> list[SkillProfileEntry]:
    from app.agents.shared.skill_utils import build_skill_profile

    explicit_profile = build_skill_profile(str(candidate.get("skill_list") or ""))
    explicit_names = {str(e.get("skill", "")).strip().lower() for e in explicit_profile if e.get("skill")}

    full_profile: list[dict] = []
    profile = candidate.get("skill_profile")
    if isinstance(profile, list) and profile:
        full_profile = profile
    else:
        for s in (candidate.get("skills") or []):
            if isinstance(s, str) and s.strip():
                full_profile.append({"skill": s.strip(), "proficiency": 2})

    inferred: list[SkillProfileEntry] = []
    for item in full_profile:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill", "")).strip()
        if not skill or skill.lower() in explicit_names:
            continue
        try:
            prof = int(item.get("proficiency", 2))
        except (TypeError, ValueError):
            prof = 2
        inferred.append(SkillProfileEntry(skill=skill, proficiency=max(1, min(5, prof)), source="inferred"))

    dedup: dict[str, SkillProfileEntry] = {}
    for e in inferred:
        dedup[e.skill.lower()] = e
    return list(dedup.values())


def _find_candidate(roster: list[dict], req: WhatIfMatchRequest) -> dict | None:
    if req.candidate_id:
        for c in roster:
            if str(c.get("candidate_id") or "") == req.candidate_id:
                return c
    if req.candidate_name and req.candidate_role:
        rn = req.candidate_name.strip().lower()
        rr = req.candidate_role.strip().lower()
        for c in roster:
            if str(c.get("name") or "").strip().lower() == rn and str(c.get("role") or "").strip().lower() == rr:
                return c
    if req.candidate_name:
        rn = req.candidate_name.strip().lower()
        for c in roster:
            if str(c.get("name") or "").strip().lower() == rn:
                return c
    return None


@router.get("/api/report/bench-matches")
async def bench_matches_report(
    fit_filter: Annotated[
        str,
        Query(description="Comma-separated fit tiers: Excellent,Good,Regular,No Match"),
    ] = "Excellent,Good,Regular",
    role_id: Annotated[
        str,
        Query(description="Restrict to a single demand, e.g. ROLE-003. Leave empty for all."),
    ] = "",
    page: Annotated[
        int,
        Query(description="1-based page number.", ge=1),
    ] = 1,
    page_size: Annotated[
        int,
        Query(description="Demands per page. Use 9999 to fetch all.", ge=1),
    ] = 20,
    refresh: Annotated[
        bool,
        Query(description="Set to true to bypass cache and recompute scores."),
    ] = False,
) -> dict:
    """Score all bench candidates against all open (pending) demands.

    Results are cached in memory for 2 minutes — pagination is instant on
    subsequent requests.  Pass ?refresh=true to force a recompute.
    """
    import math

    from app.agents.submit_demand.tools.matching_report_tool import (
        generate_bench_matching_report_structured,
    )

    key = _cache_key(role_id)
    cached = None if refresh else _get_cached(key)
    cache_hit = cached is not None

    try:
        if cached is None:
            _logger.info("[bench_matches_report] Cache miss — scoring demands (role=%s)", role_id)
            # Always score all tiers; fit_filter applied at display time
            cached = generate_bench_matching_report_structured(
                fit_filter="Excellent,Good,Regular,No Match",
                role_id=role_id,
            )
            _set_cached(key, cached)
        else:
            _logger.debug("[bench_matches_report] Cache hit (role=%s fit=%s)", role_id, fit_filter)

        # Apply fit_filter as a display filter (never triggers rescore)
        display = _apply_fit_filter(cached, fit_filter)
        # Slice for the requested page
        result = dict(display)
        all_demands = display.get("demands", [])
        total = len(all_demands)
        total_pages = max(1, math.ceil(total / page_size))
        page = min(page, total_pages)
        start = (page - 1) * page_size
        result["demands"] = all_demands[start: start + page_size]
        result["pagination"] = {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
        result["cache_hit"] = cache_hit
        return result
    except Exception as exc:
        _logger.error("[bench_matches_report] Failed: %s", exc, exc_info=True)
        return {
            "error": str(exc),
            "bench_size": 0,
            "demands_scored": 0,
            "fit_filter": [],
            "demands": [],
            "pagination": {"total": 0, "page": 1, "page_size": page_size, "total_pages": 0},
            "cache_hit": False,
        }


@router.get("/api/report/bench-matches/stream", include_in_schema=False, dependencies=[Depends(require_html_auth)])
async def bench_matches_stream(
    role_id: Annotated[str, Query()] = "",
    refresh: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    """SSE endpoint: streams scoring progress then a small \"done\" signal.

    The client fetches actual data via GET /api/report/bench-matches after
    receiving \"done\".  This avoids sending a 24MB SSE payload which causes
    browsers to drop the EventSource connection.

    Events:
      {"type":"start"}
      {"type":"progress","progress":5,"total":450}
      {"type":"done","cache_hit":true}
      {"type":"error","message":"..."}
    """
    import asyncio
    import json as _json
    import threading

    from app.agents.submit_demand.tools.matching_report_tool import (
        generate_bench_matching_report_structured,
    )

    key = _cache_key(role_id)

    async def event_stream():
        # ── Cache hit: instant done signal ────────────────────────────────────
        cached = None if refresh else _get_cached(key)
        if cached is not None:
            yield f"data: {_json.dumps({'type': 'done', 'cache_hit': True})}\n\n"
            return

        # ── Cache miss: score in background thread, stream progress ───────────
        _progress: list[dict] = []
        _error: list[str] = []
        _done = threading.Event()

        def _on_progress(current: int, total: int) -> None:
            _progress.append({"type": "progress", "progress": current, "total": total})

        def _run() -> None:
            try:
                r = generate_bench_matching_report_structured(
                    fit_filter="Excellent,Good,Regular,No Match",
                    role_id=role_id,
                    progress_callback=_on_progress,
                )
                _set_cached(key, r)
            except Exception as exc:
                _error.append(str(exc))
            finally:
                _done.set()

        yield f"data: {_json.dumps({'type': 'start'})}\n\n"
        threading.Thread(target=_run, daemon=True).start()

        while not _done.is_set():
            while _progress:
                yield f"data: {_json.dumps(_progress.pop(0))}\n\n"
            await asyncio.sleep(0.15)

        while _progress:
            yield f"data: {_json.dumps(_progress.pop(0))}\n\n"

        if _error:
            yield f"data: {_json.dumps({'type': 'error', 'message': _error[0]})}\n\n"
            return

        # Small done signal — client fetches data separately via HTTP
        yield f"data: {_json.dumps({'type': 'done', 'cache_hit': False})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/api/report/what-if-match", response_model=WhatIfMatchResponse, dependencies=[Depends(require_api_auth)])
async def what_if_match(request: WhatIfMatchRequest) -> WhatIfMatchResponse:
    """Recompute one demand × candidate match with optional edited candidate skills."""
    from app.agents.submit_demand.tools.cosmos_store import load_request_by_id, load_roster
    from components.matchmaker import MatchmakerEngine

    demand = load_request_by_id(request.role_id)
    if not demand:
        raise HTTPException(status_code=404, detail=f"Role not found: {request.role_id}")

    roster = load_roster()
    candidate = _find_candidate(roster, request)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found for supplied id/name")

    demand_skill_analysis = _build_demand_skill_analysis(demand)
    engine = MatchmakerEngine()

    original = engine.match(demand_skill_analysis=demand_skill_analysis, candidates=[candidate])
    original_result = original[0].to_dict() if original else {
        "fit": "No Match",
        "score_pct": 0.0,
        "primary_match": "none",
        "matched_skills": [],
        "missing_skills": [],
        "rationale": "No match result.",
    }

    edited_profile = request.edited_skill_profile
    updated_candidate = _candidate_for_matching(candidate, edited_profile)
    updated = engine.match(demand_skill_analysis=demand_skill_analysis, candidates=[updated_candidate])
    updated_result = updated[0].to_dict() if updated else original_result

    base_skill_profile = updated_candidate.get("skill_profile") or [
        {"skill": s, "proficiency": 2, "source": "csv"} for s in (updated_candidate.get("skills") or [])
    ]

    skill_profile = _to_skill_profile_entries(base_skill_profile)
    inferred_additional = _infer_additional_skills(candidate)

    demand_view = {
        "role_id": demand.get("role_id", request.role_id),
        "role": (demand.get("staffing_intake") or {}).get("role") or demand.get("role_title") or demand.get("role_id"),
        "primary_skill": demand_skill_analysis.get("primary_skill", ""),
        "secondary_skills": demand_skill_analysis.get("secondary_skills", []),
        "other_skills": demand_skill_analysis.get("other_skills", []),
        "career_level": demand_skill_analysis.get("accenture_level", ""),
        "capability": demand_skill_analysis.get("demand_capability", ""),
    }

    return WhatIfMatchResponse(
        role_id=request.role_id,
        candidate_id=str(candidate.get("candidate_id") or "") or None,
        candidate_name=str(candidate.get("name") or ""),
        candidate_role=str(candidate.get("role") or ""),
        demand=demand_view,
        original_result=original_result,
        updated_result=updated_result,
        skill_profile=skill_profile,
        inferred_additional_skills=inferred_additional,
    )


@router.post("/api/report/interaction-feedback", response_model=InteractionFeedbackResponse, dependencies=[Depends(require_api_auth)])
async def submit_interaction_feedback(request: InteractionFeedbackRequest) -> InteractionFeedbackResponse:
    """Persist dashboard interaction logs for future learning and audit."""
    from observability.feedback import record_interaction_feedback

    payload = {
        "schema_version": "1.0",
        "session_id": request.session_id,
        "role_id": request.role_id,
        "candidate_id": request.candidate_id,
        "candidate_name": request.candidate_name,
        "candidate_role": request.candidate_role,
        "rating": request.rating,
        "comment": request.comment,
        "demand": request.demand,
        "original_result": request.original_result,
        "updated_result": request.updated_result,
        "edited_skill_profile": [e.model_dump() for e in request.edited_skill_profile],
        "inferred_additional_skills": [e.model_dump() for e in request.inferred_additional_skills],
    }
    feedback_id = await record_interaction_feedback(payload)
    return InteractionFeedbackResponse(status="recorded", feedback_id=feedback_id)


@router.post("/api/report/cache/clear", tags=["reports"])
async def clear_report_cache() -> dict:
    """Invalidate the scoring cache (L1 + L2) so the next request recomputes fresh scores."""
    l1_count = len(_cache)
    _cache.clear()
    l2_count = 0
    try:
        from app.agents.submit_demand.tools.cosmos_store import delete_report_cache_all
        l2_count = delete_report_cache_all()
    except Exception as exc:
        _logger.warning("[report_cache] Cosmos clear error (non-fatal): %s", exc)
    _logger.info("[bench_matches_report] Cache cleared (L1=%d L2=%d)", l1_count, l2_count)
    return {"cleared_l1": l1_count, "cleared_l2": l2_count, "status": "ok"}


# ---------------------------------------------------------------------------
# AI Analyze — Azure gpt-4.1 match assessment without JSON config files
# Uses the same mathematical weights as the rule-based engine for comparison.
# ---------------------------------------------------------------------------

def _ai_compute_score(primary_result: str, covered_sec: float, covered_oth: float,
                      n_sec: int, n_oth: int) -> tuple[float, str]:
    """Apply identical weight formula as matchmaker.py to Gemini-assessed coverage."""
    W_PRI, W_SEC, W_OTH = 2.0, 1.5, 1.0
    SINGLE_CAP = 0.75
    primary_bonus = 1.0 if primary_result in ("exact", "equivalent") else 0.0
    numerator   = W_PRI * primary_bonus + W_SEC * covered_sec + W_OTH * covered_oth
    denominator = W_PRI + W_SEC * n_sec + W_OTH * n_oth
    score = (numerator / denominator) if denominator > 0 else 0.0
    if (n_sec + n_oth) == 0:
        score = min(score, SINGLE_CAP)
    score_pct = round(score * 100, 1)
    # Classify using same thresholds
    if primary_result == "exact":
        fit = "Excellent" if score >= 0.80 else ("Good" if score >= 0.50 else ("Regular" if score >= 0.40 else "No Match"))
    elif primary_result == "equivalent":
        fit = "Good" if score >= 0.60 else ("Regular" if score >= 0.40 else "No Match")
    else:
        fit = "Regular" if score >= 0.40 else "No Match"
    return score_pct, fit


async def _call_gemini_analyze(demand_sa: dict, candidate: dict) -> dict:
    """Call Azure gpt-4.1 to assess skill match WITHOUT using any JSON config files."""
    import asyncio, json as _json, os
    from openai import AzureOpenAI

    primary    = demand_sa.get("primary_skill") or ""
    secondary  = demand_sa.get("secondary_skills") or []
    other      = demand_sa.get("other_skills") or []

    profile = candidate.get("skill_profile") or []
    skill_lines = "\n".join(
        f"- {e['skill']} (P{e.get('proficiency', 2)})"
        for e in profile if isinstance(e, dict) and e.get("skill")
    ) or "\n".join(f"- {s}" for s in (candidate.get("skills") or []))

    sec_str = ", ".join(secondary) if secondary else "none"
    oth_str = ", ".join(other) if other else "none"

    prompt = f"""You are a technical staffing expert. Assess how well a candidate's skills match a job demand.

Demand:
- Primary skill (required): {primary or 'not specified'}
- Secondary skills: {sec_str}
- Other skills: {oth_str}

Candidate skills:
{skill_lines}

Return ONLY valid JSON with this exact structure:
{{
  "primary": {{
    "skill": "{primary}",
    "result": "exact",
    "via": null,
    "reasoning": "..."
  }},
  "secondary": [{{"skill": "skill_name", "result": "matched", "via": null}}],
  "other": [{{"skill": "skill_name", "result": "none", "via": null}}],
  "overall_reasoning": "2-sentence assessment"
}}

Rules:
- primary.result must be "exact", "equivalent", or "none"
- secondary/other result must be "matched" or "none"
- "via" is the candidate skill name used for an equivalent match, else null
- Include ALL secondary and other skills in respective arrays
- Be strict: only "exact" when skill name clearly matches; "equivalent" when functionally similar but different name"""

    def _sync_call():
        client = AzureOpenAI(
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            azure_endpoint=os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "https://openai-interviewassists.openai.azure.com/"),
            api_key=os.environ.get("AZURE_AI_FOUNDRY_KEY", ""),
        )
        resp = client.chat.completions.create(
            model=os.environ.get("AZURE_CHAT_DEPLOYMENT", "gpt-4.1"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        return _json.loads(resp.choices[0].message.content or "{}")

    return await asyncio.to_thread(_sync_call)


@router.post("/api/report/ai-analyze", dependencies=[Depends(require_api_auth)])
async def ai_analyze_match(request: AiAnalyzeRequest) -> dict:
    """Re-score one demand × candidate using Gemini (no JSON config files).

    Returns both the rule-based result (from cache) and the AI result so the
    dashboard can show a side-by-side diff with identical math.
    """
    from app.agents.submit_demand.tools.cosmos_store import load_request_by_id, load_roster

    demand = load_request_by_id(request.role_id)
    if not demand:
        raise HTTPException(status_code=404, detail=f"Role not found: {request.role_id}")

    roster = load_roster()
    # Find candidate by employee_id, name, or name+role
    candidate = None
    if request.employee_id:
        for c in roster:
            if str(c.get("candidate_id") or c.get("employee_id") or "") == request.employee_id:
                candidate = c
                break
    if not candidate and request.candidate_name:
        nm = request.candidate_name.strip().lower()
        for c in roster:
            if str(c.get("name") or "").strip().lower() == nm:
                candidate = c
                break
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    demand_sa = _build_demand_skill_analysis(demand)

    # ── Rule-based result (same engine, no changes) ────────────────────────
    from components.matchmaker import MatchmakerEngine
    engine = MatchmakerEngine()
    rule_results = engine.match(demand_skill_analysis=demand_sa, candidates=[candidate])
    rule = rule_results[0].to_dict() if rule_results else {"fit": "No Match", "score_pct": 0.0, "primary_match": "none"}

    # ── AI-based result (Gemini, no config files) ───────────────────────────
    try:
        ai_raw = await _call_gemini_analyze(demand_sa, candidate)
    except Exception as exc:
        _logger.warning("[ai_analyze] Azure LLM call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Azure LLM call failed: {exc}")

    primary_res = (ai_raw.get("primary") or {}).get("result", "none")
    secondary   = demand_sa.get("secondary_skills") or []
    other       = demand_sa.get("other_skills") or []
    n_sec, n_oth = len(secondary), len(other)

    ai_secondary = {s.get("skill", "").lower(): s for s in (ai_raw.get("secondary") or [])}
    ai_other     = {s.get("skill", "").lower(): s for s in (ai_raw.get("other") or [])}

    covered_sec = sum(1 for s in secondary if ai_secondary.get(s.lower(), {}).get("result") == "matched")
    covered_oth = sum(1 for s in other     if ai_other.get(s.lower(),     {}).get("result") == "matched")

    ai_score_pct, ai_fit = _ai_compute_score(primary_res, covered_sec, covered_oth, n_sec, n_oth)

    # ── Build diff: skills where AI disagrees with rule-based ──────────────
    diff: list[dict] = []
    rule_primary = rule.get("primary_match", "none")
    if rule_primary != primary_res:
        diff.append({
            "tier": "primary",
            "skill": demand_sa.get("primary_skill") or "",
            "rule": rule_primary,
            "ai": primary_res,
            "ai_via": (ai_raw.get("primary") or {}).get("via"),
            "ai_reasoning": (ai_raw.get("primary") or {}).get("reasoning"),
        })

    rule_matched = {s.lower() for s in (rule.get("matched_skills") or [])}
    for s in secondary:
        ai_entry = ai_secondary.get(s.lower(), {})
        ai_matched = ai_entry.get("result") == "matched"
        rule_m = s.lower() in rule_matched
        if ai_matched != rule_m:
            diff.append({"tier": "secondary", "skill": s, "rule": "matched" if rule_m else "none",
                         "ai": "matched" if ai_matched else "none", "ai_via": ai_entry.get("via")})
    for s in other:
        ai_entry = ai_other.get(s.lower(), {})
        ai_matched = ai_entry.get("result") == "matched"
        rule_m = s.lower() in rule_matched
        if ai_matched != rule_m:
            diff.append({"tier": "other", "skill": s, "rule": "matched" if rule_m else "none",
                         "ai": "matched" if ai_matched else "none", "ai_via": ai_entry.get("via")})

    return {
        "role_id":           request.role_id,
        "candidate_name":    candidate.get("name"),
        "rule_result":       {"score_pct": rule.get("score_pct"), "fit": rule.get("fit"), "primary_match": rule_primary},
        "ai_result":         {"score_pct": ai_score_pct, "fit": ai_fit, "primary_match": primary_res,
                              "primary_via": (ai_raw.get("primary") or {}).get("via"),
                              "primary_reasoning": (ai_raw.get("primary") or {}).get("reasoning"),
                              "secondary_detail": ai_raw.get("secondary") or [],
                              "other_detail": ai_raw.get("other") or [],
                              "overall_reasoning": ai_raw.get("overall_reasoning") or ""},
        "diff":              diff,
        "demand_primary":    demand_sa.get("primary_skill") or "",
        "demand_secondary":  secondary,
        "demand_other":      other,
    }



async def bench_matches_stream(
    role_id: Annotated[str, Query()] = "",
    refresh: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    """SSE endpoint: streams scoring progress then a small \"done\" signal.

    The client fetches actual data via GET /api/report/bench-matches after
    receiving \"done\".  This avoids sending a 24MB SSE payload which causes
    browsers to drop the EventSource connection.

    Events:
      {"type":"start"}
      {"type":"progress","progress":5,"total":450}
      {"type":"done","cache_hit":true}
      {"type":"error","message":"..."}
    """
    import asyncio
    import json as _json
    import threading

    from app.agents.submit_demand.tools.matching_report_tool import (
        generate_bench_matching_report_structured,
    )

    key = _cache_key(role_id)

    async def event_stream():
        # ── Cache hit: instant done signal ────────────────────────────────────
        cached = None if refresh else _get_cached(key)
        if cached is not None:
            yield f"data: {_json.dumps({'type': 'done', 'cache_hit': True})}\n\n"
            return

        # ── Cache miss: score in background thread, stream progress ───────────
        _progress: list[dict] = []
        _error: list[str] = []
        _done = threading.Event()

        def _on_progress(current: int, total: int) -> None:
            _progress.append({"type": "progress", "progress": current, "total": total})

        def _run() -> None:
            try:
                r = generate_bench_matching_report_structured(
                    fit_filter="Excellent,Good,Regular,No Match",
                    role_id=role_id,
                    progress_callback=_on_progress,
                )
                _set_cached(key, r)
            except Exception as exc:
                _error.append(str(exc))
            finally:
                _done.set()

        yield f"data: {_json.dumps({'type': 'start'})}\n\n"
        threading.Thread(target=_run, daemon=True).start()

        while not _done.is_set():
            while _progress:
                yield f"data: {_json.dumps(_progress.pop(0))}\n\n"
            await asyncio.sleep(0.15)

        while _progress:
            yield f"data: {_json.dumps(_progress.pop(0))}\n\n"

        if _error:
            yield f"data: {_json.dumps({'type': 'error', 'message': _error[0]})}\n\n"
            return

        # Small done signal — client fetches data separately via HTTP
        yield f"data: {_json.dumps({'type': 'done', 'cache_hit': False})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )




# ── Dashboard HTML — read from file at request time (dev-reload friendly) ────
_DASHBOARD_HTML_PATH = (
    __import__("pathlib").Path(__file__).resolve().parent.parent.parent
    / "bench-matches-dashboard.html"
)



@router.get("/report/dashboard", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_html_auth)])
async def bench_matches_dashboard() -> HTMLResponse:
    """Serve the Bench Match Dashboard (reads bench-matches-dashboard.html at request time)."""
    try:
        html = _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = "<h1>Dashboard file not found</h1><p>bench-matches-dashboard.html missing from repo root.</p>"
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")
