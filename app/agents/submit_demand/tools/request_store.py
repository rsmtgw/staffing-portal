"""
tools/request_store.py
----------------------
Central store for all staffing requests backed by Azure Cosmos DB.

Each request is assigned a unique role ID (ROLE-001, ROLE-002, …) when saved
by the StaffingIntakeAgent pipeline.  The ArtifactAgent and RoleFulfillmentAgent
read from and write to this same store so the full lifecycle of every request
is tracked in one place.
"""

from __future__ import annotations

import json
from datetime import datetime
import asyncio

from app.agents.submit_demand.tools.cosmos_store import (
    load_requests as _load,
    upsert_request as _cosmos_upsert_request,
    load_request_by_id as _cosmos_load_by_id,
)


def _next_role_id(requests: list) -> str:
    nums = []
    for r in requests:
        rid = r.get("role_id", "")
        if rid.startswith("ROLE-"):
            try:
                nums.append(int(rid.split("-")[1]))
            except (IndexError, ValueError):
                pass
    return f"ROLE-{(max(nums, default=0) + 1):03d}"


def _extract_request_description(request_doc: dict) -> str:
    """Return the best available role description from Cosmos-style demand documents."""
    intake = request_doc.get("staffing_intake") or {}
    for candidate in (
        request_doc.get("role_description"),
        request_doc.get("description"),
        intake.get("description"),
        intake.get("role_description"),
        intake.get("notes"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


# ---------------------------------------------------------------------------
# Public tools (exposed to agents)
# ---------------------------------------------------------------------------

def save_staffing_request(staffing_intake: str, validation_status: str) -> dict:
    """
    Save a new staffing request and assign it a unique role ID.

    Called automatically by the intake pipeline after every successful
    Parser + Validator run.

    Returns:
        {"role_id": "ROLE-001", "status": "saved", "message": "..."}
    """
    requests = _load()
    role_id = _next_role_id(requests)

    intake_data = json.loads(staffing_intake) if isinstance(staffing_intake, str) else staffing_intake
    validation_data = json.loads(validation_status) if isinstance(validation_status, str) else validation_status

    entry = {
        "role_id": role_id,
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "staffing_intake": intake_data,
        "validation_status": validation_data,
        "fulfilled_at": None,
        "fulfilled_by": None,
        "fulfillment_notes": None,
    }
    _cosmos_upsert_request(entry)
    return {
        "role_id": role_id,
        "status": "saved",
        "message": f"Staffing request saved as {role_id}. Open artifact_agent to generate the email.",
    }


# Wrapper to always trigger skill analysis after saving a new demand
def save_staffing_request_with_skill_analysis(staffing_intake: str, validation_status: str) -> dict:
    result = save_staffing_request(staffing_intake, validation_status)
    role_id = result.get("role_id")
    if role_id:
        try:
            asyncio.get_running_loop()  # raises RuntimeError when no loop is active
            asyncio.ensure_future(_analyze_with_retry(role_id))
        except RuntimeError:
            # No running event loop (e.g. called from a sync test / CLI script)
            import threading
            def _run():
                asyncio.run(_analyze_with_retry(role_id))
            threading.Thread(target=_run, daemon=True).start()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "[save_staffing_request_with_skill_analysis] Could not schedule skill analysis for %s: %s",
                role_id, exc, exc_info=True,
            )
    return result


def get_pending_requests() -> dict:
    """
    Return all pending (unfulfilled) staffing requests.

    Returns a summary list — use get_request_by_role_id() to get full details.

    Returns:
        {
          "status": "ok" | "empty",
          "count": int,
          "requests": [{"role_id", "role", "location", "created_at", "readiness"}, ...]
        }
    """
    requests = _load()
    pending = [r for r in requests if r.get("status") == "pending"]
    return {
        "status": "ok" if pending else "empty",
        "count": len(pending),
        "requests": [
            {
                "role_id": r["role_id"],
                "role": r["staffing_intake"].get("role"),
                "location": r["staffing_intake"].get("location"),
                "created_at": r["created_at"],
                "readiness": r["validation_status"].get("readiness"),
            }
            for r in pending
        ],
    }


def get_request_by_role_id(role_id: str) -> dict:
    """
    Return the full details for a specific role ID, including intake data,
    validation status, and fulfillment info (if already fulfilled).

    Args:
        role_id: The role ID to look up, e.g. "ROLE-001".

    Returns:
        {"status": "ok", "request": {...}} or {"status": "not_found", "message": "..."}
    """
    r = _cosmos_load_by_id(role_id)
    if r:
        return {"status": "ok", "request": r}
    return {"status": "not_found", "message": f"No request found with role_id={role_id}"}


def mark_role_fulfilled(role_id: str, fulfilled_by: str, notes: str = "") -> dict:
    """
    Mark a staffing request as fulfilled.

    Called by the RoleFulfillmentAgent when a candidate has been placed.
    The ArtifactAgent will reflect the fulfilled status in any subsequent
    email or weekly status generated for this role.

    Args:
        role_id:      Role ID to mark fulfilled, e.g. "ROLE-001".
        fulfilled_by: Name of the candidate who was placed.
        notes:        Optional notes (start date, onboarding details, etc.).

    Returns:
        {"status": "ok", "role_id": "...", "message": "..."} or {"status": "not_found", ...}
    """
    requests = _load()
    for r in requests:
        if r.get("role_id") == role_id:
            r["status"] = "fulfilled"
            r["fulfilled_at"] = datetime.now().isoformat(timespec="seconds")
            r["fulfilled_by"] = fulfilled_by
            r["fulfillment_notes"] = notes
            _cosmos_upsert_request(r)
            return {
                "status": "ok",
                "role_id": role_id,
                "message": f"{role_id} marked as fulfilled — placed: {fulfilled_by}.",
            }
    return {"status": "not_found", "message": f"No request found with role_id={role_id}"}


def save_partial_request(staffing_intake: str, clarification_questions: list[str], sender_email: str = "") -> dict:
    """
    Save a partial staffing request awaiting clarification from the contact.

    Called when required fields are missing and a clarification email has been
    sent to the primary_contact. Status is set to "awaiting_clarification".

    Args:
        staffing_intake:          JSON string of fields extracted so far (some may be null).
        clarification_questions:  List of question strings.
        sender_email:             Email address of the original demand sender.

    Returns:
        {"role_id": "ROLE-001", "status": "awaiting_clarification", "message": "..."}
    """
    requests = _load()
    role_id = _next_role_id(requests)

    intake_data = json.loads(staffing_intake) if isinstance(staffing_intake, str) else staffing_intake
    questions = clarification_questions if isinstance(clarification_questions, list) else list(clarification_questions)

    entry = {
        "role_id": role_id,
        "status": "awaiting_clarification",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sender_email": sender_email,
        "staffing_intake": intake_data,
        "validation_status": {
            "readiness": "NOT_READY",
            "missing_fields": intake_data.get("missing_fields", []),
            "clarification_questions": questions,
        },
        "fulfilled_at": None,
        "fulfilled_by": None,
        "fulfillment_notes": None,
    }
    _cosmos_upsert_request(entry)
    return {
        "role_id": role_id,
        "status": "awaiting_clarification",
        "message": f"Partial request saved as {role_id}. Awaiting clarification reply.",
    }


async def analyze_demand_skills(role_id: str) -> dict:
    """
    Analyse a saved staffing demand and produce a structured skill breakdown.

    Uses Gemini to infer must_have_skills, nice_to_have_skills,
    seniority_indicator, and accenture_level, then saves the result back
    onto the Cosmos demand document under ``skill_analysis``.

    Also produces Matchmaker-ready fields:
      - primary_skill    : must_have_skills[0] — the single most important requirement
      - secondary_skills : must_have_skills[1:] — explicitly required but not primary
      - other_skills     : nice_to_have_skills — beneficial but not mandatory
      - inferred_skills  : skills extracted from free-text role description that were
                           not already in primary/secondary/other. Each has a
                           {skill, proficiency, proficiency_label} structure with
                           proficiency assigned by career level.

    Args:
        role_id: The ROLE-NNN demand to analyse (must already exist in Cosmos).

    Returns:
        {"status": "ok", "role_id": "...", "skill_analysis": {...}}
        or {"status": "not_found" | "error", "message": "..."}
    """
    import os
    import re as _re
    import json as _json
    import logging
    from google import genai

    logger = logging.getLogger(__name__)
    logger.info(f"[analyze_demand_skills] Called for role_id={role_id}")

    r = _cosmos_load_by_id(role_id)
    if r is None:
        logger.warning(f"[analyze_demand_skills] No request found with role_id={role_id}")
        return {"status": "not_found", "message": f"No request found with role_id={role_id}"}

    intake    = r.get("staffing_intake", {})
    role      = intake.get("role", "")
    skills    = intake.get("skills", [])
    exp_years = intake.get("experience_years") or ""
    description = _extract_request_description(r)

    logger.info(f"[analyze_demand_skills] Intake: role={role}, skills={skills}, exp_years={exp_years}")

    # ── Step 1: Structured skill expansion (existing logic) ──────────────────
    prompt = (
        "You are a staffing expert. Analyse the following demand and return ONLY valid JSON.\n\n"
        f"Role: {role}\n"
        f"Required skills mentioned: {', '.join(skills) if isinstance(skills, list) else skills}\n"
        f"Experience required: {exp_years} years\n\n"
        "If the role or skills are generic (e.g. 'Full stack', 'Java'), EXPAND to include all typical frameworks, libraries, and technologies required for such a role.\n"
        "For example, if the role is 'Java Full Stack Developer', must-have skills should include Java, Spring, J2EE, REST, React, JavaScript, SQL, HTML, CSS, and related tools.\n"
        "If the role is 'Python Developer', expand to include Django/Flask, REST, SQL, etc.\n"
        "If the role is 'Frontend Developer', expand to React, Angular, JavaScript, HTML, CSS, etc.\n"
        "Infer and list all must-have and nice-to-have skills that a strong candidate for this role would be expected to have, even if not explicitly mentioned.\n"
        "Return a JSON object with exactly these fields:\n"
        "{\n"
        '  "must_have_skills": ["list of absolutely required skills for this role"],\n'
        '  "nice_to_have_skills": ["skills beneficial but not mandatory"],\n'
        '  "seniority_indicator": "Junior | Mid | Senior | Lead | Principal",\n'
        '  "accenture_level": "CL-X (e.g. CL-9, CL-10, CL-11) — typical Accenture level. null if unknown"\n'
        "}\n"
        "Return ONLY the JSON object — no markdown, no explanation."
    )

    logger.debug(f"[analyze_demand_skills] Prompt sent to Azure gpt-4.1: {prompt}")

    try:
        from openai import AsyncAzureOpenAI as _AsyncAzureOpenAI
        _az_endpoint = os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "https://openai-interviewassists.openai.azure.com/")
        _az_key      = os.environ.get("AZURE_AI_FOUNDRY_KEY", "")
        _az_version  = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        _chat_model  = os.environ.get("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")

        if not _az_key:
            logger.warning("[azure-skill-infer] AZURE_AI_FOUNDRY_KEY not set — skill analysis will fail")

        logger.info(
            "[azure-skill-infer] calling %s for skill expansion | role_id=%s endpoint=%s",
            _chat_model, role_id, _az_endpoint,
        )
        _az = _AsyncAzureOpenAI(api_version=_az_version, azure_endpoint=_az_endpoint, api_key=_az_key)
        resp = await _az.chat.completions.create(
            model=_chat_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        logger.info("[azure-skill-infer] skill expansion response OK for role_id=%s", role_id)
        raw = (resp.choices[0].message.content or "").strip()
        logger.debug(f"[azure-skill-infer] Raw response: {raw}")
        b1, b2 = raw.find("{"), raw.rfind("}")
        analysis: dict = _json.loads(raw[b1: b2 + 1]) if b1 != -1 and b2 > b1 else {}
        logger.info(f"[azure-skill-infer] Parsed skill analysis: {analysis}")
    except Exception as exc:
        logger.error(f"[azure-skill-infer] Skill analysis FAILED for {role_id}: {exc}", exc_info=True)
        return {"status": "error", "message": f"Skill analysis failed: {exc}"}

    # ── Step 2: Build Matchmaker-ready structured fields ─────────────────────
    must_have  = analysis.get("must_have_skills") or []
    nice_have  = analysis.get("nice_to_have_skills") or []
    cl_raw     = analysis.get("accenture_level") or ""

    analysis["primary_skill"]    = must_have[0] if must_have else ""
    analysis["secondary_skills"] = must_have[1:] if len(must_have) > 1 else []
    analysis["other_skills"]     = nice_have

    # ── Step 3: Demand enrichment — infer hidden skills from description text ─
    # Determine proficiency to assign to inferred skills based on career level.
    # CL-6/7 → P4, CL-8 → P4, CL-9 → P3, CL-10/11+ → P2 (mirrors matchmaker-explainer)
    _PROF_LABELS = {1: "P1 - Beginner", 2: "P2 - Intermediate", 3: "P3 - Advanced", 4: "P4 - Expert"}
    _cl_m = _re.search(r"CL[- ]?(\d{1,2})", cl_raw, _re.IGNORECASE)
    _cl_num = int(_cl_m.group(1)) if _cl_m else 10
    if _cl_num <= 8:
        _inferred_prof = 4
    elif _cl_num == 9:
        _inferred_prof = 3
    else:
        _inferred_prof = 2

    description_text = description or role  # fall back to role title if no description
    inferred_skills: list[dict] = []
    logger.debug(
        "[azure-skill-infer] Using description for Azure prompt | role_id=%s | chars=%d | preview=%s",
        role_id,
        len(description_text),
        description_text[:1000],
    )
    if len(description_text.strip()) >= 50:
        # Build set of already-explicit skills (lowercase) for deduplication
        _explicit = {s.lower() for s in (must_have + nice_have)}
        _infer_prompt = (
            "You are a Technical Recruiter. Extract a list of relevant TECHNICAL skills, tools, and platforms "
            "from the Job Description text below.\n"
            "Ignore generic terms like 'Communication', 'Leadership', 'Problem Solving', 'SDLC', 'Agile'.\n"
            "Focus on specific technologies (e.g. 'Kafka', 'AWS', 'Python', 'Kubernetes').\n"
            "Output ONLY a JSON list of strings. Example: [\"Java\", \"Spring Boot\", \"AWS\"]\n\n"
            f"Role Title: {role}\n"
            f"Description:\n{description_text[:3000]}\n\n"
            "Extract TECHNICAL skills (required, recommended, and nice-to-have).\n"
            "Do not include extremely generic terms (e.g. 'Software Development').\n"
            "Return a JSON object with key \"skills\": {\"skills\": [\"Java\", \"Redis\", \"Kafka\"]}"
        )
        try:
            logger.info("[azure-skill-infer] calling %s for skill inference from description | role_id=%s", _chat_model, role_id)
            _infer_resp = await _az.chat.completions.create(
                model=_chat_model,
                messages=[{"role": "user", "content": _infer_prompt}],
                temperature=0.0,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            _infer_raw = (_infer_resp.choices[0].message.content or "").strip()
            _extracted: list = _json.loads(_infer_raw).get("skills", [])
            for _skill in _extracted:
                if isinstance(_skill, str) and _skill.strip() and _skill.strip().lower() not in _explicit:
                    inferred_skills.append({
                        "skill": _skill.strip(),
                        "proficiency": _inferred_prof,
                        "proficiency_label": _PROF_LABELS[_inferred_prof],
                    })
                    _explicit.add(_skill.strip().lower())
            logger.info("[azure-skill-infer] inferred %d additional skills from description | role_id=%s", len(inferred_skills), role_id)
        except Exception as _exc:
            logger.warning(f"[analyze_demand_skills] Demand enrichment (inferred skills) failed: {_exc}")

    analysis["inferred_skills"] = inferred_skills

    r["skill_analysis"]    = analysis
    r["skill_analysis_at"] = datetime.now().isoformat(timespec="seconds")
    logger.info(f"[analyze_demand_skills] Saving skill_analysis to Cosmos for role_id={role_id}")
    _cosmos_upsert_request(r)

    return {"status": "ok", "role_id": role_id, "skill_analysis": analysis}


async def extract_inferred_skills_from_description(role_id: str) -> dict:
    """
    Extract inferred skills ONLY from role_description and save to inferred_skills field.
    
    This is a focused extraction that ONLY looks at the role_description text and extracts
    technical skills, then saves them to the inferred_skills field without modifying
    other_skills or primary/secondary skills.
    
    Used during matching to ensure role_description skills are captured as inferred_skills.
    
    Args:
        role_id: The ROLE-NNN demand to process
        
    Returns:
        {"status": "ok", "role_id": "...", "inferred_skills": [...], "count": N}
        or {"status": "not_found" | "error", "message": "..."}
    """
    import os
    import json as _json
    import logging
    from datetime import datetime
    
    logger = logging.getLogger(__name__)
    logger.info(f"[extract_inferred_from_desc] Called for role_id={role_id}")
    
    r = _cosmos_load_by_id(role_id)
    if r is None:
        logger.warning(f"[extract_inferred_from_desc] No request found with role_id={role_id}")
        return {"status": "not_found", "message": f"No request found with role_id={role_id}"}
    
    # Get description
    role_description = r.get("role_description") or ""
    if not role_description or len(role_description.strip()) < 50:
        logger.warning(f"[extract_inferred_from_desc] role_description too short or missing for {role_id}")
        return {"status": "ok", "role_id": role_id, "inferred_skills": [], "count": 0}
    
    # Get existing skill_analysis to avoid duplicating already-extracted skills
    skill_analysis = r.get("skill_analysis") or {}
    existing_inferred = skill_analysis.get("inferred_skills") or []
    
    # If inferred skills already exist, don't re-extract
    if existing_inferred:
        logger.info(f"[extract_inferred_from_desc] inferred_skills already exist ({len(existing_inferred)} items) for {role_id} — skipping extraction")
        return {"status": "ok", "role_id": role_id, "inferred_skills": existing_inferred, "count": len(existing_inferred), "source": "existing"}
    
    # Extract from explicit fields to avoid duplicating
    must_have = skill_analysis.get("must_have_skills") or skill_analysis.get("primary_skill", "")
    if isinstance(must_have, str):
        must_have = [must_have] if must_have else []
    nice_have = skill_analysis.get("nice_to_have_skills") or skill_analysis.get("other_skills") or []
    
    _explicit = {s.lower().strip() for s in (must_have + nice_have) if s}
    
    logger.info(f"[extract_inferred_from_desc] Extracting from description for {role_id} | chars={len(role_description)} | explicit_skills={len(_explicit)}")
    
    try:
        from openai import AsyncAzureOpenAI as _AsyncAzureOpenAI
        _az_endpoint = os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "https://openai-interviewassists.openai.azure.com/")
        _az_key      = os.environ.get("AZURE_AI_FOUNDRY_KEY", "")
        _az_version  = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        _chat_model  = os.environ.get("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")
        
        if not _az_key:
            logger.warning("[extract_inferred_from_desc] AZURE_AI_FOUNDRY_KEY not set")
            return {"status": "error", "message": "Azure API key not configured"}
        
        _az = _AsyncAzureOpenAI(api_version=_az_version, azure_endpoint=_az_endpoint, api_key=_az_key)
        
        # Prompt to extract technical skills from description
        _infer_prompt = (
            "You are a Technical Recruiter. Extract ONLY specific technical skills, tools, and platforms "
            "from the Job Description text below.\n"
            "Ignore: generic terms (Communication, Leadership, Problem Solving, SDLC, Agile, Software Development, etc.)\n"
            "Include: specific technologies (e.g. Kafka, AWS, Python, Kubernetes, React, Spring Boot, etc.)\n"
            "Output ONLY a JSON object with key 'skills' containing an array of strings.\n\n"
            f"Job Description:\n{role_description[:3000]}\n\n"
            "Return valid JSON: {\"skills\": [\"Skill1\", \"Skill2\", ...]}"
        )
        
        logger.info(f"[extract_inferred_from_desc] Calling {_chat_model} for description extraction | {role_id}")
        _infer_resp = await _az.chat.completions.create(
            model=_chat_model,
            messages=[{"role": "user", "content": _infer_prompt}],
            temperature=0.0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        
        _infer_raw = (_infer_resp.choices[0].message.content or "").strip()
        logger.debug(f"[extract_inferred_from_desc] LLM response: {_infer_raw[:500]}")
        
        _extracted: list = _json.loads(_infer_raw).get("skills", [])
        
        # Build inferred skills list, deduplicating against explicit skills
        inferred_skills: list[dict] = []
        for _skill in _extracted:
            if isinstance(_skill, str) and _skill.strip():
                _skill_normalized = _skill.strip().lower()
                if _skill_normalized not in _explicit:
                    inferred_skills.append({
                        "skill": _skill.strip(),
                        "proficiency": 2,  # P2 - Intermediate (conservative default)
                        "proficiency_label": "P2 - Intermediate",
                    })
                    _explicit.add(_skill_normalized)
        
        logger.info(f"[extract_inferred_from_desc] Extracted {len(inferred_skills)} inferred skills for {role_id}")
        
        # Save back to Cosmos
        if not skill_analysis:
            skill_analysis = {}
        skill_analysis["inferred_skills"] = inferred_skills
        r["skill_analysis"] = skill_analysis
        r["skill_analysis_at"] = datetime.now().isoformat(timespec="seconds")
        
        logger.info(f"[extract_inferred_from_desc] Saving {len(inferred_skills)} inferred skills to Cosmos for {role_id}")
        _cosmos_upsert_request(r)
        
        return {"status": "ok", "role_id": role_id, "inferred_skills": inferred_skills, "count": len(inferred_skills), "source": "extracted"}
        
    except Exception as exc:
        logger.error(f"[extract_inferred_from_desc] Extraction failed for {role_id}: {exc}", exc_info=True)
        return {"status": "error", "message": f"Extraction failed: {exc}"}


async def batch_extract_inferred_skills(role_ids: list[str], batch_size: int = 10, progress_callback=None) -> dict:
    """
    Batch extract inferred skills from multiple demands in ONE or MORE Azure LLM calls.

    Splits large role_ids lists into smaller batches (default: 10 per call) to avoid:
    - Exceeding token limits
    - Long unresponsive periods that trigger client timeouts
    
    Each batch gets 1 Azure LLM call. This is still 5-10x faster than individual calls.

    Flow:
      1. Split role_ids into batches of batch_size
      2. For each batch:
         a. Load demands with role_descriptions
         b. Call Azure GPT-4.1 ONCE per batch
         c. Parse results
         d. Save each back to Cosmos
         e. Call progress_callback if provided
      3. Aggregate results across all batches

    Args:
        role_ids: List of role IDs to batch process
        batch_size: Max role_ids per LLM call (default: 10)
        progress_callback: Optional callable(processed, total) for progress updates

    Returns:
        {
            "status": "ok",
            "total": N,
            "processed": M,
            "skipped": K,
            "failed": [role_id, ...],
            "message": "..."
        }
    """
    import os
    import json as _json
    import logging
    from openai import AsyncAzureOpenAI as _AsyncAzureOpenAI

    logger = logging.getLogger(__name__)
    logger.info(f"[batch-extract-infer] Starting batch extraction for {len(role_ids)} role(s) | batch_size={batch_size}")

    if not role_ids:
        return {"status": "ok", "total": 0, "processed": 0, "skipped": 0, "failed": [], "message": "No role_ids provided"}

    # Test Cosmos connectivity early
    try:
        _test_load = _cosmos_load_by_id(role_ids[0])
        if _test_load is None:
            logger.warning(f"[batch-extract-infer] Could not load first role {role_ids[0]} — Cosmos may be unreachable")
            return {
                "status": "error",
                "total": len(role_ids),
                "processed": 0,
                "skipped": 0,
                "failed": role_ids,
                "message": "Could not reach Cosmos DB — extraction skipped",
            }
    except Exception as _cosmos_exc:
        logger.error(f"[batch-extract-infer] Cosmos connectivity check failed: {_cosmos_exc}")
        return {
            "status": "error",
            "total": len(role_ids),
            "processed": 0,
            "skipped": 0,
            "failed": role_ids,
            "message": f"Cosmos connectivity error: {_cosmos_exc}",
        }

    # Early return if all demands already have inferred_skills (don't waste LLM call)
    _preflight_to_process = []
    _preflight_skip = 0
    for idx, rid in enumerate(role_ids):
        # Skip first one since we already tested it above
        if idx == 0:
            r = _test_load
        else:
            r = _cosmos_load_by_id(rid)
        
        if r and r.get("role_description") and not r.get("skill_analysis", {}).get("inferred_skills"):
            _preflight_to_process.append(rid)
        else:
            _preflight_skip += 1
    
    if not _preflight_to_process:
        logger.info(f"[batch-extract-infer] All {len(role_ids)} demands already have inferred_skills — skipping extraction")
        return {
            "status": "ok",
            "total": len(role_ids),
            "processed": 0,
            "skipped": len(role_ids),
            "failed": [],
            "message": f"All {len(role_ids)} demands already have inferred_skills",
        }

    # ── Split into batches ──────────────────────────────────────────────────
    batches = [role_ids[i:i+batch_size] for i in range(0, len(role_ids), batch_size)]
    logger.info(f"[batch-extract-infer] Split {len(role_ids)} roles into {len(batches)} batch(es)")

    total_processed = 0
    total_skipped = 0
    total_failed = []
    
    for batch_idx, batch_role_ids in enumerate(batches):
        logger.info(f"[batch-extract-infer] Processing batch {batch_idx + 1}/{len(batches)} | roles={len(batch_role_ids)}")
        
        # ── Load all roles with descriptions ────────────────────────────────
        roles_to_process: list[dict] = []
        skipped_count = 0
        
        for rid in batch_role_ids:
            r = _cosmos_load_by_id(rid)
            if r is None:
                logger.warning(f"[batch-extract-infer] Role not found: {rid}")
                continue
            
            role_description = r.get("role_description") or ""
            if not role_description or len(role_description.strip()) < 50:
                logger.debug(f"[batch-extract-infer] Skipping {rid} — description too short or missing")
                skipped_count += 1
                continue
            
            # Check if already extracted
            skill_analysis = r.get("skill_analysis") or {}
            if skill_analysis.get("inferred_skills"):
                logger.debug(f"[batch-extract-infer] Skipping {rid} — inferred_skills already extracted")
                skipped_count += 1
                continue
            
            role_title = r.get("role_title") or r.get("title") or ""
            roles_to_process.append({
                "role_id": rid,
                "role_title": role_title,
                "description": role_description,
                "doc": r,
            })

        total_skipped += skipped_count

        if not roles_to_process:
            logger.info(f"[batch-extract-infer] Batch {batch_idx + 1}: No new roles to process")
            if progress_callback:
                try:
                    progress_callback(total_processed, len(role_ids))
                except Exception as _cb_exc:
                    logger.warning(f"[batch-extract-infer] Progress callback failed: {_cb_exc}")
            continue

        # ── Build batch prompt ──────────────────────────────────────────────
        batch_descriptions = "\n\n".join([
            f"[{r['role_id']}] Title: {r['role_title']}\nDescription:\n{r['description'][:2000]}"
            for r in roles_to_process
        ])

        batch_prompt = (
            "You are a Technical Recruiter. Extract specific TECHNICAL skills, tools, and platforms "
            "from EACH job description below.\n"
            "Ignore generic terms: Communication, Leadership, Problem Solving, SDLC, Agile, Software Development.\n"
            "Focus on specific technologies: e.g., Kafka, AWS, Python, Kubernetes, React, Spring Boot, Terraform.\n\n"
            f"Process {len(roles_to_process)} job descriptions:\n\n"
            f"{batch_descriptions}\n\n"
            "Return ONLY valid JSON (no markdown, no code blocks) with role_id keys mapping to skill arrays:\n"
            "{\n"
            '  "ROLE-001": ["Java", "Spring Boot", "AWS"],\n'
            '  "ROLE-002": ["Python", "Airflow", "Snowflake"],\n'
            '  "ROLE-003": []\n'
            "}"
        )

        try:
            _az_endpoint = os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT")
            _az_key = os.environ.get("AZURE_AI_FOUNDRY_KEY")
            _az_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
            _chat_model = os.environ.get("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")

            if not _az_key or not _az_endpoint:
                logger.warning("[batch-extract-infer] Azure credentials not configured")
                total_failed.extend([r["role_id"] for r in roles_to_process])
                if progress_callback:
                    try:
                        progress_callback(total_processed, len(role_ids))
                    except Exception as _cb_exc:
                        logger.warning(f"[batch-extract-infer] Progress callback failed: {_cb_exc}")
                continue

            logger.info(
                "[batch-extract-infer] Calling %s for batch %d/%d | %d roles",
                _chat_model, batch_idx + 1, len(batches), len(roles_to_process),
            )

            _az = _AsyncAzureOpenAI(api_version=_az_version, azure_endpoint=_az_endpoint, api_key=_az_key)
            
            resp = await _az.chat.completions.create(
                model=_chat_model,
                messages=[{"role": "user", "content": batch_prompt}],
                temperature=0.0,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )

            _batch_raw = (resp.choices[0].message.content or "").strip()
            logger.info("[batch-extract-infer] Azure response received for batch %d | roles=%d", batch_idx + 1, len(roles_to_process))

            # Parse with error recovery
            try:
                batch_results: dict = _json.loads(_batch_raw)
            except _json.JSONDecodeError as json_exc:
                logger.warning(f"[batch-extract-infer] JSON parse failed ({json_exc}) — attempting recovery")
                _brace_start = _batch_raw.find("{")
                _brace_end = _batch_raw.rfind("}")
                if _brace_start != -1 and _brace_end > _brace_start:
                    try:
                        batch_results = _json.loads(_batch_raw[_brace_start:_brace_end+1])
                    except Exception:
                        batch_results = {}
                else:
                    batch_results = {}

        except Exception as exc:
            logger.error(f"[batch-extract-infer] Azure LLM call FAILED for batch {batch_idx + 1}: {exc}", exc_info=True)
            total_failed.extend([r["role_id"] for r in roles_to_process])
            if progress_callback:
                try:
                    progress_callback(total_processed, len(role_ids))
                except Exception as _cb_exc:
                    logger.warning(f"[batch-extract-infer] Progress callback failed: {_cb_exc}")
            continue

        # ── Save results back to Cosmos ─────────────────────────────────────
        batch_processed = 0
        for role_data in roles_to_process:
            rid = role_data["role_id"]
            doc = role_data["doc"]
            extracted_skills = batch_results.get(rid, []) or []

            try:
                skill_analysis = doc.get("skill_analysis") or {}
                must_have = skill_analysis.get("must_have_skills") or skill_analysis.get("primary_skill", "")
                if isinstance(must_have, str):
                    must_have = [must_have] if must_have else []
                nice_have = skill_analysis.get("nice_to_have_skills") or skill_analysis.get("other_skills") or []
                
                # Build set of explicit skills to avoid duplicates
                _explicit = {s.lower().strip() for s in (must_have + nice_have) if s}

                # Build inferred_skills list
                inferred_skills: list[dict] = []
                for _skill in extracted_skills:
                    if isinstance(_skill, str) and _skill.strip():
                        _skill_lower = _skill.strip().lower()
                        if _skill_lower not in _explicit:
                            inferred_skills.append({
                                "skill": _skill.strip(),
                                "proficiency": 2,  # P2 - Intermediate (default)
                                "proficiency_label": "P2 - Intermediate",
                            })
                            _explicit.add(_skill_lower)

                # Save ONLY to inferred_skills field
                if "skill_analysis" not in doc:
                    doc["skill_analysis"] = {}
                doc["skill_analysis"]["inferred_skills"] = inferred_skills
                doc["skill_analysis_at"] = datetime.now().isoformat(timespec="seconds")

                _cosmos_upsert_request(doc)
                logger.debug(f"[batch-extract-infer] ✓ {rid} → {len(inferred_skills)} inferred skills saved")
                batch_processed += 1
                total_processed += 1

            except Exception as exc:
                logger.error(f"[batch-extract-infer] ✗ {rid} failed to save: {exc}", exc_info=True)
                total_failed.append(rid)

        logger.info(f"[batch-extract-infer] Batch {batch_idx + 1} complete: {batch_processed} processed")
        if progress_callback:
            try:
                progress_callback(total_processed, len(role_ids))
            except Exception as _cb_exc:
                logger.warning(f"[batch-extract-infer] Progress callback failed: {_cb_exc}")

    logger.info(
        "[batch-extract-infer] All batches complete | total=%d | processed=%d | skipped=%d | failed=%d",
        len(role_ids), total_processed, total_skipped, len(total_failed),
    )

    return {
        "status": "ok",
        "total": len(role_ids),
        "processed": total_processed,
        "skipped": total_skipped,
        "failed": total_failed,
        "message": f"Batch extraction completed: {total_processed} processed, {total_skipped} skipped, {len(total_failed)} failed",
    }



def update_partial_request_with_answers(role_id: str, updated_intake: str | dict) -> dict:
    """Update a partial request with clarification answers and promote it to 'pending'.

    Called when the contact replies with answers to the clarification questions.
    The updated intake is merged and the status is set to "pending" so the
    ArtifactAgent can generate the email.

    Args:
        role_id:         Role ID to update, e.g. "ROLE-001".
        updated_intake:  JSON string of the fully completed StaffingIntake.

    Returns:
        {"status": "ok", "role_id": "...", "message": "..."} or {"status": "not_found", ...}
    """
    requests = _load()
    intake_data = json.loads(updated_intake) if isinstance(updated_intake, str) else updated_intake

    for r in requests:
        if r.get("role_id") == role_id:
            r["staffing_intake"] = intake_data
            r["status"] = "pending"
            r["validation_status"] = {
                "readiness": "READY",
                "missing_fields": [],
                "clarification_questions": [],
            }
            r["clarification_answered_at"] = datetime.now().isoformat(timespec="seconds")
            _cosmos_upsert_request(r)
            
            # Invalidate match cache for this demand since it was updated
            try:
                from app.services.match_cache import invalidate_cache  # noqa: PLC0415
                invalidate_cache(role_id)
            except Exception as _cache_exc:
                logger.warning(f"[update_partial] Could not invalidate cache for {role_id}: {_cache_exc}")
            
            return {
                "status": "ok",
                "role_id": role_id,
                "message": (
                    f"{role_id} updated with clarification answers and is now READY. "
                    "Open artifact_agent to generate the email."
                ),
            }
    return {"status": "not_found", "message": f"No request found with role_id={role_id}"}


# ---------------------------------------------------------------------------
# Remediation loop tools — patch, escalate, retry
# ---------------------------------------------------------------------------

#: Fields the LLM is permitted to overwrite during self-correction.
_PATCHABLE_FIELDS = {"role", "location", "skills", "description", "seniority_level"}


def patch_staffing_request(role_id: str, field_updates: str) -> str:
    """Apply LLM-corrected field values to an existing staffing demand in Cosmos.

    This is the "Apply Fix / Edit Config" step in the Remediation Loop.
    The agent calls this after diagnosing WHY a bench search returned no results
    (e.g. too-specific location, acronym in role title, overly narrow skills list)
    and proposes corrected values to improve the next search attempt.

    Args:
        role_id:       The demand to patch, e.g. "ROLE-001".
        field_updates: JSON string mapping field names to new values.
                       Patchable fields: role, location, skills,
                       description, seniority_level.
                       ``skills`` must be a list of strings.

    Returns:
        A diff-style summary string of what changed, or an error string.
    """
    import logging as _logging
    logger = _logging.getLogger(__name__)

    try:
        updates: dict = json.loads(field_updates) if isinstance(field_updates, str) else field_updates
    except json.JSONDecodeError as exc:
        return f"patch_staffing_request: invalid JSON in field_updates — {exc}"

    # Only allow known safe fields
    updates = {k: v for k, v in updates.items() if k in _PATCHABLE_FIELDS}
    if not updates:
        return f"patch_staffing_request: no patchable fields in update (allowed: {sorted(_PATCHABLE_FIELDS)})"

    r = _cosmos_load_by_id(role_id)
    if r is None:
        return f"patch_staffing_request: {role_id} not found in Cosmos."

    intake: dict = r.get("staffing_intake", {})
    diff_lines: list[str] = []

    for field, new_val in updates.items():
        old_val = intake.get(field)
        if old_val != new_val:
            diff_lines.append(f"  {field}: {old_val!r} → {new_val!r}")
            intake[field] = new_val

    if not diff_lines:
        return f"patch_staffing_request: {role_id} — no changes needed (values already match)."

    r["staffing_intake"] = intake
    r["patched_at"] = datetime.now().isoformat(timespec="seconds")
    _cosmos_upsert_request(r)

    # Invalidate match cache for this demand since it was patched
    try:
        from app.services.match_cache import invalidate_cache  # noqa: PLC0415
        invalidate_cache(role_id)
    except Exception as _cache_exc:
        logger.warning(f"[patch_staffing_request] Could not invalidate cache for {role_id}: {_cache_exc}")

    diff_summary = "\n".join(diff_lines)
    logger.info("[patch_staffing_request] %s patched:\n%s", role_id, diff_summary)
    return f"Patched {role_id}:\n{diff_summary}"


def save_escalation_record(role_id: str, reason: str, attempts_made: int) -> str:
    """Record that auto-resolution failed and mark the demand as escalated in Cosmos.

    Called as the final step of the Remediation Loop when all attempts are
    exhausted. Sets status to "escalated" so the admin UI surfaces the demand
    for manual intervention.

    Args:
        role_id:       The demand that could not be resolved, e.g. "ROLE-001".
        reason:        Human-readable explanation of why resolution failed.
        attempts_made: Number of loop iterations that were tried.

    Returns:
        A status string.
    """
    import logging as _logging
    logger = _logging.getLogger(__name__)

    r = _cosmos_load_by_id(role_id)
    if r is None:
        return f"save_escalation_record: {role_id} not found — cannot mark escalated."

    r["status"] = "escalated"
    r["escalation_reason"] = reason
    r["escalation_attempts"] = attempts_made
    r["escalated_at"] = datetime.now().isoformat(timespec="seconds")
    _cosmos_upsert_request(r)

    logger.warning(
        "[save_escalation_record] %s escalated after %d attempt(s): %s",
        role_id, attempts_made, reason,
    )
    return (
        f"{role_id} marked as escalated after {attempts_made} attempt(s). "
        f"Reason: {reason}"
    )


# ---------------------------------------------------------------------------
# Internal retry wrapper for analyze_demand_skills (fire-and-forget safe)
# ---------------------------------------------------------------------------

async def _analyze_with_retry(role_id: str, max_retries: int = 3) -> None:
    """Run analyze_demand_skills with exponential-backoff retry.

    Replaces the old raw create_task() call so transient Gemini errors no
    longer cause silent skill-analysis failures.
    """
    import asyncio as _asyncio
    import logging as _logging
    logger = _logging.getLogger(__name__)

    for attempt in range(max_retries):
        try:
            await analyze_demand_skills(role_id)
            return
        except Exception as exc:
            if attempt == max_retries - 1:
                logger.error(
                    "[_analyze_with_retry] analyze_demand_skills failed for %s after %d attempts: %s",
                    role_id, max_retries, exc, exc_info=True,
                )
            else:
                backoff = 2 ** attempt
                logger.warning(
                    "[_analyze_with_retry] Attempt %d failed for %s (%s) — retrying in %ds",
                    attempt + 1, role_id, exc, backoff,
                )
                await _asyncio.sleep(backoff)
