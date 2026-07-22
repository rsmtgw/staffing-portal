"""
app/routes/admin.py
-------------------
Minimal admin endpoints for the Staffing Match Dashboard.

Routes:
  GET  /api/admin/feature-flags          — list runtime feature flags
  POST /api/admin/feature-flags/{flag}   — toggle a runtime feature flag
  POST /api/poll                         — stub (email poller not available here)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_EXPOSED_FLAGS: dict[str, dict] = {
    "skill_equiv_llm_revalidate": {
        "label": "LLM Skill Revalidation",
        "description": (
            "When enabled, AI embedding matches are confirmed by Gemini Flash "
            "before being accepted. Prevents false-positive '~ Equivalent' matches. "
            "Disable to trust embedding similarity alone (faster, less accurate)."
        ),
        "default": True,
    },
    "skill_embed_enabled": {
        "label": "Embedding Similarity Fallback",
        "description": (
            "When enabled, if no string/JSON match is found for a primary skill, "
            "the engine embeds both skill names and checks cosine similarity. "
            "Disable to use only deterministic string + JSON matching."
        ),
        "default": True,
    },
}


@router.get("/api/admin/feature-flags")
async def get_feature_flags():
    from app.config import get_runtime_flag
    return {
        flag: {**meta, "value": get_runtime_flag(flag, meta["default"])}
        for flag, meta in _EXPOSED_FLAGS.items()
    }


@router.post("/api/admin/feature-flags/{flag}")
async def set_feature_flag(flag: str, request: Request):
    if flag not in _EXPOSED_FLAGS:
        raise HTTPException(status_code=404, detail=f"Unknown flag: {flag}")
    try:
        body = await request.json()
        value = bool(body.get("value", True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid body: {exc}") from exc
    from app.config import set_runtime_flag
    set_runtime_flag(flag, value)
    _logger.info("[admin] Feature flag %s set to %s", flag, value)
    return {"flag": flag, "value": value, "status": "ok"}


@router.post("/api/poll")
async def trigger_poll():
    """Email poller is not available in the dashboard-only portal."""
    return {"status": "skipped", "message": "Email polling is not available in this portal."}
