"""
app/lifespan.py
---------------
FastAPI lifespan context manager for the Staffing Portal (UI-only).

Starts the bench-match report cache pre-warm on startup so the first
dashboard request is instant.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Pre-warm report cache on startup."""
    import threading

    def _prewarm_report_cache() -> None:
        try:
            _logger.info("[Lifespan] Pre-warming bench-match report cache…")
            from app.agents.submit_demand.tools.matching_report_tool import (
                generate_bench_matching_report_structured,
            )
            from app.routes.report import _cache_key, _get_cached, _set_cached

            key = _cache_key("")
            if _get_cached(key) is None:
                result = generate_bench_matching_report_structured(
                    fit_filter="Excellent,Good,Regular",
                    role_id="",
                )
                _set_cached(key, result)
                _logger.info(
                    "[Lifespan] Cache warm — %d demands scored, %d on bench.",
                    result.get("demands_scored", 0),
                    result.get("bench_size", 0),
                )
            else:
                _logger.info("[Lifespan] Cache already warm — skipping pre-warm.")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[Lifespan] Report cache pre-warm failed (non-fatal): %s", exc)

    threading.Thread(target=_prewarm_report_cache, daemon=True, name="report-prewarm").start()

    try:
        yield
    finally:
        _logger.info("[Lifespan] Shutdown complete.")
