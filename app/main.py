"""
app/main.py
-----------
FastAPI application for the Staffing Match Dashboard.

Run::
    uvicorn app.main:app --reload --port 8000
    uvicorn app:app --reload --port 8000
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.lifespan import lifespan
from app.auth import NeedsLoginRedirect
from app.routes.report import router as report_router
from app.routes.auth import router as auth_router
from app.routes.manage import router as manage_router
from app.routes.admin import router as admin_router

import warnings
import urllib3
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("azure.cosmos._cosmos_http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)

app = FastAPI(
    title="Staffing Match Dashboard",
    version="1.0.0",
    description="Bench-candidate × demand scoring dashboard powered by Azure Cosmos DB.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_errors_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        _logger.error("Unhandled exception: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.exception_handler(NeedsLoginRedirect)
async def _needs_login_handler(request: Request, exc: NeedsLoginRedirect) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/login?next={quote(str(request.url.path), safe='/')}",
        status_code=302,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "version": settings.app_version}


app.include_router(auth_router)
app.include_router(report_router)
app.include_router(manage_router)
app.include_router(admin_router)


@app.get("/")
async def root():
    return RedirectResponse(url="/report/dashboard", status_code=302)
