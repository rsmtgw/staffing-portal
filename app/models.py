"""
app/models.py
-------------
Pydantic schemas for the Staffing Match Dashboard API.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    version: str


class FeedbackRequest(BaseModel):
    session_id: str
    turn_index: int = Field(..., ge=0)
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=2_000)


class SkillProfileEntry(BaseModel):
    skill: str = Field(..., min_length=1, max_length=120)
    proficiency: int = Field(..., ge=1, le=5)
    source: str = Field("csv", max_length=40)


class WhatIfMatchRequest(BaseModel):
    role_id: str = Field(..., min_length=1)
    candidate_id: str | None = Field(None, min_length=1)
    candidate_name: str | None = Field(None, min_length=1)
    candidate_role: str | None = Field(None, min_length=1)
    edited_skill_profile: list[SkillProfileEntry] | None = None


class AiAnalyzeRequest(BaseModel):
    role_id: str = Field(..., min_length=1)
    employee_id: str | None = None
    candidate_name: str | None = Field(None, min_length=1)
    candidate_role: str | None = None


class WhatIfMatchResponse(BaseModel):
    role_id: str
    candidate_id: str | None = None
    candidate_name: str
    candidate_role: str
    demand: dict[str, Any]
    original_result: dict[str, Any]
    updated_result: dict[str, Any]
    skill_profile: list[SkillProfileEntry]
    inferred_additional_skills: list[SkillProfileEntry]


class InteractionFeedbackRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    role_id: str = Field(..., min_length=1)
    candidate_id: str | None = None
    candidate_name: str = Field(..., min_length=1)
    candidate_role: str = Field(..., min_length=1)
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=2000)
    demand: dict[str, Any] = Field(default_factory=dict)
    original_result: dict[str, Any] = Field(default_factory=dict)
    updated_result: dict[str, Any] = Field(default_factory=dict)
    edited_skill_profile: list[SkillProfileEntry] = Field(default_factory=list)
    inferred_additional_skills: list[SkillProfileEntry] = Field(default_factory=list)


class InteractionFeedbackResponse(BaseModel):
    status: str
    feedback_id: str
