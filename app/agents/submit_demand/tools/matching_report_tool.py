"""
app/agents/submit_demand/tools/matching_report_tool.py
-------------------------------------------------------
ADK tool: generate_bench_matching_report(fit_filter, role_id)

Produces a full bench × open-demands matching report using the deterministic
MatchmakerEngine — zero LLM tokens, two Cosmos reads.

Flow:
  1. Load full bench roster once (Cosmos read).
  2. Load all pending demands (Cosmos read).
  3. Instantiate MatchmakerEngine once (loads equivalencies from JSON config).
  4. For each pending demand: run engine.match() against the full roster.
  5. Aggregate and return a Markdown report (for the agent chat surface)
     or structured dict (for the REST endpoint).

Returns:
  str — ADK tool contract: human-readable Markdown report.

Cost:
  No LLM calls.  Two Cosmos reads regardless of roster or demand count.
"""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger(__name__)

# Fit tier constants
_VALID_FITS = {"Excellent", "Good", "Regular", "No Match"}


def generate_bench_matching_report(
    fit_filter: str = "Excellent,Good",
    role_id: str = "",
) -> str:
    """Score all bench candidates against all open (pending) demands.

    This tool runs a deterministic, zero-LLM matching pass across the entire
    bench roster × open demand list and returns a grouped Markdown report.

    Args:
        fit_filter: Comma-separated list of fit tiers to include in the report.
                    Valid values: Excellent, Good, Regular, No Match.
                    Defaults to "Excellent,Good".
        role_id:    When non-empty, restrict the report to a single demand
                    (e.g. "ROLE-003").  Ignored when empty.

    Returns:
        A formatted Markdown string with one section per demand, candidates
        grouped by fit tier and sorted by score descending.
    """
    from app.agents.submit_demand.tools.cosmos_store import load_roster, load_requests
    from components.matchmaker import MatchmakerEngine

    # ── Parse fit filter ─────────────────────────────────────────────────────
    wanted_fits = {t.strip() for t in fit_filter.split(",") if t.strip() in _VALID_FITS}
    if not wanted_fits:
        wanted_fits = {"Excellent", "Good"}

    # ── Load data (two Cosmos reads) ─────────────────────────────────────────
    roster = load_roster()
    if not roster:
        return "No candidates found on the bench roster."

    all_requests = load_requests()
    def _is_open(r: dict) -> bool:
        s = (r.get("status") or r.get("role_status") or "").strip().lower()
        return s == "pending" or s.startswith("open") or s in ("new", "active")
    demands = [r for r in all_requests if _is_open(r)]

    if role_id:
        demands = [d for d in demands if str(d.get("role_id", "")).lower() == role_id.lower()]
        if not demands:
            return f"No open/pending demand found with role_id={role_id!r}."

    if not demands:
        return "No open (pending) demands found."

    _logger.info(
        "[bench_report] Scoring %d candidates × %d demands (fits: %s)",
        len(roster), len(demands), ", ".join(sorted(wanted_fits)),
    )
    # Import skill parser for fallback path
    from app.agents.shared.skill_utils import extract_skill_name as _extract_skill_name, parse_demand_skill_column as _parse_demand_skill_column
    
    # ── Extract inferred skills before matching (batch) ─────────────────────
    # For all demands that have role_description but no inferred_skills, extract in ONE batch call
    from app.agents.submit_demand.tools.request_store import batch_extract_inferred_skills
    import asyncio
    
    # Identify role_ids that need extraction
    role_ids_needing_extraction = []
    for demand in demands:
        rid = demand.get("role_id")
        skill_analysis = demand.get("skill_analysis") or {}
        inferred = skill_analysis.get("inferred_skills") or []
        role_desc = demand.get("role_description") or ""
        
        # Only extract if: has role_description AND no inferred skills yet
        if rid and role_desc and not inferred:
            role_ids_needing_extraction.append(rid)
    
    if role_ids_needing_extraction:
        _logger.info(f"[bench_report] Batch extracting inferred skills for {len(role_ids_needing_extraction)} demands (non-blocking)")
        try:
            # Non-blocking: don't wait too long for extraction
            # If it takes >5 seconds per demand, skip and use what we have
            batch_result = asyncio.run(batch_extract_inferred_skills(role_ids_needing_extraction, batch_size=10, progress_callback=progress_callback))
            _logger.info(f"[bench_report] Batch extraction completed: {batch_result.get('processed', 0)} processed, {batch_result.get('skipped', 0)} skipped")
        except Exception as _exc:
            # Don't fail entire report if extraction fails — just warn and continue
            _logger.warning(f"[bench_report] Batch extraction failed (continuing anyway): {_exc}")
            if progress_callback:
                try:
                    progress_callback(len(role_ids_needing_extraction), len(role_ids_needing_extraction))
                except Exception:
                    pass  # Ignore callback errors
    else:
        _logger.debug("[bench_report] All demands already have inferred_skills or no role_description")
    
    # Reload demands after extraction
    demands = [r for r in load_requests() if _is_open(r)]
    if role_id:
        demands = [d for d in demands if str(d.get("role_id", "")).lower() == role_id.lower()]
    
    # ── Single MatchmakerEngine (loads skill_equivalencies.json once) ─────────
    engine = MatchmakerEngine()

    # ── Run matching ─────────────────────────────────────────────────────────
    report_sections: list[str] = []
    summary_rows: list[dict] = []  # for the summary table

    for demand in demands:
        rid      = demand.get("role_id", "?")
        intake   = demand.get("staffing_intake") or {}
        # Use role title from intake; fall back to role description snippet, then role_id
        role_txt = (
            intake.get("role")
            or demand.get("role_title")
            or rid
        )

        skill_analysis = dict(demand.get("skill_analysis") or {})

        raw_skills = intake.get("skills") or []
        top_primary = demand.get("primary_skill", "") or ""
        top_secondary_raw = demand.get("secondary_skills") or demand.get("role_secondary_skill", "")
        top_other_raw = demand.get("other_skills") or demand.get("role_other_skills", "")
        top_secondary = _parse_demand_skill_column(top_secondary_raw)
        top_other = _parse_demand_skill_column(top_other_raw)
        computed_skill_analysis = {
            "primary_skill":    (
                _extract_skill_name(raw_skills[0]) if raw_skills
                else top_primary
            ),
            "secondary_skills": (
                [_extract_skill_name(s) for s in raw_skills[1:3]] if len(raw_skills) > 1
                else top_secondary
            ),
            "other_skills":     [_extract_skill_name(s) for s in raw_skills[3:]] if len(raw_skills) > 3 else top_other,
            "inferred_skills":  skill_analysis.get("inferred_skills") or [],
            "accenture_level":  skill_analysis.get("accenture_level") or intake.get("career_level") or demand.get("career_level") or "",
            "seniority_indicator": skill_analysis.get("seniority_indicator") or "",
            "description":      demand.get("role_description") or intake.get("description") or demand.get("description") or role_txt or "",
            "role_title":       role_txt,
        }

        if skill_analysis:
            for key, value in computed_skill_analysis.items():
                if skill_analysis.get(key) in (None, "", []):
                    skill_analysis[key] = value
        else:
            skill_analysis = computed_skill_analysis

        _logger.debug(
            "[bench_report] demand payload for match | role_id=%s | role_title=%s | description_preview=%s | description_source=%s",
            rid,
            role_txt,
            (skill_analysis.get("description") or "")[:1000],
            "role_description" if skill_analysis.get("role_description") else "derived",
        )

        # Remember what was stored before matching (may have been [])
        # Normalize to list of skill names for comparison (handle both dict and string formats)
        def _normalize_inferred(items):
            return [
                (item.get("skill") if isinstance(item, dict) else str(item)).lower().strip()
                for item in (items or [])
                if item
            ]
        _stored_inferred = _normalize_inferred(skill_analysis.get("inferred_skills"))

        # Check match cache before scoring
        from app.services.match_cache import get_cached_match_results, set_cached_match_results  # noqa: PLC0415
        cached_results = get_cached_match_results(rid, demand)
        
        if cached_results:
            _logger.info(f"[bench_report] Using cached results for {rid}")
            results = cached_results
        else:
            _logger.info(f"[bench_report] Cache miss for {rid} — scoring {len(roster)} candidates")
            results = engine.match(demand_skill_analysis=skill_analysis, candidates=roster)
            # Store results in cache for future runs
            try:
                set_cached_match_results(rid, demand, results)
            except Exception as _cache_exc:
                _logger.warning(f"[bench_report] Could not cache results for {rid}: {_cache_exc}")

        # If match() inferred new skills via Azure (combined call), save them back to
        # Cosmos so subsequent runs reuse them without another LLM call.
        _new_inferred = skill_analysis.get("inferred_skills") or []
        _new_inferred_normalized = _normalize_inferred(_new_inferred)
        # Save if inferred skills were computed during matching (changed from stored value)
        if _new_inferred_normalized != _stored_inferred:
            try:
                from app.agents.submit_demand.tools.cosmos_store import load_request_by_id as _lr, upsert_request as _cu  # noqa: PLC0415
                _doc = _lr(rid)
                if _doc is not None:
                    if "skill_analysis" not in _doc:
                        _doc["skill_analysis"] = {}
                    _doc["skill_analysis"]["inferred_skills"] = _new_inferred
                    _cu(_doc)
                    _logger.info(
                        "[bench_report] Saved %d inferred skills back to Cosmos for %s",
                        len(_new_inferred), rid,
                    )
            except Exception as _exc:
                _logger.warning("[bench_report] Could not save inferred_skills to Cosmos for %s: %s", rid, _exc)

        # Filter to wanted fits
        filtered = [r for r in results if r.fit in wanted_fits]

        # Summary row
        summary_rows.append({
            "role_id":    rid,
            "role":       role_txt,
            "excellent":  sum(1 for r in results if r.fit == "Excellent"),
            "good":       sum(1 for r in results if r.fit == "Good"),
            "regular":    sum(1 for r in results if r.fit == "Regular"),
            "no_match":   sum(1 for r in results if r.fit == "No Match"),
        })

        section = _format_demand_section(
            role_id=rid,
            role_text=role_txt,
            skill_analysis=skill_analysis,
            intake=intake,
            results=filtered,
        )
        report_sections.append(section)

    # ── Assemble full report ─────────────────────────────────────────────────
    header = _build_header(len(roster), len(demands), wanted_fits, summary_rows)
    body   = "\n\n".join(report_sections)

    # GAP-054/019: flag candidates with zero matches across all demands
    unmatchable = _find_unmatchable_candidates(roster, demands, engine)
    unmatchable_section = _format_unmatchable(unmatchable) if unmatchable else ""

    return f"{header}\n\n{body}" + (f"\n\n{unmatchable_section}" if unmatchable_section else "")


# ---------------------------------------------------------------------------
# Structured output (for REST endpoint, no Markdown)
# ---------------------------------------------------------------------------

def generate_bench_matching_report_structured(
    fit_filter: str = "Excellent,Good",
    role_id: str = "",
    progress_callback=None,
) -> dict:
    """Same logic as generate_bench_matching_report but returns a structured dict.

    Intended for the REST endpoint GET /api/report/bench-matches.
    """
    from app.agents.submit_demand.tools.cosmos_store import load_roster, load_requests
    from components.matchmaker import MatchmakerEngine
    from app.agents.shared.skill_utils import extract_skill_name as _extract_skill_name_s
    import asyncio

    wanted_fits = {t.strip() for t in fit_filter.split(",") if t.strip() in _VALID_FITS}
    if not wanted_fits:
        wanted_fits = {"Excellent", "Good"}

    # Include all requested fits at the candidate level (including "No Match")
    # The "include_no_match_demands" flag handles whether to show demands with zero E/G/R candidates
    candidate_fits = wanted_fits  # Don't exclude "No Match" — include at candidate level
    include_no_match_demands = "No Match" in wanted_fits or wanted_fits == {"No Match"}

    roster = load_roster()
    all_requests = load_requests()

    def _is_open_s(r: dict) -> bool:
        s = (r.get("status") or r.get("role_status") or "").strip().lower()
        return s == "pending" or s.startswith("open") or s in ("new", "active")
    demands = [r for r in all_requests if _is_open_s(r)]

    if role_id:
        demands = [d for d in demands if d.get("role_id") == role_id]

    # ── Extract inferred skills before matching (batch) ─────────────────────
    # For all demands that have role_description but no inferred_skills, extract in ONE batch call
    from app.agents.submit_demand.tools.request_store import batch_extract_inferred_skills
    
    # Identify role_ids that need extraction
    role_ids_needing_extraction = []
    for demand in demands:
        rid = demand.get("role_id")
        skill_analysis = demand.get("skill_analysis") or {}
        inferred = skill_analysis.get("inferred_skills") or []
        role_desc = demand.get("role_description") or ""
        
        # Only extract if: has role_description AND no inferred skills yet
        if rid and role_desc and not inferred:
            role_ids_needing_extraction.append(rid)
    
    if role_ids_needing_extraction:
        _logger.info(f"[bench_report_structured] Batch extracting inferred skills for {len(role_ids_needing_extraction)} demands (non-blocking)")
        try:
            # Non-blocking: don't wait too long for extraction
            # If it takes >5 seconds per demand, skip and use what we have
            batch_result = asyncio.run(batch_extract_inferred_skills(role_ids_needing_extraction, batch_size=10, progress_callback=progress_callback))
            _logger.info(f"[bench_report_structured] Batch extraction completed: {batch_result.get('processed', 0)} processed, {batch_result.get('skipped', 0)} skipped")
        except Exception as _exc:
            # Don't fail entire report if extraction fails — just warn and continue
            _logger.warning(f"[bench_report_structured] Batch extraction failed (continuing anyway): {_exc}")
            if progress_callback:
                try:
                    progress_callback(len(role_ids_needing_extraction), len(role_ids_needing_extraction))
                except Exception:
                    pass  # Ignore callback errors
    else:
        _logger.debug("[bench_report_structured] All demands already have inferred_skills or no role_description")
    
    # Reload demands after extraction
    all_requests = load_requests()
    demands = [r for r in all_requests if _is_open_s(r)]
    if role_id:
        demands = [d for d in demands if d.get("role_id") == role_id]

    engine = MatchmakerEngine()
    output_demands = []
    _n_demands = len(demands)

    for idx, demand in enumerate(demands):
        rid    = demand.get("role_id", "?")
        intake = demand.get("staffing_intake") or {}
        role_txt = (
            intake.get("role")
            or demand.get("role_title")
            or rid
        )
        
        # Log first demand to inspect what fields are actually present
        if idx == 0:
            _logger.info(f"[DEBUG] First demand keys: {list(demand.keys())}")
            _logger.info(f"[DEBUG] First intake keys: {list(intake.keys())}")
            _logger.info(f"[DEBUG] career_level_from={demand.get('career_level_from')} or intake={intake.get('career_level_from')}")
            _logger.info(f"[DEBUG] employee_id={demand.get('employee_id')} or intake={intake.get('employee_id')}")
            _logger.info(f"[DEBUG] practice={demand.get('practice')} or intake={intake.get('practice')}")
        skill_analysis = demand.get("skill_analysis") or {}

        if not skill_analysis or not skill_analysis.get("primary_skill"):
            raw_skills = intake.get("skills") or []
            top_primary = demand.get("primary_skill", "") or ""
            top_secondary_raw = demand.get("secondary_skills") or demand.get("role_secondary_skill", "")
            top_secondary = (
                top_secondary_raw if isinstance(top_secondary_raw, list)
                else [s.strip() for s in str(top_secondary_raw).split(",") if s.strip()]
            ) if top_secondary_raw else []
            # Preserve any inferred_skills already stored in skill_analysis (from AI extraction
            # at upload time) — don't overwrite with empty list when rebuilding from intake.
            stored_inferred = skill_analysis.get("inferred_skills") or []
            skill_analysis = {
                **skill_analysis,  # keep accenture_level, ai_source, demand_industry, etc.
                "primary_skill":    (
                    _extract_skill_name_s(raw_skills[0]) if raw_skills
                    else top_primary
                ),
                "secondary_skills": (
                    [_extract_skill_name_s(s) for s in raw_skills[1:3]] if len(raw_skills) > 1
                    else top_secondary
                ),
                "other_skills":     [_extract_skill_name_s(s) for s in raw_skills[3:]] if len(raw_skills) > 3 else [],
                "inferred_skills":  stored_inferred,
                "accenture_level":  intake.get("career_level") or demand.get("career_level") or skill_analysis.get("accenture_level") or "",
                "seniority_indicator": "",
            }

        # AI skill map and inferred skills are pre-computed at upload time
        # (compute_ai_skill_map in manage.py) and stored in the demand doc.
        # No AI calls needed here — the matchmaker reads ai_skill_map directly.

        # Remember what was stored before matching (may have been [])
        # Normalize to list of skill names for comparison (handle both dict and string formats)
        def _normalize_inferred_s(items):
            return [
                (item.get("skill") if isinstance(item, dict) else str(item)).lower().strip()
                for item in (items or [])
                if item
            ]
        _stored_inferred_s = _normalize_inferred_s(skill_analysis.get("inferred_skills"))

        # Check match cache before scoring
        from app.services.match_cache import get_cached_match_results, set_cached_match_results  # noqa: PLC0415
        cached_results = get_cached_match_results(rid, demand)
        
        if cached_results:
            _logger.info(f"[bench_report_structured] Using cached results for {rid}")
            results = cached_results
        else:
            _logger.info(f"[bench_report_structured] Cache miss for {rid} — scoring {len(roster)} candidates")
            results = engine.match(demand_skill_analysis=skill_analysis, candidates=roster)
            # Store results in cache for future runs
            try:
                set_cached_match_results(rid, demand, results)
            except Exception as _cache_exc:
                _logger.warning(f"[bench_report_structured] Could not cache results for {rid}: {_cache_exc}")
        
        # If match() inferred new skills via Azure (combined call), save them back to
        # Cosmos so subsequent runs reuse them without another LLM call.
        _new_inferred_s = skill_analysis.get("inferred_skills") or []
        _new_inferred_normalized_s = _normalize_inferred_s(_new_inferred_s)
        # Save if inferred skills were computed during matching (changed from stored value)
        if _new_inferred_normalized_s != _stored_inferred_s:
            try:
                from app.agents.submit_demand.tools.cosmos_store import load_request_by_id as _lr_s, upsert_request as _cu_s  # noqa: PLC0415
                _doc_s = _lr_s(rid)
                if _doc_s is not None:
                    if "skill_analysis" not in _doc_s:
                        _doc_s["skill_analysis"] = {}
                    _doc_s["skill_analysis"]["inferred_skills"] = _new_inferred_s
                    _cu_s(_doc_s)
                    _logger.info(
                        "[bench_report_structured] Saved %d inferred skills back to Cosmos for %s",
                        len(_new_inferred_s), rid,
                    )
            except Exception as _exc_s:
                _logger.warning("[bench_report_structured] Could not save inferred_skills to Cosmos for %s: %s", rid, _exc_s)
        
        if progress_callback:
            progress_callback(idx + 1, _n_demands)

        # Always check whether any E/G/R match exists (needed for the No Match gate)
        _EGER = {"Excellent", "Good", "Regular"}
        has_eger = any(r.fit in _EGER for r in results)
        filtered = [r for r in results if r.fit in candidate_fits]

        # Inclusion rules:
        #   1. Has E/G/R candidates in the requested tiers → always include
        #   2. No E/G/R candidates at all AND "No Match" was requested → include with empty list
        #   3. Everything else → skip
        if not filtered:
            if include_no_match_demands and not has_eger:
                pass   # truly unmatched demand — include with candidates: []
            else:
                continue

        # GAP-005: flag low-specificity demands (≤2 skills total, no inferred)
        total_skills = (
            (1 if skill_analysis.get("primary_skill") else 0)
            + len(skill_analysis.get("secondary_skills") or [])
            + len(skill_analysis.get("other_skills") or [])
            + len(skill_analysis.get("inferred_skills") or [])
        )
        is_low_specificity = total_skills <= 2

        # GAP-022: flag "Copy" roles (multiple headcount for same role)
        is_copy = bool(re.search(r"\bcopy\b", role_txt, re.IGNORECASE))

        # GAP-004: flag "Full Stack" title mismatch — role title implies full-stack
        # but primary skill is backend-only (no frontend component listed at all).
        _FULLSTACK_TITLE_RE = re.compile(r"\bfull.?stack\b", re.IGNORECASE)
        _FRONTEND_SKILLS = {"react", "angular", "vue", "typescript", "javascript",
                            "html", "css", "frontend", "front-end", "ionic", "rxjs"}

        def _norm(s: object) -> str:
            if isinstance(s, dict):
                return s.get("name", "").strip().lower()
            return (s if isinstance(s, str) else "").strip().lower()
        all_skill_names = {
            _norm(s)
            for bucket in ("secondary_skills", "other_skills", "inferred_skills")
            for s in (skill_analysis.get(bucket) or [])
        } | {_norm(skill_analysis.get("primary_skill") or "")}
        is_title_skill_mismatch = (
            bool(_FULLSTACK_TITLE_RE.search(role_txt))
            and not any(fs in " ".join(all_skill_names) for fs in _FRONTEND_SKILLS)
        )

        output_demands.append({
            "role_id":   rid,
            "role":      role_txt,
            "location":  intake.get("location") or intake.get("location_type") or "",
            "career_level": skill_analysis.get("accenture_level") or "",
            "career_level_from": intake.get("career_level_from") or demand.get("career_level_from") or "",
            "career_level_to": intake.get("career_level_to") or demand.get("career_level_to") or "",
            "employee_id": intake.get("employee_id") or demand.get("employee_id") or "",
            "practice": intake.get("practice") or demand.get("practice") or "",
            "primary_contact": intake.get("primary_contact") or intake.get("contact_email") or demand.get("primary_contact") or "",
            "industry_level_1": intake.get("industry_level_1") or demand.get("industry_level_1") or intake.get("industry") or "",
            "primary_skill": skill_analysis.get("primary_skill") or "",
            "secondary_skills": skill_analysis.get("secondary_skills") or [],
            "other_skills": skill_analysis.get("other_skills") or [],
            "inferred_skills": skill_analysis.get("inferred_skills") or [],
            "industry": skill_analysis.get("demand_industry") or intake.get("industry") or "",
            "client":    intake.get("client") or "",
            "priority":  intake.get("priority") or "",
            "is_overdue": intake.get("is_overdue") or False,
            "demand_capability": skill_analysis.get("demand_capability") or "",
            "low_specificity": is_low_specificity,
            "is_copy": is_copy,
            "is_title_skill_mismatch": is_title_skill_mismatch,
            "candidates": [r.to_dict() for r in filtered],
            "totals": {
                "excellent": sum(1 for r in results if r.fit == "Excellent"),
                "good":      sum(1 for r in results if r.fit == "Good"),
                "regular":   sum(1 for r in results if r.fit == "Regular"),
                "no_match":  sum(1 for r in results if r.fit == "No Match"),
            },
        })

    # ── GAP-019/054: Unmatchable candidates (zero matches across all demands) ──
    all_matched_ids: set[str] = set()
    for d in output_demands:
        for cand in d["candidates"]:
            cid = cand.get("employee_id") or cand.get("name", "")
            if cid:
                all_matched_ids.add(cid)

    unmatchable = []
    for c in roster:
        cid = c.get("employee_id") or c.get("name", "")
        if cid and cid not in all_matched_ids:
            unmatchable.append({
                "name":            c.get("name"),
                "role":            c.get("role"),
                "accenture_level": c.get("accenture_level"),
                "capability":      c.get("capability"),
                "skills":          (c.get("skills") or [])[:5],
            })

    # ── GAP-021/052: Over-allocation — candidates appearing in many demands ──
    # Build a lookup from employee_id → full candidate doc for name enrichment
    roster_by_id: dict[str, dict] = {
        (c.get("employee_id") or c.get("name", "")): c
        for c in roster
    }

    candidate_demand_counts: dict[str, int] = {}
    for d in output_demands:
        for cand in d["candidates"]:
            cid = cand.get("employee_id") or cand.get("name", "")
            if cid:
                candidate_demand_counts[cid] = candidate_demand_counts.get(cid, 0) + 1

    over_allocated = []
    for cid, cnt in sorted(candidate_demand_counts.items(), key=lambda x: -x[1]):
        if cnt < 3:
            continue
        doc = roster_by_id.get(cid, {})
        over_allocated.append({
            "employee_id":  cid,
            "name":          doc.get("name") or cid,
            "role":          doc.get("role") or "",
            "capability":    doc.get("capability") or "",
            "demand_count":  cnt,
        })

    return {
        "bench_size":       len(roster),
        "demands_scored":   len(demands),
        "fit_filter":       sorted(wanted_fits),
        "demands":          output_demands,
        "unmatchable_candidates": unmatchable,         # GAP-019/054
        "over_allocated_candidates": over_allocated,  # GAP-021/052
    }


# ---------------------------------------------------------------------------
# Private formatting helpers
# ---------------------------------------------------------------------------

def _build_header(
    bench_size: int,
    demand_count: int,
    wanted_fits: set[str],
    summary_rows: list[dict],
) -> str:
    lines = [
        "# Bench × Demand Matching Report",
        "",
        f"**Bench size:** {bench_size} candidates  |  "
        f"**Open demands:** {demand_count}  |  "
        f"**Showing:** {', '.join(sorted(wanted_fits))}",
        "",
        "## Summary",
        "",
        "| Role ID | Role | Excellent | Good | Regular | No Match |",
        "|---------|------|-----------|------|---------|----------|",
    ]
    for row in summary_rows:
        lines.append(
            f"| {row['role_id']} | {row['role']} | "
            f"{row['excellent']} | {row['good']} | {row['regular']} | {row['no_match']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_demand_section(
    role_id: str,
    role_text: str,
    skill_analysis: dict,
    intake: dict,
    results: list,
) -> str:
    primary   = skill_analysis.get("primary_skill") or "(not specified)"
    demand_cl = skill_analysis.get("accenture_level") or "?"
    location  = intake.get("location") or "Any"

    lines = [
        "---",
        f"## {role_id}: {role_text}",
        f"**Primary skill:** {primary}  |  **Level:** {demand_cl}  |  **Location:** {location}",
        "",
    ]

    if not results:
        lines.append("_No candidates matched the selected fit tiers._")
        return "\n".join(lines)

    tiers: dict[str, list] = {"Excellent": [], "Good": [], "Regular": []}
    for r in results:
        if r.fit in tiers:
            tiers[r.fit].append(r)

    for tier, cands in tiers.items():
        if not cands:
            continue
        lines.append(f"### {tier} ({len(cands)})")
        for r in cands:
            c     = r.candidate
            name  = c.get("name", "Unknown")
            role  = c.get("role", "?")
            cl    = c.get("accenture_level") or "?"
            exp   = c.get("experience_years") or "?"
            avail = c.get("availability") or "Unknown"
            matched = ", ".join(r.matched_skills) if r.matched_skills else "—"
            missing = ", ".join(r.missing_skills) if r.missing_skills else "None"
            lines += [
                f"- **{name}** — {role} | {cl} | {exp}y exp | {avail}",
                f"  - Score: **{r.score_pct:.1f}%**  |  Primary match: {r.primary_match}",
                f"  - Matched: {matched}",
                f"  - Missing: {missing}",
                f"  - _{r.rationale}_",
            ]
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GAP-054/019: Unmatchable candidate detection
# ---------------------------------------------------------------------------

def _find_unmatchable_candidates(
    roster: list[dict],
    demands: list[dict],
    engine,
) -> list[dict]:
    """Return candidates that scored 'No Match' on every open demand.

    Provides visibility into the 33-39% of bench that produces zero output (GAP-054).
    """
    # Build per-candidate match counts
    candidate_matched: dict[str, bool] = {
        c.get("employee_id") or c.get("name", f"idx-{i}"): False
        for i, c in enumerate(roster)
    }

    for demand in demands:
        intake = demand.get("staffing_intake") or {}
        skill_analysis = demand.get("skill_analysis") or {}
        if not skill_analysis or not skill_analysis.get("primary_skill"):
            raw_skills = intake.get("skills") or []
            skill_analysis = {
                "primary_skill":    raw_skills[0] if raw_skills else "",
                "secondary_skills": raw_skills[1:3] if len(raw_skills) > 1 else [],
                "other_skills":     raw_skills[3:] if len(raw_skills) > 3 else [],
                "inferred_skills":  [],
                "accenture_level":  intake.get("career_level") or "",
            }

        results = engine.match(demand_skill_analysis=skill_analysis, candidates=roster)
        
        # Cache results if possible
        try:
            from app.services.match_cache import set_cached_match_results  # noqa: PLC0415
            set_cached_match_results(demand.get("role_id", ""), demand, results)
        except Exception:
            pass  # Cache is optional in this helper function
        for r in results:
            if r.fit != "No Match":
                cid = r.candidate.get("employee_id") or r.candidate.get("name", "")
                if cid in candidate_matched:
                    candidate_matched[cid] = True

    unmatchable = []
    for i, cand in enumerate(roster):
        cid = cand.get("employee_id") or cand.get("name", f"idx-{i}")
        if not candidate_matched.get(cid, True):
            unmatchable.append(cand)
    return unmatchable


def _format_unmatchable(candidates: list[dict]) -> str:
    if not candidates:
        return ""
    lines = [
        "---",
        f"## ⚠ Unmatchable Candidates ({len(candidates)})",
        "",
        "These candidates produced **zero matches** across all open demands.",
        "Likely causes: career level out of range for current demand, or insufficient skill coverage.",
        "",
        "| Name | Role | Level | Skills |",
        "|------|------|-------|--------|",
    ]
    for c in candidates:
        skills = ", ".join((c.get("skills") or [])[:4])
        lines.append(
            f"| {c.get('name','?')} | {c.get('role','?')} | "
            f"{c.get('accenture_level','?')} | {skills} |"
        )
    lines.append("")
    return "\n".join(lines)
