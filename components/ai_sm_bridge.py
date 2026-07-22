"""
components/ai_sm_bridge.py
--------------------------
Format adapter between staffing-agent's Cosmos document model and the
ai-staffing-matchmaker library's flat-dict API.

Conversion flow:
  bench_roster doc (Cosmos)  ──► build_ai_sm_candidate() ──► AI-SM candidate dict
  skill_analysis dict         ──► build_ai_sm_demand()    ──► AI-SM demand dict
  AI-SM result dict           ──► ai_sm_result_to_match_result() ──► MatchResult
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

# Proficiency level labels used in Skill_List strings
_PROF_LABELS: dict[int, str] = {
    1: "Beginner",
    2: "Intermediate",
    3: "Advanced",
    4: "Expert",
}


def _build_skill_list(candidate: dict) -> str:
    """Convert skill_profile (structured) or skills (flat) to AI-SM Skill_List string.

    Output format: "1 - Java (P4 - Expert); 2 - Spring Boot (P3 - Advanced)"
    """
    profile = candidate.get("skill_profile")
    if profile and isinstance(profile, list):
        parts = []
        for i, item in enumerate(profile, 1):
            if not isinstance(item, dict):
                continue
            skill = (item.get("skill") or "").strip()
            if not skill:
                continue
            try:
                prof = int(item.get("proficiency", 2))
            except (TypeError, ValueError):
                prof = 2
            label = _PROF_LABELS.get(prof, "Intermediate")
            parts.append(f"{i} - {skill} (P{prof} - {label})")
        return "; ".join(parts)

    # Fallback: flat skills list — treat all as P2 (Intermediate)
    skills = candidate.get("skills") or []
    return "; ".join(
        f"{i} - {s} (P2 - Intermediate)"
        for i, s in enumerate(skills, 1)
        if isinstance(s, str) and s.strip()
    )


def build_ai_sm_candidate(candidate: dict) -> dict:
    """Convert a Cosmos bench_roster document to AI-SM candidate format."""
    return {
        "Employee_ID": (
            candidate.get("employee_id")
            or candidate.get("candidate_id")
            or candidate.get("name", "unknown")
        ),
        "Skill_List": _build_skill_list(candidate),
        "Current_Career_Level": candidate.get("accenture_level", ""),
        "Candidate_CL": candidate.get("accenture_level", ""),
        "Industry_Experience": candidate.get("industry", ""),
        "Industries": candidate.get("industries", ""),
        "Capability": candidate.get("capability", ""),
        "Resume_Link": candidate.get("resume_link", ""),
    }


def build_ai_sm_demand(
    primary_skill: str,
    secondary_skills: list[str],
    other_skills: list[str],
    inferred_skills: list[str],
    demand_cl: str | None,
    demand_cl_to: str | None,
    role_id: str = "",
    equivalencies: list[str] | None = None,
    role_description: str = "",
) -> dict:
    """Convert staffing-agent skill_analysis fields to an AI-SM demand dict."""
    def _pipe_join(skills: list[str]) -> str:
        return " | ".join(s for s in skills if s)

    return {
        "Role ID": role_id,
        "Role Description": role_description,
        "Role Primary Skill": primary_skill or "",
        "Role Secondary Skill": _pipe_join(secondary_skills),
        "Role Other Skills": _pipe_join(other_skills),
        "Inferred Skills": _pipe_join(inferred_skills),
        "Valid_Equivalencies": equivalencies or [],
        # Career level: AI-SM check_career_level uses both From and To
        "Role Career Level From": demand_cl or "",
        "Role Career Level To": demand_cl_to or demand_cl or "",
    }


def ai_sm_result_to_match_result(result: dict | None, candidate: dict) -> "MatchResult":
    """Convert an AI-SM result dict (or None for No Match) to a MatchResult."""
    from components.matchmaker import MatchResult  # local import to avoid circular dep

    if result is None:
        return MatchResult(
            candidate=candidate,
            fit="No Match",
            score_pct=0.0,
            primary_match="none",
        )

    pct_str = result.get("Match_Percentage", "0%")
    try:
        score_pct = float(pct_str.rstrip("%"))
    except ValueError:
        score_pct = 0.0

    fit = result.get("Fit", "No Match")

    # Determine primary_match type from the rationale / matched fields
    primary_match = "none"
    equiv_str = (result.get("Equivalent_Skills") or "").strip()
    skills_matched_str = (result.get("Skills_Matched") or "").strip()
    primary_raw = (result.get("Role Primary Skill") or "").lower().strip()

    if skills_matched_str and primary_raw:
        matched_lower = [s.strip().lower() for s in skills_matched_str.split(",") if s.strip()]
        if any(primary_raw in m or m in primary_raw for m in matched_lower):
            primary_match = "exact"

    if primary_match == "none" and equiv_str:
        primary_match = "equivalent"

    matched = [s.strip() for s in skills_matched_str.split(",") if s.strip()]
    missing = [
        s.strip()
        for s in (result.get("Missing_Skills") or "").split(",")
        if s.strip()
    ]

    return MatchResult(
        candidate=candidate,
        fit=fit,
        score_pct=score_pct,
        primary_match=primary_match,
        matched_skills=matched,
        missing_skills=missing,
        rationale=result.get("Rationale", ""),
        scoring_trace={
            "source": "ai_sm",
            "llm_eval_reason": result.get("LLM_Eval_Reason", ""),
        },
    )
