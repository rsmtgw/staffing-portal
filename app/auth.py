"""
app/auth.py
-----------
Lightweight session-based authentication for the Staffing Platform web UI.

Sessions are stored in-memory (cleared on server restart).
Credentials are configured via PORTAL_USERNAME / PORTAL_PASSWORD env vars.

Usage in route handlers:
    from fastapi import Depends
    from app.auth import require_html_auth, require_api_auth

    @router.get("/some/page", dependencies=[Depends(require_html_auth)])
    async def my_page(): ...

    @router.get("/api/something", dependencies=[Depends(require_api_auth)])
    async def my_api(): ...
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time

from fastapi import HTTPException, Request

_logger = logging.getLogger(__name__)

# Log environment variables at module load time
_oidc_enabled = os.environ.get("OIDC_ENABLED", "false").lower() == "true"
_logger.info(f"[AUTH INIT] OIDC_ENABLED={_oidc_enabled}")
_logger.info(f"[AUTH INIT] PORTAL_USERNAME={os.environ.get('PORTAL_USERNAME', 'NOT_SET')}")

SESSION_COOKIE = "sp_session"
SESSION_TTL_SECONDS = 8 * 3600  # 8 hours

# In-memory session store: token -> expiry timestamp
_SESSIONS: dict[str, float] = {}


class NeedsLoginRedirect(Exception):
    """Raised by HTML auth dependency; converted to a 302 redirect by the exception handler."""


# ── Session helpers ───────────────────────────────────────────────────────────

def create_session() -> str:
    """Generate a new cryptographically random session token and register it."""
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.monotonic() + SESSION_TTL_SECONDS
    _prune_sessions()
    return token


def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    expires = _SESSIONS.get(token)
    if expires is None:
        return False
    if time.monotonic() > expires:
        _SESSIONS.pop(token, None)
        return False
    # Sliding window: extend expiry on each valid use
    _SESSIONS[token] = time.monotonic() + SESSION_TTL_SECONDS
    return True


def revoke_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def _prune_sessions() -> None:
    now = time.monotonic()
    stale = [t for t, exp in _SESSIONS.items() if now > exp]
    for t in stale:
        del _SESSIONS[t]


# ── Credential check ──────────────────────────────────────────────────────────

def check_credentials(username: str, password: str) -> bool:
    """Constant-time credential check — always evaluates both comparisons."""
    from app.config import settings

    expected_user = settings.portal_username
    expected_pass = settings.portal_password

    # Use hmac.compare_digest to prevent timing attacks.
    # & instead of 'and' ensures both sides are always evaluated.
    user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return bool(user_ok & pass_ok)


# ── Azure App Service auth detection ─────────────────────────────────────────

def _is_azure_authenticated(request: Request) -> bool:
    """
    Check if the request is authenticated via Azure App Service authentication.
    
    When Azure App Service auth is enabled, it injects X-MS-CLIENT-PRINCIPAL header
    with the authenticated user's identity. Presence of this header means
    Azure has already validated the user via SSO (and we can skip basic auth).
    """
    azure_id = request.headers.get("x-ms-client-principal-id")
    azure_name = request.headers.get("x-ms-client-principal-name")
    is_azure = "x-ms-client-principal-id" in request.headers
    
    _logger.info(f"[AUTH CHECK] Azure header present: {is_azure}")
    if azure_id:
        _logger.info(f"[AUTH CHECK] Azure user ID: {azure_id}")
    if azure_name:
        _logger.info(f"[AUTH CHECK] Azure user name: {azure_name}")
    
    # Log all headers for debugging (excluding Authorization for safety)
    safe_headers = {k: v for k, v in request.headers.items() if k.lower() not in ["authorization", "cookie"]}
    _logger.debug(f"[AUTH CHECK] Request headers: {safe_headers}")
    
    return is_azure


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def require_html_auth(request: Request) -> None:
    """
    Dependency for HTML routes: raises NeedsLoginRedirect when not authenticated.
    
    Checks Azure AD headers first (if App Service auth is enabled).
    Falls back to session cookie check for local dev without Azure auth.
    """
    # If Azure App Service auth is active, skip basic auth check
    if _is_azure_authenticated(request):
        _logger.info(f"[HTML AUTH] ✅ Azure SSO authenticated — access granted")
        return
    
    # Otherwise, require session cookie (basic auth flow)
    token = request.cookies.get(SESSION_COOKIE)
    if not is_valid_session(token):
        _logger.warning(f"[HTML AUTH] ❌ No valid session — redirecting to login")
        raise NeedsLoginRedirect()
    
    _logger.info(f"[HTML AUTH] ✅ Session cookie valid — access granted")


def require_api_auth(request: Request) -> None:
    """
    Dependency for API routes: raises HTTP 401 when not authenticated.
    
    Checks Azure AD headers first (if App Service auth is enabled).
    Falls back to session cookie check for local dev without Azure auth.
    """
    # If Azure App Service auth is active, skip basic auth check
    if _is_azure_authenticated(request):
        _logger.info(f"[API AUTH] ✅ Azure SSO authenticated — access granted")
        return
    
    # Otherwise, require session cookie (basic auth flow)
    token = request.cookies.get(SESSION_COOKIE)
    if not is_valid_session(token):
        _logger.warning(f"[API AUTH] ❌ No valid session — returning 401")
        raise HTTPException(status_code=401, detail="Not authenticated — please log in at /login")
    
    _logger.info(f"[API AUTH] ✅ Session cookie valid — access granted")
