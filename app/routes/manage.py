"""
app/routes/manage.py
--------------------
REST CRUD + HTML management pages for Bench Roster and Demand list.

REST APIs:
  GET    /api/bench                      — paginated bench list (search, page, page_size)
  PUT    /api/bench/{doc_id}             — patch a bench candidate
  DELETE /api/bench/{doc_id}?name=...    — delete a bench candidate

  GET    /api/demands                    — paginated demand list (search, page, page_size)
  PUT    /api/demands/{role_id}          — patch a demand
  DELETE /api/demands/{role_id}          — delete a demand

HTML pages:
  GET  /                   — landing page (nav to Dashboard / Manage Bench / Manage Demand)
  GET  /manage/bench       — bench management UI
  GET  /manage/demand      — demand management UI
"""

from __future__ import annotations

import csv
import io
import logging
import math
import secrets
import time
import threading
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse

from app.auth import require_api_auth, require_html_auth


def _bust_report_cache(wait_for_l2: bool = False) -> None:
    """Clear the bench-match scoring cache (L1 in-process + L2 Blob) so the next dashboard load recomputes.
    
    Args:
        wait_for_l2: If True, block until L2 cache is cleared (for immediate consistency after CSV upload).
                     If False, clear L2 in background (don't block request).
    """
    # L1 — in-process dict
    try:
        from app.routes.report import _cache
        _cache.clear()
    except Exception:
        pass
    
    # L2 — Blob Storage (sync functions)
    def _clear_l2() -> None:
        try:
            from app.agents.submit_demand.tools.cosmos_store import delete_report_cache_all
            delete_report_cache_all()
            _logger.info("[cache-invalidation] L2 report cache cleared")
        except Exception as exc:
            _logger.warning("[cache-invalidation] L2 clear failed: %s", exc)
    
    if wait_for_l2:
        # Synchronous: wait for L2 clear (used after CSV upload)
        _clear_l2()
    else:
        # Asynchronous: clear in background thread (don't block request)
        threading.Thread(target=_clear_l2, daemon=True).start()


def _patch_cache_for_candidates(changed_entries: list[dict]) -> None:
    """Incrementally update the report cache for newly added/changed bench candidates.

    Instead of discarding the entire scored result, scores only the changed
    candidates against every cached demand and patches their candidates lists.
    This is O(demands × changed) rather than O(demands × full_roster).
    Falls back to a full cache clear if anything goes wrong.
    """
    if not changed_entries:
        return

    def _do_patch() -> None:
        try:
            import copy
            from app.routes.report import _cache, _cache_key
            from app.agents.submit_demand.tools.cosmos_store import (
                load_report_cache, save_report_cache, load_requests,
            )
            from components.matchmaker import MatchmakerEngine
            from app.agents.shared.skill_utils import extract_skill_name as _xskill

            # Use the same cache key format as report.py: "v2:__all__"
            cache_key = _cache_key("")  # Returns "v2:__all__"

            # ── Load existing cache (L1 → L2) ───────────────────────────
            full_result = _cache.get(cache_key)
            if full_result is None:
                full_result = load_report_cache(cache_key)
            if full_result is None:
                _logger.info("[cache-patch] no existing cache to patch, skipping")
                return  # fresh load will score from scratch

            # ── Load demands from Cosmos for skill_analysis ─────────────
            all_requests = load_requests()
            demands_by_role_id = {str(d.get("role_id", "")): d for d in all_requests}

            engine = MatchmakerEngine()
            patched = copy.deepcopy(full_result)

            for demand_dict in patched.get("demands", []):
                role_id = str(demand_dict.get("role_id", ""))
                full_demand = demands_by_role_id.get(role_id)
                if full_demand is None:
                    continue

                # Reconstruct skill_analysis (same fallback logic as matching_report_tool)
                sa = full_demand.get("skill_analysis") or {}
                if not sa or not sa.get("primary_skill"):
                    intake = full_demand.get("staffing_intake") or {}
                    raw_skills = intake.get("skills") or []
                    top_primary = full_demand.get("primary_skill", "") or ""
                    sa = {
                        "primary_skill": (
                            _xskill(raw_skills[0]) if raw_skills else top_primary
                        ),
                        "secondary_skills": (
                            [_xskill(s) for s in raw_skills[1:3]] if len(raw_skills) > 1 else []
                        ),
                        "other_skills": (
                            [_xskill(s) for s in raw_skills[3:]] if len(raw_skills) > 3 else []
                        ),
                        "inferred_skills": [],
                        "accenture_level": (
                            intake.get("career_level") or full_demand.get("career_level") or ""
                        ),
                    }

                # Score only the changed candidates against this demand
                new_results = engine.match(demand_skill_analysis=sa, candidates=changed_entries)

                existing = demand_dict.get("candidates") or []
                idx_by_name = {c.get("name", ""): i for i, c in enumerate(existing)}

                for mr in new_results:
                    cd = mr.to_dict()
                    cname = cd.get("name", "")
                    if cname in idx_by_name:
                        existing[idx_by_name[cname]] = cd   # update in-place
                    else:
                        existing.append(cd)                  # new candidate

                existing.sort(key=lambda c: c.get("score_pct", 0.0), reverse=True)
                demand_dict["candidates"] = existing

            # ── Save patched result back to L1 + L2 ─────────────────────
            _cache[cache_key] = patched
            save_report_cache(cache_key, patched)
            _logger.info(
                "[cache-patch] patched %d candidate(s) across %d demands",
                len(changed_entries), len(patched.get("demands", [])),
            )

        except Exception as exc:
            _logger.warning("[cache-patch] failed (%s) — falling back to full cache clear", exc)
            _bust_report_cache(wait_for_l2=True)  # Synchronous: wait for L2 to clear

    threading.Thread(target=_do_patch, daemon=True).start()

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["manage"])

# ── REST: Bench Roster ────────────────────────────────────────────────────────

@router.get("/api/bench", dependencies=[Depends(require_api_auth)])
async def list_bench(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    search: Annotated[str, Query(description="Search name, role, skills, capability")] = "",
) -> dict:
    """Return a paginated, searchable list of bench roster candidates."""
    from app.agents.submit_demand.tools.cosmos_store import load_roster, _make_candidate_id

    roster = load_roster()
    roster.sort(key=lambda c: (c.get("name") or "").lower())

    if search:
        q = search.lower()
        roster = [
            c for c in roster
            if q in (c.get("name") or "").lower()
            or q in (c.get("role") or "").lower()
            or q in (c.get("capability") or "").lower()
            or any(q in s.lower() for s in c.get("skills", []))
        ]

    total = len(roster)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    # Re-attach the Cosmos doc `id` (stripped by load_roster) so the UI can use
    # it for PUT /api/bench/{doc_id} and DELETE /api/bench/{doc_id} calls.
    items = [
        {**c, "id": _make_candidate_id(c.get("name", ""), c.get("role", ""))}
        for c in roster[start: start + page_size]
    ]

    return {
        "items": items,
        "pagination": {"total": total, "page": page, "page_size": page_size, "total_pages": total_pages},
    }


@router.put("/api/bench/{doc_id}", dependencies=[Depends(require_api_auth)])
async def update_bench_candidate(doc_id: str, body: dict) -> dict:
    """Merge-patch a bench roster candidate."""
    from app.agents.submit_demand.tools.cosmos_store import patch_candidate

    name = body.get("name", "")
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required in body")

    # If skills field provided as comma-string, convert to list
    if isinstance(body.get("skills"), str):
        body["skills"] = [s.strip() for s in body["skills"].split(",") if s.strip()]

    updated = patch_candidate(doc_id, name, body)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Candidate doc '{doc_id}' not found")
    _bust_report_cache()
    return {"status": "updated", "item": updated}


@router.delete("/api/bench/all", dependencies=[Depends(require_api_auth)])
async def delete_all_bench() -> dict:
    """Delete ALL bench roster candidates. Use with caution — this cannot be undone."""
    from app.agents.submit_demand.tools.cosmos_store import delete_all_candidates

    count = delete_all_candidates()
    _bust_report_cache()
    _logger.info("Deleted all bench candidates: %d documents removed", count)
    return {"deleted": count, "collection": "bench_roster"}


@router.delete("/api/bench/{doc_id}", dependencies=[Depends(require_api_auth)])
async def delete_bench_candidate(
    doc_id: str,
    name: Annotated[str, Query(description="Candidate name (Cosmos partition key)")],
) -> dict:
    """Delete a bench roster candidate."""
    from app.agents.submit_demand.tools.cosmos_store import delete_candidate

    if not delete_candidate(doc_id, name):
        raise HTTPException(status_code=404, detail=f"Candidate '{doc_id}' not found")
    _bust_report_cache()
    return {"deleted": True, "id": doc_id}


@router.delete("/api/demands/all", dependencies=[Depends(require_api_auth)])
async def delete_all_demands() -> dict:
    """Delete ALL staffing demand requests. Use with caution — this cannot be undone."""
    from app.agents.submit_demand.tools.cosmos_store import delete_all_requests

    count = delete_all_requests()
    _bust_report_cache()
    _logger.info("Deleted all demands: %d documents removed", count)
    return {"deleted": count, "collection": "staffing_requests"}


# ── REST: Demand List ─────────────────────────────────────────────────────────

@router.get("/api/demands", dependencies=[Depends(require_api_auth)])
async def list_demands(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    search: Annotated[str, Query(description="Search role_id, title, client, skill")] = "",
) -> dict:
    """Return a paginated, searchable list of staffing demand requests."""
    from app.agents.submit_demand.tools.cosmos_store import load_requests

    requests = load_requests()
    requests.sort(key=lambda r: r.get("role_id", ""), reverse=True)

    if search:
        q = search.lower()
        requests = [
            r for r in requests
            if q in (r.get("role_id") or "").lower()
            or q in (r.get("role_title") or r.get("role") or "").lower()
            or q in (r.get("client") or "").lower()
            or q in (r.get("primary_skill") or "").lower()
        ]

    total = len(requests)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    items = requests[start: start + page_size]

    return {
        "items": items,
        "pagination": {"total": total, "page": page, "page_size": page_size, "total_pages": total_pages},
    }


@router.put("/api/demands/{role_id}", dependencies=[Depends(require_api_auth)])
async def update_demand(role_id: str, body: dict) -> dict:
    """Merge-patch a staffing demand request."""
    from app.agents.submit_demand.tools.cosmos_store import patch_request

    updated = patch_request(role_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Demand '{role_id}' not found")
    _bust_report_cache()
    return {"status": "updated", "item": updated}


@router.delete("/api/demands/{role_id}", dependencies=[Depends(require_api_auth)])
async def delete_demand_item(role_id: str) -> dict:
    """Delete a staffing demand request."""
    from app.agents.submit_demand.tools.cosmos_store import delete_request

    if not delete_request(role_id):
        raise HTTPException(status_code=404, detail=f"Demand '{role_id}' not found")
    _bust_report_cache()
    return {"deleted": True, "role_id": role_id}


# ── REST: CSV Upload (background jobs with persistent tracking) ──────────────
# Uploads are processed in a background thread so the HTTP response returns
# immediately (avoiding Azure Container Apps' 60-second gateway timeout).
# Job state is persisted in Cosmos DB so uploads survive pod restarts.
# The client polls GET /api/upload-jobs/{job_id} until status == "done".

from app.services.job_service import UploadJobService


@router.get("/api/upload-jobs/{job_id}")
async def get_upload_job(job_id: str) -> dict:
    """Poll upload job status. No auth required — job_id is a cryptographically
    random token, so possession proves authorization.
    Returns {status, result, error, progress, total, can_pause, can_resume, can_stop}."""
    job = UploadJobService.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    
    # Map DB format to API response
    return {
        "job_id": job.get("job_id"),
        "upload_type": job.get("upload_type"),
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "total": job.get("total", 0),
        "result": job.get("result"),
        "error_message": job.get("error_message"),
        "errors": job.get("errors", []),
        "warnings": job.get("warnings", []),
        "filename": job.get("filename", ""),
        "started_at": job.get("started_at", ""),
        "updated_at": job.get("updated_at", ""),
        "completed_at": job.get("completed_at"),
        "paused_at": job.get("paused_at"),
        # Control flags
        "can_pause": job.get("status") == "running",
        "can_resume": job.get("status") == "paused",
        "can_stop": job.get("status") in ("running", "paused"),
    }


@router.post("/api/upload-jobs/{job_id}/pause", dependencies=[Depends(require_api_auth)])
async def pause_upload_job(job_id: str) -> dict:
    """Request to pause a running upload job."""
    if UploadJobService.pause_job(job_id):
        return {"status": "pause_requested", "job_id": job_id}
    raise HTTPException(status_code=400, detail="Cannot pause this job (not running)")


@router.post("/api/upload-jobs/{job_id}/resume", dependencies=[Depends(require_api_auth)])
async def resume_upload_job(job_id: str) -> dict:
    """Resume a paused upload job."""
    if UploadJobService.resume_job(job_id):
        return {"status": "resumed", "job_id": job_id}
    raise HTTPException(status_code=400, detail="Cannot resume this job (not paused)")


@router.post("/api/upload-jobs/{job_id}/stop", dependencies=[Depends(require_api_auth)])
async def stop_upload_job(job_id: str) -> dict:
    """Stop a running or paused upload job."""
    if UploadJobService.stop_job(job_id):
        return {"status": "stop_requested", "job_id": job_id}
    raise HTTPException(status_code=400, detail="Cannot stop this job")


@router.get("/api/upload-jobs", dependencies=[Depends(require_api_auth)])
async def list_upload_jobs(upload_type: str | None = None) -> dict:
    """List active upload jobs (running or paused). Optional filter by type (demand/bench)."""
    jobs = UploadJobService.list_active_jobs(upload_type)
    # Map to API response format
    job_list = []
    for job in jobs:
        job_list.append({
            "job_id": job.get("job_id"),
            "upload_type": job.get("upload_type"),
            "status": job.get("status"),
            "progress": job.get("progress", 0),
            "total": job.get("total", 0),
            "filename": job.get("filename", ""),
            "started_at": job.get("started_at", ""),
            "can_pause": job.get("status") == "running",
            "can_resume": job.get("status") == "paused",
            "can_stop": job.get("status") in ("running", "paused"),
        })
    return {"jobs": job_list, "count": len(job_list)}


# ── Required-field definitions for upload validation ─────────────────────────
_BENCH_REQUIRED_FIELDS: list[tuple[list[str], str, str]] = [
    # (candidate key names, display label, impact if missing)
    (["skill_list", "skills"],        "skill_list / skills",     "candidates will score 0 — matching will not work"),
    (["current_career_level",
      "accenture_level"],             "career level",            "career-level filtering will not work"),
    (["capability"],                  "capability",              "capability filter and scoring bonus will not apply"),
]

_DEMAND_REQUIRED_FIELDS: list[tuple[list[str], str, str]] = [
    (["role_primary_skill",
      "primary_skill"],               "primary_skill",           "demand cannot be matched — no skill to score against"),
    (["role_career_level_from",
      "career_level_from",
      "career_level"],                "career level",            "career-level filtering will not work"),
    (["role_status", "status"],       "status",                  "role will default to 'pending'; verify it should be open"),
    (["role_work_location",
      "role_location_type",
      "location"],                    "location",                "location will show as blank in the dashboard"),
    (["role_job_family_group",
      "skill_category_group",
      "capability"],                  "capability",              "capability filter and scoring bonus will not apply"),
]


def _check_missing_fields(
    rows: list[dict],
    field_specs: list[tuple[list[str], str, str]],
) -> list[str]:
    """Return warning strings for fields that are blank in ALL rows."""
    if not rows:
        return []
    warnings: list[str] = []
    for keys, label, impact in field_specs:
        # A field is "missing" when every row has no value for any of the key aliases
        all_blank = all(
            not any(row.get(k, "").strip() for k in keys)
            for row in rows
        )
        if all_blank:
            warnings.append(
                f"Column '{label}' is missing or blank in all rows — {impact}."
            )
    return warnings


def _run_bench_upload(job_id: str, rows: list[dict]) -> None:
    """Background worker: upsert bench CSV rows into Cosmos.
    Checks for pause/stop requests during processing."""
    try:
        from app.agents.shared.skill_utils import enrich_bench_row, load_common_skills
        from app.agents.submit_demand.tools.cosmos_store import upsert_candidate

        common_skills = load_common_skills()
        added = updated = unchanged = skipped = 0
        errors: list[str] = []
        warnings: list[str] = _check_missing_fields(rows, _BENCH_REQUIRED_FIELDS)
        added_names: list[str] = []
        updated_names: list[str] = []
        changed_entries: list[dict] = []  # added + updated entries for incremental cache patch

        for idx, row in enumerate(rows, 1):
            # Check for pause/stop requests
            if UploadJobService.check_stop_requested(job_id):
                UploadJobService.update_job_status(job_id, status="stopped", 
                    result={"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped})
                return
            
            if UploadJobService.check_pause_requested(job_id):
                UploadJobService.handle_pause(job_id)
                # Wait for resume or stop
                while True:
                    job = UploadJobService.get_job(job_id)
                    if job["status"] != "paused":
                        break
                    time.sleep(1)
                if job["status"] == "stopped":
                    UploadJobService.update_job_status(job_id, status="stopped",
                        result={"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped})
                    return
            
            UploadJobService.update_progress(job_id, idx)
            name = row.get("name", "").strip()
            role = row.get("primary_role", "").strip()
            if not name or not role:
                skipped += 1
                continue
            try:
                enriched = enrich_bench_row(dict(row), common_skills)
                career_raw = row.get("current_career_level", "").strip()
                if career_raw.isdigit():
                    accenture_level = f"CL-{career_raw}"
                else:
                    try:
                        accenture_level = f"CL-{int(float(career_raw))}"
                    except (ValueError, TypeError):
                        accenture_level = career_raw

                resume_link_raw = row.get("resume_link", "").strip()
                # Derive employee_id if not provided — use name + role combo as unique key
                employee_id = row.get("employee_id", "").strip()
                if not employee_id:
                    # Generate from name and role: "NAME-ROLE" format (safe for Cosmos)
                    safe_name = name.replace(" ", "-").lower()[:20]
                    safe_role = role.replace(" ", "-").lower()[:20]
                    employee_id = f"EMP-{safe_name}-{safe_role}"
                
                entry = {
                    "name": name,
                    "role": role,
                    "employee_id": employee_id,
                    "skill_list": row.get("skill_list", "").strip(),
                    "skill_profile": enriched.get("skill_profile", []),
                    "skills": [e["skill"] for e in enriched.get("skill_profile", []) if e.get("skill")],
                    "accenture_level": accenture_level,
                    "industries": row.get("industries", "").strip(),
                    "resume_link": resume_link_raw if resume_link_raw and resume_link_raw.upper() != "N/A" else "",
                    "capability": row.get("capability", "").strip(),
                }
                result = upsert_candidate(entry)
                if result.startswith("added:"):
                    added += 1
                    added_names.append(name)
                    changed_entries.append(entry)
                elif result.startswith("updated:"):
                    updated += 1
                    updated_names.append(name)
                    changed_entries.append(entry)
                    updated_names.append(name)
                    changed_entries.append(entry)
                else:
                    unchanged += 1
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                UploadJobService.add_error(job_id, f"{name}: {exc}")

        # Incrementally patch cache for changed candidates (avoids full rescore).
        # Falls back to a full cache clear automatically if the patch fails.
        _patch_cache_for_candidates(changed_entries)
        
        # Wait for cache to be fully invalidated before marking job as done
        # so the next dashboard refresh sees fresh scores
        _logger.info("[bench-upload] CSV processing complete, waiting for cache invalidation...")
        try:
            from app.agents.submit_demand.tools.cosmos_store import delete_report_cache_all
            delete_report_cache_all()
            _logger.info("[bench-upload] L2 report cache cleared, bench CSV job complete")
        except Exception as exc:
            _logger.warning("[bench-upload] L2 cache clear failed: %s", exc)
        
        UploadJobService.update_job_status(job_id, status="done", result={
            "added": added, "updated": updated, "unchanged": unchanged,
            "skipped": skipped, "errors": errors[:10],
            "warnings": warnings,
            "added_names": added_names[:50], "updated_names": updated_names[:50],
        })
    except Exception as exc:
        UploadJobService.update_job_status(job_id, status="error", error=str(exc))


def _run_demand_upload(job_id: str, rows: list[dict]) -> None:
    """Background worker: upsert demand CSV rows into Cosmos.
    Checks for pause/stop requests during processing."""
    try:
        from app.agents.shared.skill_utils import extract_skill_name, parse_demand_skill_column
        from app.agents.submit_demand.tools.cosmos_store import load_requests, load_roster, patch_request, upsert_request
        from app.agents.submit_demand.tools.request_store import _next_role_id

        added = updated = unchanged = skipped = 0
        errors: list[str] = []
        warnings: list[str] = _check_missing_fields(rows, _DEMAND_REQUIRED_FIELDS)
        added_ids: list[str] = []
        updated_ids: list[str] = []
        existing_requests = load_requests()

        # Collect unique roster skills once for AI skill mapping
        _roster_skill_set: set[str] = set()
        try:
            for c in load_roster():
                for sp in (c.get("skill_profile") or []):
                    name = sp.get("name") or sp.get("skill") or "" if isinstance(sp, dict) else str(sp)
                    if name:
                        _roster_skill_set.add(name)
                for sk in (c.get("skills") or []):
                    if isinstance(sk, str) and sk:
                        _roster_skill_set.add(sk)
        except Exception:
            pass  # roster unavailable — AI map will use fallback
        _roster_skills_list = sorted(_roster_skill_set)

        for idx, row in enumerate(rows, 1):
            # Check for pause/stop requests
            if UploadJobService.check_stop_requested(job_id):
                UploadJobService.update_job_status(job_id, status="stopped",
                    result={"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped})
                return
            
            if UploadJobService.check_pause_requested(job_id):
                UploadJobService.handle_pause(job_id)
                # Wait for resume or stop
                while True:
                    job = UploadJobService.get_job(job_id)
                    if job["status"] != "paused":
                        break
                    time.sleep(1)
                if job["status"] == "stopped":
                    UploadJobService.update_job_status(job_id, status="stopped",
                        result={"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped})
                    return
            
            UploadJobService.update_progress(job_id, idx)
            # The CSV upload normalises headers: "Role ID" → "role_id",
            # "Role Priority" → "role_priority", etc.
            # Support both prefixed (from Accenture CSV export) and plain keys.
            def _f(*keys: str, default: str = "") -> str:
                for k in keys:
                    v = row.get(k, "").strip()
                    if v:
                        return v
                return default

            role_id    = _f("role_id")
            role_title = _f("role_title", "role")
            if not role_title:
                skipped += 1
                continue
            try:
                # Career level: CSV exports use "Role Career Level From"
                cl_raw = _f("role_career_level_from", "career_level_from", "career_level")
                career_level = f"CL-{int(float(cl_raw))}" if cl_raw.replace(".", "").isdigit() else cl_raw

                # Capability: prefer Job Family Group, fall back to Skill Category Group
                capability = _f("role_job_family_group", "skill_category_group", "capability")

                # Location: prefer Work Location, fall back to Country/Territory
                location = _f(
                    "role_work_location", "role_location_type",
                    "role_country_territory", "location"
                )

                priority = _f("role_priority", "priority") or "Normal"
                status   = _f("role_status", "status") or "pending"

                industry = _f("industry_level_1", "industry", "industry_level_2")
                primary_skill = extract_skill_name(_f("role_primary_skill", "primary_skill"))
                secondary_skills = parse_demand_skill_column(_f("role_secondary_skill", "secondary_skills"))
                other_skills = parse_demand_skill_column(_f("role_other_skills", "other_skills"))

                # Skill inference is deferred to match time via infer_and_score_combined().
                # Description is stored in the demand doc so the matchmaker can read it.
                description = _f("Role Description", "role_description", "description", "notes")
                inferred_skills = []

                # MINIMAL document storage: only fields needed for report + matching
                # All metadata goes into staffing_intake (single source of truth)
                fields = {
                    "role_id":       role_id,  # Report needs this
                    "role":          role_title,  # Report needs this
                    "client":        _f("client"),  # Report needs this
                    "status":        status,  # For query filtering (open/pending)
                    "capability":    capability,
                    "location":      location,
                    "priority":      priority,
                    # ALL demand metadata goes ONLY into staffing_intake
                    "staffing_intake": {
                        "role":          role_title,
                        "client":        _f("client"),
                        "location":      location,
                        "location_type": _f("role_location_type", "role_work_location"),
                        "priority":      priority,
                        "career_level":  career_level,
                        # Report fields
                        "career_level_from": _f("role_career_level_from", "career_level_from"),
                        "career_level_to": _f("role_career_level_to", "career_level_to"),
                        "employee_id": _f("role_contractor_requisition_id", "employee_id"),
                        "practice": _f("wmu_level_1", "practice"),
                        "primary_contact": _f("role_primary_contact", "primary_contact"),
                        "contact_email": _f("role_primary_contact_email_id", "contact_email"),
                        "industry_level_1": industry,
                        "industry_level_2": _f("industry_level_2", "industry_l2"),
                        # Supporting fields
                        "is_overdue":    _f("role_is_overdue", "is_overdue").lower() == "yes",
                        "industry":      industry,
                        "skills": [primary_skill, *secondary_skills, *other_skills],
                    },
                    # skill_analysis: store primary/secondary/other so the matching report
                    # tool can use them directly without falling back to the flat skills list
                    # (which would wipe inferred_skills).
                    "role_description": description,
                    "skill_analysis": {
                        "primary_skill":    primary_skill,
                        "secondary_skills": secondary_skills,
                        "other_skills":     other_skills,
                        "inferred_skills": inferred_skills,
                        "accenture_level": career_level,
                        "demand_industry": industry if industry else None,
                    },
                }
                if role_id:
                    patched = patch_request(role_id, fields)
                    if patched is not None:
                        updated += 1
                        updated_ids.append(role_id)
                        continue
                new_role_id = role_id or _next_role_id(existing_requests)
                fields["role_id"] = new_role_id
                fields["role_status"] = fields["status"]
                upsert_request(fields)
                existing_requests.append({"role_id": new_role_id})
                added += 1
                added_ids.append(new_role_id)
            except Exception as exc:
                error_msg = f"{role_id or role_title}: {exc}"
                errors.append(error_msg)
                UploadJobService.add_error(job_id, error_msg)

        _bust_report_cache()
        UploadJobService.update_job_status(job_id, status="done", result={
            "added": added, "updated": updated, "unchanged": unchanged,
            "skipped": skipped, "errors": errors[:10],
            "warnings": warnings,
            "added_ids": added_ids[:50], "updated_ids": updated_ids[:50],
        })
    except Exception as exc:
        UploadJobService.update_job_status(job_id, status="error", error=str(exc))


@router.post("/api/bench/upload-csv", dependencies=[Depends(require_api_auth)])
async def upload_bench_csv(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> dict:
    """Accept a bench roster CSV, start background processing, return job_id immediately.
    CSV is stored in Cosmos DB so it survives pod restarts."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    def _normalise_header(h: str) -> str:
        return h.strip().lower().replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "")
    rows = [{_normalise_header(k): (v or "").strip()
             for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]
    
    # Create job document (metadata only, no CSV content)
    job_id = UploadJobService.create_job(
        upload_type="bench",
        total_rows=len(rows),
        filename=file.filename,
    )
    background_tasks.add_task(_run_bench_upload, job_id, rows)
    return {"job_id": job_id, "rows": len(rows)}


@router.post("/api/demands/upload-csv", dependencies=[Depends(require_api_auth)])
async def upload_demand_csv(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> dict:
    """Accept a demand list CSV, start background processing, return job_id immediately.
    CSV is stored in Cosmos DB so it survives pod restarts."""
    _logger.info("[upload_demand_csv] Received file: %s", file.filename)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    
    def _normalise_header(h: str) -> str:
        return h.strip().lower().replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "")
    
    rows = [{_normalise_header(k): (v or "").strip()
             for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]
    
    try:
        # Create job document (metadata only, no CSV content)
        job_id = UploadJobService.create_job(
            upload_type="demand",
            total_rows=len(rows),
            filename=file.filename,
        )
    except Exception as exc:
        _logger.error("[upload_demand_csv] Failed to create job in Cosmos DB: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create upload job: {exc}") from exc
    background_tasks.add_task(_run_demand_upload, job_id, rows)
    return {"job_id": job_id, "rows": len(rows)}



_CSS_BASE = """
  :root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3250;
        --accent:#6366f1;--accent2:#818cf8;--text:#e2e8f0;--text-muted:#8892aa;
        --green:#22c55e;--red:#ef4444;--yellow:#f59e0b;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;}
  a{color:var(--accent2);text-decoration:none;}
  a:hover{text-decoration:underline;}
  header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;
         display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100;}
  header h1{font-size:18px;font-weight:700;color:var(--accent2);white-space:nowrap;}
  .nav-links{display:flex;gap:16px;margin-left:auto;font-size:13px;}
  .nav-links a{color:var(--text-muted);padding:4px 10px;border-radius:6px;border:1px solid transparent;}
  .nav-links a:hover{color:var(--text);border-color:var(--border);text-decoration:none;}
  main{padding:20px 24px;}
  .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap;}
  .toolbar input{background:var(--surface2);color:var(--text);border:1px solid var(--border);
                 border-radius:6px;padding:7px 12px;font-size:13px;outline:none;min-width:260px;}
  .toolbar input:focus{border-color:var(--accent);}
  .toolbar select{background:var(--surface2);color:var(--text);border:1px solid var(--border);
                  border-radius:6px;padding:7px 10px;font-size:13px;outline:none;}
  .toolbar .count{margin-left:auto;font-size:12px;color:var(--text-muted);}
  .btn{padding:7px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;border:none;transition:opacity .15s;}
  .btn:hover{opacity:.85;}
  .btn-primary{background:var(--accent);color:#fff;}
  .btn-danger{background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b;}
  .btn-ghost{background:var(--surface2);color:var(--text-muted);border:1px solid var(--border);}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th{text-align:left;padding:8px 12px;border-bottom:2px solid var(--border);color:var(--text-muted);
     font-size:11px;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;}
  td{padding:9px 12px;border-bottom:1px solid var(--border)44;vertical-align:middle;}
  tr:hover td{background:var(--surface2)66;}
  .tag{background:var(--surface2);color:var(--text-muted);font-size:11px;padding:2px 7px;
       border-radius:4px;white-space:nowrap;display:inline-block;}
  .tag.green{background:#14532d44;color:var(--green);}
  .tag.yellow{background:#78350f44;color:var(--yellow);}
  .tag.red{background:#7f1d1d44;color:var(--red);}
  .pagination{display:flex;gap:6px;align-items:center;justify-content:center;padding:20px 0;flex-wrap:wrap;}
  .pagination button{background:var(--surface2);color:var(--text-muted);border:1px solid var(--border);
                     border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;min-width:32px;}
  .pagination button:hover{border-color:var(--accent);color:var(--text);}
  .pagination button.active{background:var(--accent);color:#fff;border-color:var(--accent);}
  .pagination button:disabled{opacity:.4;cursor:default;}
  .pagination .info{font-size:12px;color:var(--text-muted);margin:0 8px;}
  /* Modal */
  .overlay{display:none;position:fixed;inset:0;background:#00000088;z-index:200;
           align-items:center;justify-content:center;}
  .overlay.open{display:flex;}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;
         width:min(540px,94vw);max-height:88vh;overflow-y:auto;padding:28px 32px;position:relative;}
  .modal h2{font-size:16px;font-weight:700;color:var(--accent2);margin-bottom:20px;}
  .modal-close{position:absolute;top:14px;right:18px;background:none;border:none;
               color:var(--text-muted);font-size:22px;cursor:pointer;line-height:1;}
  .modal-close:hover{color:var(--text);}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;}
  .form-group{display:flex;flex-direction:column;gap:4px;}
  .form-group.full{grid-column:1/-1;}
  label{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em;}
  input[type=text],select,textarea{background:var(--surface2);color:var(--text);
    border:1px solid var(--border);border-radius:6px;padding:7px 10px;font-size:13px;
    outline:none;width:100%;font-family:inherit;}
  input[type=text]:focus,select:focus,textarea:focus{border-color:var(--accent);}
  textarea{resize:vertical;min-height:70px;}
  .form-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:20px;}
  .read-only{background:var(--bg)!important;color:var(--text-muted)!important;cursor:default;}
  #status-bar{font-size:12px;color:var(--text-muted);padding:6px 0;min-height:24px;}
  .empty{text-align:center;padding:48px;color:var(--text-muted);font-size:14px;}
  #confirm-overlay{display:none;position:fixed;inset:0;background:#00000088;z-index:300;align-items:center;justify-content:center;}
  #confirm-overlay.open{display:flex;}
  .confirm-box{background:var(--surface);border:1px solid #991b1b55;border-radius:10px;
               padding:28px 32px;max-width:400px;text-align:center;}
  .confirm-box h3{font-size:15px;margin-bottom:10px;color:var(--red);}
  .confirm-box p{font-size:13px;color:var(--text-muted);margin-bottom:20px;}
  .confirm-actions{display:flex;gap:10px;justify-content:center;}
  /* CSV Upload */
  .upload-panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;
                padding:16px 20px;margin-bottom:18px;}
  .upload-panel h3{font-size:13px;font-weight:600;color:var(--accent2);margin-bottom:12px;}
  .drop-zone{border:2px dashed var(--border);border-radius:8px;padding:18px;
             text-align:center;font-size:13px;color:var(--text-muted);cursor:pointer;
             transition:border-color .2s,background .2s;}
  .drop-zone:hover,.drop-zone.dragover{border-color:var(--accent);background:var(--surface2);}
  .drop-zone input[type=file]{display:none;}
  .upload-result{margin-top:12px;font-size:13px;display:none;}
  .upload-result.show{display:block;}
  .summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:10px;}
  .summary-card{background:var(--surface2);border-radius:6px;padding:10px 14px;text-align:center;}
  .summary-card .num{font-size:22px;font-weight:700;}
  .summary-card .lbl{font-size:11px;color:var(--text-muted);margin-top:2px;}
  .summary-card.added .num{color:var(--green);}
  .summary-card.updated .num{color:var(--accent2);}
  .summary-card.unchanged .num{color:var(--text-muted);}
  .summary-card.skipped .num{color:var(--yellow);}
  .summary-card.errors .num{color:var(--red);}
  .name-list{font-size:11px;color:var(--text-muted);line-height:1.7;max-height:100px;overflow-y:auto;
             background:var(--bg);border-radius:4px;padding:6px 10px;}
"""

# ── Landing Page ──────────────────────────────────────────────────────────────

_LANDING_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Staffing Platform</title>
  <style>
    {_CSS_BASE}
    header{{justify-content:center;}}
    header h1{{font-size:22px;}}
    main{{max-width:960px;margin:60px auto;padding:0 24px;}}
    .subtitle{{font-size:14px;color:var(--text-muted);text-align:center;margin-bottom:44px;}}
    .nav-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px;}}
    .nav-card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
               padding:32px 28px;text-decoration:none;color:var(--text);
               transition:border-color .2s,transform .15s;display:block;}}
    .nav-card:hover{{border-color:var(--accent);transform:translateY(-2px);text-decoration:none;}}
    .nav-card .icon{{font-size:36px;margin-bottom:16px;}}
    .nav-card h2{{font-size:18px;font-weight:700;color:var(--accent2);margin-bottom:8px;}}
    .nav-card p{{font-size:13px;color:var(--text-muted);line-height:1.6;}}
  </style>
</head>
<body>
  <header><h1>🎯 Staffing Platform</h1><nav class="nav-links"><a href="/admin">Admin</a><a href="/logout" style="color:var(--red)">Logout</a></nav></header>
  <main>
    <p class="subtitle">Manage your bench roster, open demands, and view match analytics.</p>
    <div class="nav-grid">
      <a href="/report/dashboard" class="nav-card">
        <div class="icon">📊</div>
        <h2>Dashboard</h2>
        <p>View bench-to-demand match scores, fit tiers, and pipeline health.</p>
      </a>
      <a href="/admin" class="nav-card">
        <div class="icon">⚙️</div>
        <h2>Admin</h2>
        <p>Upload CSVs, manage demand &amp; bench data, clear cache, and system controls.</p>
      </a>
      <a href="/manage/bench" class="nav-card">
        <div class="icon">👥</div>
        <h2>Manage Bench</h2>
        <p>Browse, search, edit and remove candidates from the bench roster.</p>
      </a>
      <a href="/manage/demand" class="nav-card">
        <div class="icon">📋</div>
        <h2>Manage Demand</h2>
        <p>Review, update and remove open staffing demand requests.</p>
      </a>
    </div>
  </main>
</body>
</html>"""

# ── Manage Bench Page ─────────────────────────────────────────────────────────

_BENCH_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Manage Bench | Staffing Platform</title>
  <style>{_CSS_BASE}</style>
</head>
<body>
<header>
  <h1>👥 Manage Bench</h1>
  <nav class="nav-links">
    <a href="/">Home</a>
    <a href="/report/dashboard">Dashboard</a>
    <a href="/admin">Admin</a>
    <a href="/manage/demand">Manage Demand</a>
    <a href="/logout" style="color:var(--red)">Logout</a>
  </nav>
</header>

<main>
  <!-- CSV Upload Panel -->
  <div class="upload-panel">
    <h3>📥 Upload Bench CSV</h3>
    <div class="drop-zone" id="bench-drop" onclick="document.getElementById('bench-file-input').click()"
         ondragover="ev(event,'bench-drop',true)" ondragleave="ev(event,'bench-drop',false)"
         ondrop="onDrop(event,'bench')">
      <input type="file" id="bench-file-input" accept=".csv" onchange="uploadCsv('bench',this)">
      <span id="bench-drop-label">Drag & drop a CSV here, or click to choose a file</span>
    </div>
    <div class="upload-result" id="bench-upload-result"></div>
  </div>

  <div class="toolbar">
    <input id="search" type="text" placeholder="Search name, role, skill, capability…" oninput="onSearch()">
    <select id="page-size" onchange="currentPage=1;loadData()">
      <option value="10">10 / page</option>
      <option value="20" selected>20 / page</option>
      <option value="50">50 / page</option>
      <option value="100">100 / page</option>
    </select>
    <div id="status-bar">Loading…</div>
    <span class="count" id="count-label"></span>
  </div>

  <table id="bench-table">
    <thead>
      <tr>
        <th>Name</th><th>Role</th><th>Level</th><th>Capability</th>
        <th>Skills</th><th>Availability</th><th>Resume</th><th style="width:100px">Actions</th>
      </tr>
    </thead>
    <tbody id="bench-body"></tbody>
  </table>
  <div class="empty" id="empty-msg" style="display:none">No candidates found.</div>
  <div class="pagination" id="pagination"></div>
</main>

<!-- Skills Detail Modal -->
<div class="overlay" id="skills-overlay" onclick="if(event.target===this)closeModal('skills-overlay')">
  <div class="modal" style="width:min(560px,94vw)">
    <button class="modal-close" onclick="closeModal('skills-overlay')">&times;</button>
    <h2 id="skills-modal-title">Skills</h2>
    <div id="skills-modal-body"></div>
  </div>
</div>

<!-- Edit Modal -->
<div class="overlay" id="edit-overlay">
  <div class="modal">
    <button class="modal-close" onclick="closeModal('edit-overlay')">&times;</button>
    <h2>Edit Candidate</h2>
    <input type="hidden" id="edit-doc-id">
    <input type="hidden" id="edit-name-hidden">
    <div class="form-grid">
      <div class="form-group full">
        <label>Name (read-only)</label>
        <input type="text" id="edit-name-display" class="read-only" readonly>
      </div>
      <div class="form-group">
        <label>Role</label>
        <input type="text" id="edit-role">
      </div>
      <div class="form-group">
        <label>Accenture Level</label>
        <input type="text" id="edit-level" placeholder="e.g. CL-8">
      </div>
      <div class="form-group">
        <label>Capability</label>
        <input type="text" id="edit-capability">
      </div>
      <div class="form-group">
        <label>Availability</label>
        <select id="edit-availability">
          <option>Available</option>
          <option>Unavailable</option>
          <option>On Project</option>
        </select>
      </div>
      <div class="form-group full">
        <label>Industries</label>
        <input type="text" id="edit-industries">
      </div>
      <div class="form-group full">
        <label>Skills (comma-separated)</label>
        <textarea id="edit-skills" rows="3"></textarea>
      </div>
      <div class="form-group full">
        <label>Resume Link</label>
        <input type="text" id="edit-resume-link">
      </div>
    </div>
    <div class="form-actions">
      <button class="btn btn-ghost" onclick="closeModal('edit-overlay')">Cancel</button>
      <button class="btn btn-primary" onclick="saveEdit()">Save Changes</button>
    </div>
  </div>
</div>

<!-- Confirm Delete -->
<div id="confirm-overlay">
  <div class="confirm-box">
    <h3>Delete Candidate?</h3>
    <p id="confirm-msg">This will permanently remove the candidate from the bench roster.</p>
    <div class="confirm-actions">
      <button class="btn btn-ghost" onclick="closeConfirm()">Cancel</button>
      <button class="btn btn-danger" id="confirm-yes" onclick="execDelete()">Delete</button>
    </div>
  </div>
</div>

<script>
let currentPage = 1;
let searchTimer = null;
let pendingDelete = null;

function onSearch() {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{ currentPage = 1; loadData(); }}, 350);
}}

async function loadData() {{
  const search = document.getElementById('search').value.trim();
  const pageSize = document.getElementById('page-size').value;
  const url = `/api/bench?page=${{currentPage}}&page_size=${{pageSize}}&search=${{encodeURIComponent(search)}}`;
  setStatus('Loading…');
  try {{
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderRows(data.items || []);
    renderPagination(data.pagination);
    const p = data.pagination || {{}};
    setStatus('');
    document.getElementById('count-label').textContent =
      `${{p.total ?? 0}} total · page ${{p.page ?? 1}} of ${{p.total_pages ?? 1}}`;
  }} catch(e) {{
    setStatus('Error: ' + e.message);
  }}
}}

const _PROF_LABEL = ['','Beginner','Basic','Intermediate','Advanced','Expert'];
const _PROF_COLOR = ['','#8892aa','#f59e0b','#3b82f6','#6366f1','#22c55e'];

function renderSkillChips(c) {{
  const profile = c.skill_profile || [];
  const flat    = c.skills || [];
  // Build unified list: prefer skill_profile (has proficiency), fall back to flat skills
  const all = profile.length
    ? profile.map(e => ({{ skill: e.skill||e, prof: e.proficiency||0 }}))
    : flat.map(s => ({{ skill: s, prof: 0 }}));

  if (!all.length) return '<span style="color:var(--text-muted)">—</span>';

  const MAX_INLINE = 4;
  const shown = all.slice(0, MAX_INLINE);
  const rest  = all.length - MAX_INLINE;

  const chips = shown.map(e => {{
    const col = _PROF_COLOR[e.prof] || '#8892aa';
    const lbl = _PROF_LABEL[e.prof] || '';
    const title = lbl ? `title="${{esc(e.skill)}} — ${{lbl}}"` : `title="${{esc(e.skill)}}"`;
    return `<span class="tag" ${{title}} style="border-left:3px solid ${{col}};cursor:default">${{esc(e.skill)}}</span>`;
  }}).join(' ');

  const moreBtn = rest > 0
    ? ` <button class="btn btn-ghost" style="font-size:10px;padding:2px 7px"
          onclick='openSkills(${{JSON.stringify(c)}})'>+${{rest}} more</button>`
    : '';

  return `<div style="display:flex;flex-wrap:wrap;gap:3px;align-items:center">${{chips}}${{moreBtn}}</div>`;
}}

function renderRows(items) {{
  const tbody = document.getElementById('bench-body');
  const empty = document.getElementById('empty-msg');
  if (!items.length) {{
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }}
  empty.style.display = 'none';
  tbody.innerHTML = items.map(c => {{
    const avail = c.availability || 'Unknown';
    const availClass = avail === 'Available' ? 'green' : avail === 'Unavailable' ? 'red' : 'yellow';
    return `<tr>
      <td><strong>${{esc(c.name)}}</strong><br><span style="font-size:11px;color:var(--text-muted)">${{esc(c.candidate_id||'')}}</span></td>
      <td>${{esc(c.role||'—')}}</td>
      <td><span class="tag">${{esc(c.accenture_level||'—')}}</span></td>
      <td>${{esc(c.capability||'—')}}</td>
      <td style="max-width:260px">${{renderSkillChips(c)}}</td>
      <td><span class="tag ${{availClass}}">${{esc(avail)}}</span></td>
      <td>${{resumeLink(c.resume_link)}}</td>
      <td>
        <button class="btn btn-ghost" style="margin-right:4px" onclick='openEdit(${{JSON.stringify(c)}})'>Edit</button>
        <button class="btn btn-danger" onclick='askDelete(${{JSON.stringify(c)}})'>Del</button>
      </td>
    </tr>`;
  }}).join('');
}}

function openSkills(c) {{
  const profile = c.skill_profile || [];
  const flat    = c.skills || [];
  const all = profile.length
    ? profile.map(e => ({{ skill: e.skill||e, prof: e.proficiency||0 }}))
    : flat.map(s => ({{ skill: s, prof: 0 }}));

  const rows = all.map(e => {{
    const col   = _PROF_COLOR[e.prof] || '#8892aa';
    const label = _PROF_LABEL[e.prof] || 'Unknown';
    const pct   = e.prof > 0 ? (e.prof / 5) * 100 : 0;
    return `<tr>
      <td style="padding:8px 10px;font-size:13px;font-weight:500">${{esc(e.skill)}}</td>
      <td style="padding:8px 10px">
        <div style="display:flex;align-items:center;gap:10px">
          <div style="width:120px;height:6px;background:var(--border);border-radius:3px;overflow:hidden">
            <div style="width:${{pct}}%;height:100%;background:${{col}};border-radius:3px"></div>
          </div>
          <span style="font-size:12px;color:${{col}};font-weight:600;min-width:70px">${{label}}</span>
          ${{e.prof > 0 ? `<span style="font-size:11px;color:var(--text-muted)">P${{e.prof}}/5</span>` : ''}}
        </div>
      </td>
    </tr>`;
  }}).join('');

  document.getElementById('skills-modal-title').textContent = `${{c.name}} — Skills (${{all.length}})`;
  document.getElementById('skills-modal-body').innerHTML = `
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr>
          <th style="text-align:left;padding:6px 10px;font-size:11px;color:var(--text-muted);border-bottom:1px solid var(--border);text-transform:uppercase">Skill</th>
          <th style="text-align:left;padding:6px 10px;font-size:11px;color:var(--text-muted);border-bottom:1px solid var(--border);text-transform:uppercase">Proficiency</th>
        </tr>
      </thead>
      <tbody>${{rows}}</tbody>
    </table>`;
  document.getElementById('skills-overlay').classList.add('open');
}}

function renderPagination(p) {{
  const el = document.getElementById('pagination');
  if (!p || p.total_pages <= 1) {{ el.innerHTML = ''; return; }}
  const {{page, total_pages}} = p;
  let html = `<button ${{page===1?'disabled':''}} onclick="go(${{page-1}})">‹</button>`;
  const start = Math.max(1, page-3), end = Math.min(total_pages, start+6);
  for (let i=start;i<=end;i++) html += `<button class="${{i===page?'active':''}}" onclick="go(${{i}})">${{i}}</button>`;
  html += `<button ${{page===total_pages?'disabled':''}} onclick="go(${{page+1}})">›</button>`;
  el.innerHTML = html;
}}

function go(p) {{ currentPage = p; loadData(); }}
function setStatus(msg) {{ document.getElementById('status-bar').textContent = msg; }}
function closeModal(id) {{ document.getElementById(id).classList.remove('open'); }}

function openEdit(c) {{
  document.getElementById('edit-doc-id').value = c.id || '';
  document.getElementById('edit-name-hidden').value = c.name || '';
  document.getElementById('edit-name-display').value = c.name || '';
  document.getElementById('edit-role').value = c.role || '';
  document.getElementById('edit-level').value = c.accenture_level || '';
  document.getElementById('edit-capability').value = c.capability || '';
  document.getElementById('edit-availability').value = c.availability || 'Available';
  document.getElementById('edit-industries').value = c.industries || '';
  document.getElementById('edit-skills').value = (c.skills || []).join(', ');
  document.getElementById('edit-resume-link').value = c.resume_link || '';
  document.getElementById('edit-overlay').classList.add('open');
}}

async function saveEdit() {{
  const docId = document.getElementById('edit-doc-id').value;
  const name = document.getElementById('edit-name-hidden').value;
  const skillsRaw = document.getElementById('edit-skills').value;
  const body = {{
    name,
    role: document.getElementById('edit-role').value.trim(),
    accenture_level: document.getElementById('edit-level').value.trim(),
    capability: document.getElementById('edit-capability').value.trim(),
    availability: document.getElementById('edit-availability').value,
    industries: document.getElementById('edit-industries').value.trim(),
    skills: skillsRaw.split(',').map(s=>s.trim()).filter(Boolean),
    resume_link: document.getElementById('edit-resume-link').value.trim(),
  }};
  try {{
    const res = await fetch(`/api/bench/${{encodeURIComponent(docId)}}`, {{
      method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)
    }});
    if (!res.ok) {{ const e=await res.json(); throw new Error(e.detail||res.status); }}
    closeModal('edit-overlay');
    setStatus('Saved.');
    loadData();
  }} catch(e) {{
    setStatus('Save failed: ' + e.message);
  }}
}}

function askDelete(c) {{
  pendingDelete = c;
  document.getElementById('confirm-msg').textContent =
    `Delete "${{c.name}}" (${{c.candidate_id||c.id}}) from the bench roster? This cannot be undone.`;
  document.getElementById('confirm-overlay').classList.add('open');
}}

function closeConfirm() {{
  pendingDelete = null;
  document.getElementById('confirm-overlay').classList.remove('open');
}}

async function execDelete() {{
  if (!pendingDelete) return;
  const c = pendingDelete;
  closeConfirm();
  try {{
    const res = await fetch(
      `/api/bench/${{encodeURIComponent(c.id)}}?name=${{encodeURIComponent(c.name)}}`,
      {{method:'DELETE'}}
    );
    if (!res.ok) {{ const e=await res.json(); throw new Error(e.detail||res.status); }}
    setStatus(`Deleted ${{c.name}}.`);
    loadData();
  }} catch(e) {{
    setStatus('Delete failed: ' + e.message);
  }}
}}

function esc(s) {{
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function resumeLink(url) {{
  if (!url || url.trim() === '' || url.trim().toUpperCase() === 'N/A') return '<span style="color:var(--text-muted);font-size:11px">—</span>';
  return `<a href="${{esc(url)}}" target="_blank" rel="noopener noreferrer"
    style="font-size:12px;color:var(--accent2);white-space:nowrap" title="${{esc(url)}}">📄 Resume</a>`;
}}

// ── CSV Upload ─────────────────────────────────────
function ev(e, id, on) {{
  e.preventDefault();
  document.getElementById(id).classList.toggle('dragover', on);
}}
function onDrop(e, kind) {{
  e.preventDefault();
  const id = kind + '-drop';
  document.getElementById(id).classList.remove('dragover');
  const f = e.dataTransfer.files[0];
  if (f) uploadCsv(kind, null, f);
}}
async function uploadCsv(kind, input, file) {{
  const f = file || (input && input.files[0]);
  if (!f) return;
  const labelEl = document.getElementById(kind + '-drop-label');
  const resultEl = document.getElementById(kind + '-upload-result');
  labelEl.textContent = `Uploading ${{f.name}}…`;
  resultEl.className = 'upload-result';
  const fd = new FormData();
  fd.append('file', f);
  const endpoint = kind === 'bench' ? '/api/bench/upload-csv' : '/api/demands/upload-csv';
  try {{
    // POST the file — server returns immediately with a job_id
    const res = await fetch(endpoint, {{ method: 'POST', body: fd }});
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {{
      const txt = await res.text();
      throw new Error(`Server error ${{res.status}}: ${{txt.slice(0,200)}}`);
    }}
    const init = await res.json();
    if (!res.ok) throw new Error(init.detail || res.status);
    const {{job_id, rows}} = init;
    labelEl.textContent = `Processing ${{f.name}} (${{rows}} rows)…`;
    resultEl.innerHTML = `<span style="color:var(--text-muted)">Processing in background… this page will update automatically.</span>`;
    resultEl.className = 'upload-result show';
    // Poll until done
    await pollJob(job_id, kind, f.name, labelEl, resultEl);
  }} catch(e) {{
    resultEl.innerHTML = `<span style="color:var(--red)">Upload failed: ${{esc(e.message)}}</span>`;
    resultEl.className = 'upload-result show';
    labelEl.textContent = 'Drag & drop a CSV here, or click to choose a file';
  }}
  if (input) input.value = '';
}}
async function pollJob(jobId, kind, fname, labelEl, resultEl) {{
  const POLL_MS = 2000;
  const MAX_WAIT = 1800000; // 30 min — large uploads with AI take time
  const started = Date.now();
  while (Date.now() - started < MAX_WAIT) {{
    await new Promise(r => setTimeout(r, POLL_MS));
    try {{
      const res = await fetch(`/api/upload-jobs/${{jobId}}`);
      const job = await res.json();
      // Show progress bar while running
      if (job.status === 'running' && job.total > 0) {{
        const pct = Math.round((job.progress / job.total) * 100);
        resultEl.innerHTML = `
          <div style="margin-bottom:6px;font-size:12px;color:var(--text-muted)">
            Processing row ${{job.progress}} of ${{job.total}}…
          </div>
          <div style="background:var(--border);border-radius:4px;height:8px;overflow:hidden">
            <div style="width:${{pct}}%;height:100%;background:var(--accent);border-radius:4px;transition:width .3s"></div>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px;text-align:right">${{pct}}%</div>`;
        resultEl.className = 'upload-result show';
      }}
      if (job.status === 'done') {{
        if (job.error) {{
          resultEl.innerHTML = `<span style="color:var(--red)">Processing failed: ${{esc(job.error)}}</span>`;
          labelEl.textContent = 'Drag & drop a CSV here, or click to choose a file';
        }} else {{
          const data = job.result || {{}};
          const addedList   = (data.added_names || data.added_ids   || []).slice(0,20);
          const updatedList = (data.updated_names || data.updated_ids || []).slice(0,20);
          resultEl.innerHTML = `
            <div class="summary-grid">
              <div class="summary-card added"><div class="num">${{data.added}}</div><div class="lbl">Added</div></div>
              <div class="summary-card updated"><div class="num">${{data.updated}}</div><div class="lbl">Updated</div></div>
              <div class="summary-card unchanged"><div class="num">${{data.unchanged}}</div><div class="lbl">Unchanged</div></div>
              <div class="summary-card skipped"><div class="num">${{data.skipped}}</div><div class="lbl">Skipped</div></div>
              ${{data.errors?.length ? `<div class="summary-card errors"><div class="num">${{data.errors.length}}</div><div class="lbl">Errors</div></div>` : ''}}
            </div>
            ${{(data.warnings?.length) ? `
              <div style="margin:10px 0 6px;background:#78350f33;border:1px solid #92400e88;
                          border-radius:6px;padding:10px 14px">
                <strong style="font-size:11px;color:var(--yellow);display:block;margin-bottom:6px">
                  ⚠ ${{data.warnings.length}} field warning${{data.warnings.length>1?'s':''}} — some data is missing that affects matching quality:
                </strong>
                <ul style="margin:0;padding-left:18px;font-size:12px;color:var(--yellow);line-height:1.8">
                  ${{data.warnings.map(w => `<li>${{esc(w)}}</li>`).join('')}}
                </ul>
                <div style="font-size:11px;color:var(--text-muted);margin-top:6px">
                  Please correct the CSV and re-upload to ensure accurate results.
                </div>
              </div>` : ''}}
            ${{addedList.length ? `<div style="margin-bottom:6px"><strong style="font-size:11px;color:var(--green)">NEW:</strong><div class="name-list">${{addedList.map(esc).join(', ')}}</div></div>` : ''}}
            ${{updatedList.length ? `<div style="margin-bottom:6px"><strong style="font-size:11px;color:var(--accent2)">UPDATED:</strong><div class="name-list">${{updatedList.map(esc).join(', ')}}</div></div>` : ''}}
            ${{data.errors?.length ? `<div><strong style="font-size:11px;color:var(--red)">ERRORS:</strong><div class="name-list">${{data.errors.map(esc).join('<br>')}}</div></div>` : ''}}
          `;
          labelEl.textContent = `${{fname}} — done. Drop another file or click to choose.`;
          loadData();
        }}
        resultEl.className = 'upload-result show';
        return;
      }}
    }} catch(_) {{ /* keep polling */ }}
  }}
  resultEl.innerHTML = `<span style="color:var(--yellow)">Still processing… refresh the page in a moment.</span>`;
  resultEl.className = 'upload-result show';
}}

loadData();
</script>
</body>
</html>"""

# ── Manage Demand Page ────────────────────────────────────────────────────────

_DEMAND_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Manage Demand | Staffing Platform</title>
  <style>{_CSS_BASE}</style>
</head>
<body>
<header>
  <h1>📋 Manage Demand</h1>
  <nav class="nav-links">
    <a href="/">Home</a>
    <a href="/report/dashboard">Dashboard</a>
    <a href="/admin">Admin</a>
    <a href="/manage/bench">Manage Bench</a>
    <a href="/logout" style="color:var(--red)">Logout</a>
  </nav>
</header>

<main>
  <!-- CSV Upload Panel -->
  <div class="upload-panel">
    <h3>📥 Upload Demand CSV</h3>
    <div class="drop-zone" id="demand-drop" onclick="document.getElementById('demand-file-input').click()"
         ondragover="ev(event,'demand-drop',true)" ondragleave="ev(event,'demand-drop',false)"
         ondrop="onDrop(event,'demand')">
      <input type="file" id="demand-file-input" accept=".csv" onchange="uploadCsv('demand',this)">
      <span id="demand-drop-label">Drag & drop a CSV here, or click to choose a file</span>
    </div>
    <div class="upload-result" id="demand-upload-result"></div>
  </div>

  <div class="toolbar">
    <input id="search" type="text" placeholder="Search role ID, title, client, skill…" oninput="onSearch()">
    <select id="page-size" onchange="currentPage=1;loadData()">
      <option value="10">10 / page</option>
      <option value="20" selected>20 / page</option>
      <option value="50">50 / page</option>
      <option value="100">100 / page</option>
    </select>
    <div id="status-bar">Loading…</div>
    <span class="count" id="count-label"></span>
  </div>

  <table id="demand-table">
    <thead>
      <tr>
        <th>Role ID</th><th>Title</th><th>Client</th><th>Industry</th><th>Level</th>
        <th>Primary Skill</th><th>Priority</th><th>Status</th><th style="width:100px">Actions</th>
      </tr>
    </thead>
    <tbody id="demand-body"></tbody>
  </table>
  <div class="empty" id="empty-msg" style="display:none">No demands found.</div>
  <div class="pagination" id="pagination"></div>
</main>

<!-- Edit Modal -->
<div class="overlay" id="edit-overlay">
  <div class="modal">
    <button class="modal-close" onclick="closeModal('edit-overlay')">&times;</button>
    <h2>Edit Demand</h2>
    <input type="hidden" id="edit-role-id">
    <div class="form-grid">
      <div class="form-group full">
        <label>Role ID (read-only)</label>
        <input type="text" id="edit-role-id-display" class="read-only" readonly>
      </div>
      <div class="form-group full">
        <label>Role Title</label>
        <input type="text" id="edit-title">
      </div>
      <div class="form-group">
        <label>Client</label>
        <input type="text" id="edit-client">
      </div>
      <div class="form-group">
        <label>Primary Skill</label>
        <input type="text" id="edit-primary-skill">
      </div>
      <div class="form-group full">
        <label>Secondary Skills <span style="font-size:10px;color:var(--text-muted)">(comma-separated)</span></label>
        <input type="text" id="edit-secondary-skills" placeholder="e.g. Spring Boot, React">
      </div>
      <div class="form-group full">
        <label>Other Skills <span style="font-size:10px;color:var(--text-muted)">(comma-separated)</span></label>
        <input type="text" id="edit-other-skills" placeholder="e.g. Docker, Kubernetes">
      </div>
      <div class="form-group full">
        <label>Inferred Skills <span style="font-size:10px;color:var(--text-muted)">(auto-extracted from description, read-only)</span></label>
        <input type="text" id="edit-inferred-skills" class="read-only" readonly>
      </div>
      <div class="form-group">
        <label>Career Level</label>
        <input type="text" id="edit-career-level" placeholder="e.g. CL-8">
      </div>
      <div class="form-group">
        <label>Priority</label>
        <select id="edit-priority">
          <option>Normal</option>
          <option>High</option>
          <option>Critical</option>
        </select>
      </div>
      <div class="form-group">
        <label>Status</label>
        <select id="edit-status">
          <option value="pending">pending</option>
          <option value="Open - New">Open - New</option>
          <option value="Open - In Process">Open - In Process</option>
          <option value="Open - Need Project Feedback">Open - Need Project Feedback</option>
          <option value="Open - Confirming Candidate">Open - Confirming Candidate</option>
          <option value="fulfilled">fulfilled</option>
          <option value="cancelled">cancelled</option>
        </select>
      </div>
      <div class="form-group">
        <label>Capability</label>
        <input type="text" id="edit-capability" placeholder="e.g. Software Engineering">
      </div>
      <div class="form-group">
        <label>Location</label>
        <input type="text" id="edit-location" placeholder="e.g. Remote, New York">
      </div>
      <div class="form-group">
        <label>Industry</label>
        <input type="text" id="edit-industry" placeholder="e.g. Financial Services">
      </div>
      <div class="form-group full">
        <label>Notes</label>
        <textarea id="edit-notes" rows="3"></textarea>
      </div>
    </div>
    <div class="form-actions">
      <button class="btn btn-ghost" onclick="closeModal('edit-overlay')">Cancel</button>
      <button class="btn btn-primary" onclick="saveEdit()">Save Changes</button>
    </div>
  </div>
</div>

<!-- Confirm Delete -->
<div id="confirm-overlay">
  <div class="confirm-box">
    <h3>Delete Demand?</h3>
    <p id="confirm-msg">This will permanently remove the demand from Cosmos DB.</p>
    <div class="confirm-actions">
      <button class="btn btn-ghost" onclick="closeConfirm()">Cancel</button>
      <button class="btn btn-danger" onclick="execDelete()">Delete</button>
    </div>
  </div>
</div>

<script>
let currentPage = 1;
let searchTimer = null;
let pendingDelete = null;

function onSearch() {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{ currentPage = 1; loadData(); }}, 350);
}}

async function loadData() {{
  const search = document.getElementById('search').value.trim();
  const pageSize = document.getElementById('page-size').value;
  const url = `/api/demands?page=${{currentPage}}&page_size=${{pageSize}}&search=${{encodeURIComponent(search)}}`;
  setStatus('Loading…');
  try {{
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderRows(data.items || []);
    renderPagination(data.pagination);
    const p = data.pagination || {{}};
    setStatus('');
    document.getElementById('count-label').textContent =
      `${{p.total ?? 0}} total · page ${{p.page ?? 1}} of ${{p.total_pages ?? 1}}`;
  }} catch(e) {{
    setStatus('Error: ' + e.message);
  }}
}}

function statusClass(s) {{
  if (!s) return '';
  s = s.toLowerCase();
  if (s === 'fulfilled') return 'green';
  if (s === 'cancelled') return 'red';
  return 'yellow';
}}

function priorityClass(p) {{
  if (!p) return '';
  if (p === 'Critical') return 'red';
  if (p === 'High') return 'yellow';
  return '';
}}

function renderRows(items) {{
  const tbody = document.getElementById('demand-body');
  const empty = document.getElementById('empty-msg');
  if (!items.length) {{
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }}
  empty.style.display = 'none';
  tbody.innerHTML = items.map(d => {{
    const title = d.role_title || d.role || '—';
    const level = d.career_level_from ? `CL-${{d.career_level_from}}` : (d.career_level || '—');
    const status = d.status || d.role_status || 'pending';
    const priority = d.priority || 'Normal';
    return `<tr>
      <td><code style="font-size:12px;color:var(--accent2)">${{esc(d.role_id||'—')}}</code></td>
      <td style="max-width:200px">${{esc(title)}}</td>
      <td>${{esc(d.client||'—')}}</td>
      <td>${{esc(_demandIndustry(d))}}</td>
      <td><span class="tag">${{esc(level)}}</span></td>
      <td>${{esc(d.primary_skill||'—')}}</td>
      <td><span class="tag ${{priorityClass(priority)}}">${{esc(priority)}}</span></td>
      <td><span class="tag ${{statusClass(status)}}">${{esc(status)}}</span></td>
      <td>
        <button class="btn btn-ghost" style="margin-right:4px" onclick='openEdit(${{JSON.stringify(d)}})'>Edit</button>
        <button class="btn btn-danger" onclick='askDelete(${{JSON.stringify(d)}})'>Del</button>
      </td>
    </tr>`;
  }}).join('');
}}

function renderPagination(p) {{
  const el = document.getElementById('pagination');
  if (!p || p.total_pages <= 1) {{ el.innerHTML = ''; return; }}
  const {{page, total_pages}} = p;
  let html = `<button ${{page===1?'disabled':''}} onclick="go(${{page-1}})">‹</button>`;
  const start = Math.max(1, page-3), end = Math.min(total_pages, start+6);
  for (let i=start;i<=end;i++) html += `<button class="${{i===page?'active':''}}" onclick="go(${{i}})">${{i}}</button>`;
  html += `<button ${{page===total_pages?'disabled':''}} onclick="go(${{page+1}})">›</button>`;
  el.innerHTML = html;
}}

function go(p) {{ currentPage = p; loadData(); }}
function setStatus(msg) {{ document.getElementById('status-bar').textContent = msg; }}
function closeModal(id) {{ document.getElementById(id).classList.remove('open'); }}

function openEdit(d) {{
  const title = d.role_title || d.role || '';
  const sa = d.skill_analysis || {{}};
  const intake = d.staffing_intake || {{}};
  const level = intake.career_level_from || d.career_level_from || (d.career_level||'').replace('CL-','') || '';
  // Skills: prefer structured skill_analysis fields, fall back to flat staffing_intake.skills list
  const flatSkills = intake.skills || [];
  const primarySkill = sa.primary_skill || d.primary_skill || (flatSkills[0] || '');
  const secondarySkills = sa.secondary_skills || d.secondary_skills
    || (flatSkills.length > 1 ? flatSkills.slice(1, 3) : []);
  const otherSkills = sa.other_skills || d.other_skills
    || (flatSkills.length > 3 ? flatSkills.slice(3) : []);
  document.getElementById('edit-role-id').value = d.role_id || '';
  document.getElementById('edit-role-id-display').value = d.role_id || '';
  document.getElementById('edit-title').value = title;
  document.getElementById('edit-client').value = intake.client || d.client || '';
  document.getElementById('edit-primary-skill').value = primarySkill;
  document.getElementById('edit-secondary-skills').value = secondarySkills.join(', ');
  document.getElementById('edit-other-skills').value = otherSkills.join(', ');
  document.getElementById('edit-inferred-skills').value = (sa.inferred_skills || []).join(', ') || '(none)';
  document.getElementById('edit-career-level').value = d.career_level || intake.career_level || (level ? `CL-${{level}}` : '');
  document.getElementById('edit-priority').value = intake.priority || d.priority || 'Normal';
  document.getElementById('edit-capability').value = d.capability || sa.demand_capability || intake.demand_capability || '';
  document.getElementById('edit-location').value = (d.staffing_intake && d.staffing_intake.location) || d.location || '';
  // Status: ensure the value exists as an option, add dynamically if needed
  const statusSel = document.getElementById('edit-status');
  const rawStatus = d.status || d.role_status || 'pending';
  let found = false;
  for (const opt of statusSel.options) {{ if (opt.value === rawStatus) {{ found = true; break; }} }}
  if (!found) {{
    const opt = document.createElement('option');
    opt.value = rawStatus; opt.textContent = rawStatus;
    statusSel.appendChild(opt);
  }}
  statusSel.value = rawStatus;
  document.getElementById('edit-industry').value = _demandIndustry(d);
  document.getElementById('edit-notes').value = d.notes || '';
  document.getElementById('edit-overlay').classList.add('open');
}}

function _demandIndustry(d) {{
  return (d.skill_analysis && d.skill_analysis.demand_industry)
      || (d.staffing_intake && d.staffing_intake.industry)
      || d.industry || '';
}}

async function saveEdit() {{
  const roleId = document.getElementById('edit-role-id').value;
  const industry = document.getElementById('edit-industry').value.trim();
  const primarySkill = document.getElementById('edit-primary-skill').value.trim();
  const secondaryStr = document.getElementById('edit-secondary-skills').value.trim();
  const otherStr = document.getElementById('edit-other-skills').value.trim();
  const secondary = secondaryStr ? secondaryStr.split(',').map(s => s.trim()).filter(Boolean) : [];
  const other = otherStr ? otherStr.split(',').map(s => s.trim()).filter(Boolean) : [];
  // Read back inferred skills from the display field so we don't lose them on save
  const inferredStr = document.getElementById('edit-inferred-skills').value.trim();
  const inferred = (inferredStr && inferredStr !== '(none)')
    ? inferredStr.split(',').map(s => s.trim()).filter(Boolean) : [];
  const careerlevel = document.getElementById('edit-career-level').value.trim();
  const client = document.getElementById('edit-client').value.trim();
  const location = document.getElementById('edit-location').value.trim();
  const flatSkills = [primarySkill, ...secondary, ...other].filter(Boolean);
  const body = {{
    role_title: document.getElementById('edit-title').value.trim(),
    role: document.getElementById('edit-title').value.trim(),
    client: client,
    career_level: careerlevel,
    primary_skill: primarySkill,
    secondary_skills: secondary,
    other_skills: other,
    capability: document.getElementById('edit-capability').value.trim(),
    priority: document.getElementById('edit-priority').value,
    status: document.getElementById('edit-status').value,
    notes: document.getElementById('edit-notes').value.trim(),
    skill_analysis: {{
      primary_skill: primarySkill,
      secondary_skills: secondary,
      other_skills: other,
      inferred_skills: inferred,
      accenture_level: careerlevel,
      demand_industry: industry,
    }},
    staffing_intake: {{
      role: document.getElementById('edit-title').value.trim(),
      client: client,
      career_level: careerlevel,
      industry: industry,
      industry_level_1: industry,
      location: location,
      priority: document.getElementById('edit-priority').value,
      skills: flatSkills,
    }},
  }};
  try {{
    const res = await fetch(`/api/demands/${{encodeURIComponent(roleId)}}`, {{
      method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)
    }});
    if (!res.ok) {{ const e=await res.json(); throw new Error(e.detail||res.status); }}
    closeModal('edit-overlay');
    setStatus('Saved.');
    loadData();
  }} catch(e) {{
    setStatus('Save failed: ' + e.message);
  }}
}}

function askDelete(d) {{
  pendingDelete = d;
  document.getElementById('confirm-msg').textContent =
    `Delete demand "${{d.role_id}}" (${{d.role_title||d.role||''}})?  This cannot be undone.`;
  document.getElementById('confirm-overlay').classList.add('open');
}}

function closeConfirm() {{
  pendingDelete = null;
  document.getElementById('confirm-overlay').classList.remove('open');
}}

async function execDelete() {{
  if (!pendingDelete) return;
  const d = pendingDelete;
  closeConfirm();
  try {{
    const res = await fetch(`/api/demands/${{encodeURIComponent(d.role_id)}}`, {{method:'DELETE'}});
    if (!res.ok) {{ const e=await res.json(); throw new Error(e.detail||res.status); }}
    setStatus(`Deleted ${{d.role_id}}.`);
    loadData();
  }} catch(e) {{
    setStatus('Delete failed: ' + e.message);
  }}
}}

function esc(s) {{
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

// ── CSV Upload ─────────────────────────────────────
function ev(e, id, on) {{
  e.preventDefault();
  document.getElementById(id).classList.toggle('dragover', on);
}}
function onDrop(e, kind) {{
  e.preventDefault();
  const id = kind + '-drop';
  document.getElementById(id).classList.remove('dragover');
  const f = e.dataTransfer.files[0];
  if (f) uploadCsv(kind, null, f);
}}
async function uploadCsv(kind, input, file) {{
  const f = file || (input && input.files[0]);
  if (!f) return;
  const labelEl = document.getElementById(kind + '-drop-label');
  const resultEl = document.getElementById(kind + '-upload-result');
  labelEl.textContent = `Uploading ${{f.name}}…`;
  resultEl.className = 'upload-result';
  const fd = new FormData();
  fd.append('file', f);
  const endpoint = kind === 'bench' ? '/api/bench/upload-csv' : '/api/demands/upload-csv';
  try {{
    const res = await fetch(endpoint, {{ method: 'POST', body: fd }});
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {{
      const txt = await res.text();
      throw new Error(`Server error ${{res.status}}: ${{txt.slice(0,200)}}`);
    }}
    const init = await res.json();
    if (!res.ok) throw new Error(init.detail || res.status);
    const {{job_id, rows}} = init;
    labelEl.textContent = `Processing ${{f.name}} (${{rows}} rows)…`;
    resultEl.innerHTML = `<span style="color:var(--text-muted)">Processing in background… this page will update automatically.</span>`;
    resultEl.className = 'upload-result show';
    await pollJob(job_id, kind, f.name, labelEl, resultEl);
  }} catch(e) {{
    resultEl.innerHTML = `<span style="color:var(--red)">Upload failed: ${{esc(e.message)}}</span>`;
    resultEl.className = 'upload-result show';
    labelEl.textContent = 'Drag & drop a CSV here, or click to choose a file';
  }}
  if (input) input.value = '';
}}
async function pollJob(jobId, kind, fname, labelEl, resultEl) {{
  const POLL_MS = 2000;
  const MAX_WAIT = 1800000; // 30 min — large uploads with AI take time
  const started = Date.now();
  while (Date.now() - started < MAX_WAIT) {{
    await new Promise(r => setTimeout(r, POLL_MS));
    try {{
      const res = await fetch(`/api/upload-jobs/${{jobId}}`);
      const job = await res.json();
      // Show progress bar while running
      if (job.status === 'running' && job.total > 0) {{
        const pct = Math.round((job.progress / job.total) * 100);
        resultEl.innerHTML = `
          <div style="margin-bottom:6px;font-size:12px;color:var(--text-muted)">
            Processing row ${{job.progress}} of ${{job.total}}…
          </div>
          <div style="background:var(--border);border-radius:4px;height:8px;overflow:hidden">
            <div style="width:${{pct}}%;height:100%;background:var(--accent);border-radius:4px;transition:width .3s"></div>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px;text-align:right">${{pct}}%</div>`;
        resultEl.className = 'upload-result show';
      }}
      if (job.status === 'done') {{
        if (job.error) {{
          resultEl.innerHTML = `<span style="color:var(--red)">Processing failed: ${{esc(job.error)}}</span>`;
          labelEl.textContent = 'Drag & drop a CSV here, or click to choose a file';
        }} else {{
          const data = job.result || {{}};
          const addedList   = (data.added_names || data.added_ids   || []).slice(0,20);
          const updatedList = (data.updated_names || data.updated_ids || []).slice(0,20);
          resultEl.innerHTML = `
            <div class="summary-grid">
              <div class="summary-card added"><div class="num">${{data.added}}</div><div class="lbl">Added</div></div>
              <div class="summary-card updated"><div class="num">${{data.updated}}</div><div class="lbl">Updated</div></div>
              <div class="summary-card unchanged"><div class="num">${{data.unchanged}}</div><div class="lbl">Unchanged</div></div>
              <div class="summary-card skipped"><div class="num">${{data.skipped}}</div><div class="lbl">Skipped</div></div>
              ${{data.errors?.length ? `<div class="summary-card errors"><div class="num">${{data.errors.length}}</div><div class="lbl">Errors</div></div>` : ''}}
            </div>
            ${{(data.warnings?.length) ? `
              <div style="margin:10px 0 6px;background:#78350f33;border:1px solid #92400e88;border-radius:6px;padding:10px 14px">
                <strong style="font-size:11px;color:var(--yellow);display:block;margin-bottom:6px">
                  ⚠ ${{data.warnings.length}} field warning${{data.warnings.length>1?'s':''}} — some data is missing that affects matching quality:
                </strong>
                <ul style="margin:0;padding-left:18px;font-size:12px;color:var(--yellow);line-height:1.8">
                  ${{data.warnings.map(w => `<li>${{esc(w)}}</li>`).join('')}}
                </ul>
                <div style="font-size:11px;color:var(--text-muted);margin-top:6px">Please correct the CSV and re-upload to ensure accurate results.</div>
              </div>` : ''}}
            ${{addedList.length ? `<div style="margin-bottom:6px"><strong style="font-size:11px;color:var(--green)">NEW:</strong><div class="name-list">${{addedList.map(esc).join(', ')}}</div></div>` : ''}}
            ${{updatedList.length ? `<div style="margin-bottom:6px"><strong style="font-size:11px;color:var(--accent2)">UPDATED:</strong><div class="name-list">${{updatedList.map(esc).join(', ')}}</div></div>` : ''}}
            ${{data.errors?.length ? `<div><strong style="font-size:11px;color:var(--red)">ERRORS:</strong><div class="name-list">${{data.errors.map(esc).join('<br>')}}</div></div>` : ''}}
          `;
          labelEl.textContent = `${{fname}} — done. Drop another file or click to choose.`;
          loadData();
        }}
        resultEl.className = 'upload-result show';
        return;
      }}
    }} catch(_) {{ /* keep polling */ }}
  }}
  resultEl.innerHTML = `<span style="color:var(--yellow)">Still processing… refresh the page in a moment.</span>`;
  resultEl.className = 'upload-result show';
}}

loadData();
</script>
</body>
</html>"""


# ── Admin Page ────────────────────────────────────────────────────────────────

_ADMIN_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Admin | Staffing Platform</title>
  <style>
    {_CSS_BASE}
    .tabs{{display:flex;gap:0;border-bottom:2px solid var(--border);margin-bottom:24px;}}
    .tab{{padding:10px 22px;cursor:pointer;font-size:13px;font-weight:600;color:var(--text-muted);
          border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .2s;}}
    .tab:hover{{color:var(--text);}}
    .tab.active{{color:var(--accent2);border-bottom-color:var(--accent2);}}
    .tab-panel{{display:none;}}
    .tab-panel.active{{display:block;}}
    .action-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-bottom:24px;}}
    .action-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;}}
    .action-card h4{{font-size:14px;color:var(--accent2);margin-bottom:8px;}}
    .action-card p{{font-size:12px;color:var(--text-muted);margin-bottom:12px;line-height:1.5;}}
    .action-card .result{{font-size:12px;margin-top:8px;padding:8px;border-radius:6px;background:var(--bg);min-height:20px;}}
    .upload-section{{display:grid;grid-template-columns:1fr 1fr;gap:20px;}}
    @media(max-width:768px){{.upload-section{{grid-template-columns:1fr;}}}}
    /* ── Feature flag toggle switch ── */
    .flag-toggle{{position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0;}}
    .flag-toggle input{{opacity:0;width:0;height:0;}}
    .flag-slider{{position:absolute;cursor:pointer;inset:0;background:#374151;border-radius:24px;transition:.25s;}}
    .flag-slider:before{{content:"";position:absolute;height:18px;width:18px;left:3px;bottom:3px;
      background:#fff;border-radius:50%;transition:.25s;}}
    .flag-toggle input:checked + .flag-slider{{background:#22c55e;}}
    .flag-toggle input:checked + .flag-slider:before{{transform:translateX(20px);}}
  </style>
</head>
<body>
<header>
  <h1>⚙️ Admin</h1>
  <nav class="nav-links">
    <a href="/">Home</a>
    <a href="/report/dashboard">Dashboard</a>
    <a href="/manage/bench">Bench</a>
    <a href="/manage/demand">Demand</a>
    <a href="/logout" style="color:var(--red)">Logout</a>
  </nav>
</header>

<main>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('upload')">📥 Upload</div>
    <div class="tab" onclick="switchTab('system')">🔧 System</div>
    <div class="tab" onclick="switchTab('data')">📊 Data Summary</div>
  </div>

  <!-- ═══ UPLOAD TAB ═══ -->
  <div id="tab-upload" class="tab-panel active">
    <div class="upload-section">
      <div class="upload-panel">
        <h3>📋 Upload Demand CSV</h3>
        <div class="drop-zone" id="demand-drop" onclick="document.getElementById('demand-file-input').click()"
             ondragover="evDrag(event,'demand-drop',true)" ondragleave="evDrag(event,'demand-drop',false)"
             ondrop="onDrop(event,'demand')">
          <input type="file" id="demand-file-input" accept=".csv" onchange="uploadCsv('demand',this)">
          <span id="demand-drop-label">Drag &amp; drop demand CSV or click to choose</span>
        </div>
        <div class="upload-result" id="demand-upload-result"></div>
      </div>
      <div class="upload-panel">
        <h3>👥 Upload Bench CSV</h3>
        <div class="drop-zone" id="bench-drop" onclick="document.getElementById('bench-file-input').click()"
             ondragover="evDrag(event,'bench-drop',true)" ondragleave="evDrag(event,'bench-drop',false)"
             ondrop="onDrop(event,'bench')">
          <input type="file" id="bench-file-input" accept=".csv" onchange="uploadCsv('bench',this)">
          <span id="bench-drop-label">Drag &amp; drop bench CSV or click to choose</span>
        </div>
        <div class="upload-result" id="bench-upload-result"></div>
      </div>
    </div>
  </div>

  <!-- ═══ SYSTEM TAB ═══ -->
  <div id="tab-system" class="tab-panel">
    <div class="action-grid">
      <div class="action-card">
        <h4>🗑️ Clear Report Cache</h4>
        <p>Purge L1 (in-memory) and L2 (Cosmos/Blob) scoring cache. Next dashboard load will recompute all scores.</p>
        <button class="btn btn-danger" onclick="clearCache()">Clear Cache</button>
        <div class="result" id="cache-result"></div>
      </div>
      <div class="action-card">
        <h4>🔄 Refresh Dashboard Report</h4>
        <p>Force the dashboard to regenerate the matching report from current demand &amp; bench data.</p>
        <button class="btn btn-primary" onclick="refreshReport()">Refresh Report</button>
        <div class="result" id="report-result"></div>
      </div>
      <div class="action-card">
        <h4>💚 Health Check</h4>
        <p>Verify the server is running and responsive.</p>
        <button class="btn btn-primary" onclick="healthCheck()">Check Health</button>
        <div class="result" id="health-result"></div>
      </div>
      <div class="action-card">
        <h4>📧 Trigger Email Poll</h4>
        <p>Manually trigger one IMAP email poll cycle to check for new demand emails.</p>
        <button class="btn btn-primary" onclick="triggerPoll()">Poll Now</button>
        <div class="result" id="poll-result"></div>
      </div>
    </div>

    <!-- ── Feature Flags ── -->
    <h3 style="font-size:14px;color:var(--accent2);margin:24px 0 12px;border-bottom:1px solid var(--border);padding-bottom:8px">⚙️ Feature Flags</h3>
    <div class="action-grid" id="feature-flags-grid">
      <div style="color:var(--text-muted);font-size:13px">Loading flags…</div>
    </div>
  </div>

  <!-- ═══ DATA SUMMARY TAB ═══ -->
  <div id="tab-data" class="tab-panel">
    <div class="action-grid">
      <div class="action-card">
        <h4>📋 Demand Summary</h4>
        <p>Total open demands in the system.</p>
        <div class="result" id="demand-summary">Loading…</div>
      </div>
      <div class="action-card">
        <h4>👥 Bench Summary</h4>
        <p>Total candidates on the bench roster.</p>
        <div class="result" id="bench-summary">Loading…</div>
      </div>
      <div class="action-card">
        <h4>🗃️ Bulk Delete Demands</h4>
        <p>Delete ALL demand records. Use with caution — this cannot be undone.</p>
        <button class="btn btn-danger" onclick="bulkDelete('demands')">Delete All Demands</button>
        <div class="result" id="bulk-demand-result"></div>
      </div>
      <div class="action-card">
        <h4>🗃️ Bulk Delete Bench</h4>
        <p>Delete ALL bench candidates. Use with caution — this cannot be undone.</p>
        <button class="btn btn-danger" onclick="bulkDelete('bench')">Delete All Bench</button>
        <div class="result" id="bulk-bench-result"></div>
      </div>
    </div>
  </div>
</main>

<script>
function switchTab(name) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'data') loadSummaries();
  if (name === 'system') loadFeatureFlags();
}}

function esc(s) {{
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

// ── Feature Flags ───────────────────────────────────
async function loadFeatureFlags() {{
  const grid = document.getElementById('feature-flags-grid');
  if (!grid) return;
  try {{
    const res = await fetch('/api/admin/feature-flags');
    const flags = await res.json();
    grid.innerHTML = Object.entries(flags).map(([key, meta]) => `
      <div class="action-card" id="flag-card-${{key}}">
        <h4 style="display:flex;justify-content:space-between;align-items:center">
          <span>${{esc(meta.label)}}</span>
          <label class="flag-toggle" title="${{meta.value ? 'Disable' : 'Enable'}}">
            <input type="checkbox" id="flag-${{key}}" ${{meta.value ? 'checked' : ''}} onchange="setFeatureFlag('${{key}}', this.checked)">
            <span class="flag-slider"></span>
          </label>
        </h4>
        <p>${{esc(meta.description)}}</p>
        <div class="result" id="flag-result-${{key}}">
          <span style="color:${{meta.value ? 'var(--excellent)' : 'var(--nomatch)'}}">
            ${{meta.value ? '● Enabled' : '○ Disabled'}}
          </span>
        </div>
      </div>`).join('');
  }} catch(e) {{
    grid.innerHTML = `<div style="color:var(--red);font-size:13px">Failed to load flags: ${{esc(e.message)}}</div>`;
  }}
}}

async function setFeatureFlag(flag, value) {{
  const el = document.getElementById('flag-result-' + flag);
  if (el) el.innerHTML = '<span style="color:var(--text-muted)">Saving…</span>';
  try {{
    const res = await fetch('/api/admin/feature-flags/' + flag, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{value}})
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.status);
    if (el) el.innerHTML = `<span style="color:${{value ? 'var(--excellent)' : 'var(--nomatch)'}}">
      ${{value ? '● Enabled' : '○ Disabled'}} — saved
    </span>`;
  }} catch(e) {{
    if (el) el.innerHTML = `<span style="color:var(--red)">Error: ${{esc(e.message)}}</span>`;
    // Revert checkbox
    const cb = document.getElementById('flag-' + flag);
    if (cb) cb.checked = !value;
  }}
}}

// ── Upload ─────────────────────────────────────────
function evDrag(e, id, on) {{
  e.preventDefault();
  document.getElementById(id).classList.toggle('dragover', on);
}}
function onDrop(e, kind) {{
  e.preventDefault();
  document.getElementById(kind + '-drop').classList.remove('dragover');
  const f = e.dataTransfer.files[0];
  if (f) uploadCsv(kind, null, f);
}}
async function uploadCsv(kind, input, file) {{
  const f = file || (input && input.files[0]);
  if (!f) return;
  const labelEl = document.getElementById(kind + '-drop-label');
  const resultEl = document.getElementById(kind + '-upload-result');
  labelEl.textContent = `Uploading ${{f.name}}…`;
  resultEl.className = 'upload-result';
  const fd = new FormData();
  fd.append('file', f);
  const endpoint = kind === 'bench' ? '/api/bench/upload-csv' : '/api/demands/upload-csv';
  try {{
    const res = await fetch(endpoint, {{ method: 'POST', body: fd }});
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('application/json')) throw new Error(`Server error ${{res.status}}`);
    const init = await res.json();
    if (!res.ok) throw new Error(init.detail || res.status);
    labelEl.textContent = `Processing ${{f.name}} (${{init.rows}} rows)…`;
    resultEl.innerHTML = `<span style="color:var(--text-muted)">Processing in background…</span>`;
    resultEl.className = 'upload-result show';
    await pollJob(init.job_id, kind, f.name, labelEl, resultEl);
  }} catch(e) {{
    resultEl.innerHTML = `<span style="color:var(--red)">Upload failed: ${{esc(e.message)}}</span>`;
    resultEl.className = 'upload-result show';
    labelEl.textContent = 'Drag & drop CSV or click to choose';
  }}
  if (input) input.value = '';
}}
async function pollJob(jobId, kind, fname, labelEl, resultEl) {{
  const POLL_MS = 2000;
  const MAX_WAIT = 1800000; // 30 min
  const started = Date.now();
  while (Date.now() - started < MAX_WAIT) {{
    await new Promise(r => setTimeout(r, POLL_MS));
    try {{
      const res = await fetch(`/api/upload-jobs/${{jobId}}`);
      const job = await res.json();
      if (job.status === 'running' && job.total > 0) {{
        const pct = Math.round((job.progress / job.total) * 100);
        const elapsed = Math.round((Date.now() - started) / 1000);
        const eta = pct > 0 ? Math.round(elapsed / pct * (100 - pct)) : '…';
        resultEl.innerHTML = `
          <div style="margin-bottom:6px;font-size:12px;color:var(--text-muted)">
            Processing row ${{job.progress}} of ${{job.total}} · ${{elapsed}}s elapsed · ~${{eta}}s remaining
          </div>
          <div style="background:var(--border);border-radius:4px;height:8px;overflow:hidden">
            <div style="width:${{pct}}%;height:100%;background:var(--accent);border-radius:4px;transition:width .3s"></div>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px;text-align:right">${{pct}}%</div>`;
        resultEl.className = 'upload-result show';
      }}
      if (job.status === 'done') {{
        if (job.error) {{
          resultEl.innerHTML = `<span style="color:var(--red)">Failed: ${{esc(job.error)}}</span>`;
          labelEl.textContent = 'Drag & drop CSV or click to choose';
        }} else {{
          const d = job.result || {{}};
          resultEl.innerHTML = `
            <div class="summary-grid">
              <div class="summary-card added"><div class="num">${{d.added}}</div><div class="lbl">Added</div></div>
              <div class="summary-card updated"><div class="num">${{d.updated}}</div><div class="lbl">Updated</div></div>
              <div class="summary-card unchanged"><div class="num">${{d.unchanged}}</div><div class="lbl">Unchanged</div></div>
              <div class="summary-card skipped"><div class="num">${{d.skipped}}</div><div class="lbl">Skipped</div></div>
              ${{d.errors?.length ? `<div class="summary-card errors"><div class="num">${{d.errors.length}}</div><div class="lbl">Errors</div></div>` : ''}}
            </div>
            ${{(d.warnings?.length) ? `
              <div style="margin:10px 0 6px;background:#78350f33;border:1px solid #92400e88;border-radius:6px;padding:10px 14px">
                <strong style="font-size:11px;color:var(--yellow);display:block;margin-bottom:6px">
                  ⚠ ${{d.warnings.length}} warning${{d.warnings.length>1?'s':''}}
                </strong>
                <ul style="margin:0;padding-left:18px;font-size:12px;color:var(--yellow);line-height:1.8">
                  ${{d.warnings.map(w => `<li>${{esc(w)}}</li>`).join('')}}
                </ul>
              </div>` : ''}}
            ${{d.errors?.length ? `<div><strong style="font-size:11px;color:var(--red)">ERRORS:</strong><div class="name-list">${{d.errors.map(esc).join('<br>')}}</div></div>` : ''}}
          `;
          labelEl.textContent = `${{fname}} — done.`;
        }}
        resultEl.className = 'upload-result show';
        return;
      }}
    }} catch(_) {{}}
  }}
  resultEl.innerHTML = `<span style="color:var(--yellow)">Still processing… check back shortly.</span>`;
  resultEl.className = 'upload-result show';
}}

// ── System Actions ─────────────────────────────────
async function clearCache() {{
  const el = document.getElementById('cache-result');
  el.textContent = 'Clearing…';
  try {{
    const res = await fetch('/api/report/cache/clear', {{method:'POST'}});
    const data = await res.json();
    el.innerHTML = `<span style="color:var(--green)">✓ Cache cleared. L1=${{data.cleared_l1 ?? '?'}}, L2=${{data.cleared_l2 ?? '?'}}</span>`;
  }} catch(e) {{
    el.innerHTML = `<span style="color:var(--red)">Failed: ${{esc(e.message)}}</span>`;
  }}
}}

async function refreshReport() {{
  const el = document.getElementById('report-result');
  el.textContent = 'Generating report (this may take a minute)…';
  try {{
    const res = await fetch('/api/report/bench-matches?refresh=true');
    const data = await res.json();
    const n = data.demands?.length ?? 0;
    el.innerHTML = `<span style="color:var(--green)">✓ Report generated: ${{n}} demands scored.</span>`;
  }} catch(e) {{
    el.innerHTML = `<span style="color:var(--red)">Failed: ${{esc(e.message)}}</span>`;
  }}
}}

async function healthCheck() {{
  const el = document.getElementById('health-result');
  el.textContent = 'Checking…';
  try {{
    const res = await fetch('/health');
    const data = await res.json();
    el.innerHTML = `<span style="color:var(--green)">✓ ${{data.status}} — v${{data.version ?? '?'}}</span>`;
  }} catch(e) {{
    el.innerHTML = `<span style="color:var(--red)">Failed: ${{esc(e.message)}}</span>`;
  }}
}}

async function triggerPoll() {{
  const el = document.getElementById('poll-result');
  el.textContent = 'Polling…';
  try {{
    const res = await fetch('/api/poll', {{method:'POST'}});
    const data = await res.json();
    el.innerHTML = `<span style="color:var(--green)">✓ ${{data.message || data.status}}</span>`;
  }} catch(e) {{
    el.innerHTML = `<span style="color:var(--red)">Failed: ${{esc(e.message)}}</span>`;
  }}
}}

// ── Data Summary ─────────────────────────────────
async function loadSummaries() {{
  try {{
    const [dRes, bRes] = await Promise.all([
      fetch('/api/demands?page_size=1'),
      fetch('/api/bench?page_size=1'),
    ]);
    const dData = await dRes.json();
    const bData = await bRes.json();
    document.getElementById('demand-summary').innerHTML =
      `<strong style="font-size:24px;color:var(--accent2)">${{dData.pagination?.total ?? '?'}}</strong> demands`;
    document.getElementById('bench-summary').innerHTML =
      `<strong style="font-size:24px;color:var(--accent2)">${{bData.pagination?.total ?? '?'}}</strong> candidates`;
  }} catch(e) {{
    document.getElementById('demand-summary').textContent = 'Error loading';
    document.getElementById('bench-summary').textContent = 'Error loading';
  }}
}}

async function bulkDelete(kind) {{
  if (!confirm(`Are you sure you want to delete ALL ${{kind}}? This cannot be undone.`)) return;
  const el = document.getElementById('bulk-' + (kind === 'demands' ? 'demand' : 'bench') + '-result');
  el.textContent = 'Deleting…';
  try {{
    const res = await fetch(`/api/${{kind}}/all`, {{method:'DELETE'}});
    if (!res.ok) {{
      const err = await res.json().catch(() => ({{}}));
      throw new Error(err.detail || `HTTP ${{res.status}}`);
    }}
    const data = await res.json();
    el.innerHTML = `<span style="color:var(--green)">✓ Deleted ${{data.deleted}} ${{kind}}.</span>`;
    loadSummaries();
  }} catch(e) {{
    el.innerHTML = `<span style="color:var(--red)">Failed: ${{esc(e.message)}}</span>`;
  }}
}}
</script>
</body>
</html>"""


# ── HTML route handlers ───────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_html_auth)])
async def landing_page() -> HTMLResponse:
    """Staffing Platform landing page."""
    return HTMLResponse(_LANDING_HTML, media_type="text/html; charset=utf-8")


@router.get("/manage/bench", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_html_auth)])
async def manage_bench_page() -> HTMLResponse:
    """Bench roster management UI."""
    return HTMLResponse(_BENCH_HTML, media_type="text/html; charset=utf-8")


@router.get("/manage/demand", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_html_auth)])
async def manage_demand_page() -> HTMLResponse:
    """Demand list management UI."""
    return HTMLResponse(_DEMAND_HTML, media_type="text/html; charset=utf-8")


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_html_auth)])
async def admin_page() -> HTMLResponse:
    """Admin panel — uploads, cache, system controls."""
    return HTMLResponse(_ADMIN_HTML, media_type="text/html; charset=utf-8")
