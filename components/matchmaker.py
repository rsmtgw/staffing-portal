"""
components/matchmaker.py  (staffing-portal)
-------------------------------------------
Thin shim — all matching logic lives in the ai-staffing-matchmaker package.

This module exists only to preserve the import surface that report.py,
manage.py and matching_report_tool.py rely on:

    from components.matchmaker import MatchmakerEngine
    engine = MatchmakerEngine()
    results = engine.match(demand_skill_analysis=..., candidates=...)
    result.fit, result.score_pct, result.to_dict()
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Literal

# Locate ai-staffing-matchmaker — check local dev path first, then Docker/server path.
_AI_SM_ROOT = None
for _candidate in [
    # Local dev: sibling repo next to staffing-portal
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "AI-Staffing-Matchmaker")),
    # Docker / server: cloned to fixed location by Dockerfile
    "/ai-staffing-matchmaker",
]:
    if os.path.isfile(os.path.join(_candidate, "core", "matcher.py")):
        _AI_SM_ROOT = _candidate
        break

if _AI_SM_ROOT and _AI_SM_ROOT not in sys.path:
    sys.path.insert(0, _AI_SM_ROOT)

# core/__init__.py unconditionally imports SkillKnowledgeBase which requires
# lancedb — not installed in this environment. Inject a minimal stub for the
# 'core' package so Python skips __init__.py and loads core.matcher directly.
if _AI_SM_ROOT and "core" not in sys.modules:
    import types as _types
    _core_stub = _types.ModuleType("core")
    _core_stub.__path__ = [os.path.join(_AI_SM_ROOT, "core")]  # type: ignore[attr-defined]
    _core_stub.__package__ = "core"
    sys.modules["core"] = _core_stub

FitLabel = Literal["Excellent", "Good", "Regular", "No Match"]


@dataclass
class MatchResult:
    """Minimal result object returned by MatchmakerEngine.match()."""
    candidate: dict
    fit: FitLabel
    score_pct: float
    primary_match: str                      # "exact" | "equivalent" | "none"
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    rationale: str = ""
    scoring_trace: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        raw_profile = self.candidate.get("skill_profile") or []
        raw_skills = self.candidate.get("skills") or []
        return {
            "name":             self.candidate.get("name"),
            "role":             self.candidate.get("role"),
            "employee_id":      self.candidate.get("employee_id") or self.candidate.get("candidate_id"),
            "accenture_level":  self.candidate.get("accenture_level"),
            "resume_link":      self.candidate.get("resume_link") or "",
            "candidate_skill_profile": raw_profile,
            "candidate_skills": raw_skills,
            "fit":              self.fit,
            "score_pct":        round(self.score_pct, 1),
            "primary_match":    self.primary_match,
            "matched_skills":   self.matched_skills,
            "missing_skills":   self.missing_skills,
            "rationale":        self.rationale,
            "scoring_trace":    self.scoring_trace,
        }


class MatchmakerEngine:
    """Delegates all matching to the ai-staffing-matchmaker package."""

    def match(
        self,
        demand_skill_analysis: dict,
        candidates: list[dict],
        top_n: int | None = None,
    ) -> list[MatchResult]:
        from core.matcher import match_candidate_to_role
        from components.ai_sm_bridge import (
            build_ai_sm_candidate,
            build_ai_sm_demand,
            ai_sm_result_to_match_result,
        )
        try:
            from app.services.eval_cache import get_eval_cache
            kb = get_eval_cache()
        except Exception:
            kb = None

        primary_skill    = demand_skill_analysis.get("primary_skill") or ""
        secondary_skills = demand_skill_analysis.get("secondary_skills") or []
        other_skills     = demand_skill_analysis.get("other_skills") or []
        inferred_raw     = demand_skill_analysis.get("inferred_skills") or []
        demand_cl        = demand_skill_analysis.get("accenture_level")
        demand_cl_to     = demand_skill_analysis.get("accenture_level_to")

        inferred_skills = [
            item["skill"] if isinstance(item, dict) else str(item)
            for item in inferred_raw if item
        ]

        # Resolve formal aliases (e.g. "Core Java" → "Java") from Cosmos
        formal_aliases = _load_formal_aliases()
        primary_canonical = formal_aliases.get(primary_skill.strip().lower(), primary_skill)

        # Look up Cosmos equivalencies for the primary skill
        equivalencies = _get_equivalencies_for(primary_canonical)

        ai_demand = build_ai_sm_demand(
            primary_skill=primary_canonical,
            secondary_skills=secondary_skills,
            other_skills=other_skills,
            inferred_skills=inferred_skills,
            demand_cl=demand_cl,
            demand_cl_to=demand_cl_to,
            equivalencies=equivalencies,
        )

        results: list[MatchResult] = []
        for cand in candidates:
            ai_candidate = build_ai_sm_candidate(cand)
            raw = match_candidate_to_role(ai_candidate, ai_demand, kb=kb)
            results.append(ai_sm_result_to_match_result(raw, cand))

        results.sort(
            key=lambda r: (
                {"Excellent": 0, "Good": 1, "Regular": 2, "No Match": 3}.get(r.fit, 4),
                -r.score_pct,
            )
        )

        if top_n is not None:
            results = results[:top_n]

        return results


# ---------------------------------------------------------------------------
# Helpers — load from Cosmos (same service used by staffing-agent)
# ---------------------------------------------------------------------------

def _load_formal_aliases() -> dict[str, str]:
    try:
        from app.services.equivalency_service import get_equivalencies
        fa = get_equivalencies().get("formal_aliases", {})
        return {k.strip().lower(): v.strip().lower() for k, v in fa.items()}
    except Exception:
        return {}


def _load_equivalencies() -> dict[str, list[str]]:
    try:
        from app.services.equivalency_service import get_equivalencies
        return get_equivalencies().get("equivalencies", {})
    except Exception:
        return {}


def _get_equivalencies_for(primary_skill: str) -> list[str]:
    primary_lower = primary_skill.strip().lower()
    for canon, subs in _load_equivalencies().items():
        if canon.strip().lower() == primary_lower:
            return list(subs)
        if primary_lower in [s.strip().lower() for s in subs]:
            return [canon] + [s for s in subs if s.strip().lower() != primary_lower]
    return []
