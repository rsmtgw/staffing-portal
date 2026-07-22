"""
app/routes/auth.py
------------------
Authentication endpoints:
  GET  /login              — login page (HTML)
  POST /api/auth/login     — process credentials, set session cookie
  GET  /logout             — clear session cookie and redirect to /login
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    check_credentials,
    create_session,
    revoke_session,
)

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

# ── Login page HTML ───────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Sign In | Staffing Platform</title>
  <style>
    :root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3250;
          --accent:#6366f1;--accent2:#818cf8;--text:#e2e8f0;--text-muted:#8892aa;--red:#ef4444;}
    *{box-sizing:border-box;margin:0;padding:0;}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);
         min-height:100vh;display:flex;align-items:center;justify-content:center;}
    .card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
          padding:40px 44px;width:min(380px,94vw);box-shadow:0 8px 32px #00000066;}
    .logo{text-align:center;font-size:36px;margin-bottom:6px;}
    h1{text-align:center;font-size:20px;font-weight:700;color:var(--accent2);margin-bottom:6px;}
    .subtitle{text-align:center;font-size:13px;color:var(--text-muted);margin-bottom:32px;}
    .form-group{margin-bottom:18px;}
    label{display:block;font-size:11px;color:var(--text-muted);text-transform:uppercase;
          letter-spacing:.06em;margin-bottom:6px;}
    input[type=text],input[type=password]{width:100%;background:var(--surface2);color:var(--text);
      border:1px solid var(--border);border-radius:7px;padding:10px 13px;font-size:14px;
      outline:none;transition:border-color .15s;}
    input:focus{border-color:var(--accent);}
    .btn{width:100%;padding:11px;border-radius:7px;border:none;background:var(--accent);
         color:#fff;font-size:14px;font-weight:700;cursor:pointer;transition:opacity .15s;
         margin-top:6px;}
    .btn:hover{opacity:.88;}
    .sso-btn{display:block;text-align:center;text-decoration:none;background:#1e40af;margin-bottom:12px;}
    .sso-btn:hover{opacity:.88;}
    .divider{text-align:center;margin:12px 0;position:relative;}
    .divider span{background:var(--surface);padding:0 12px;font-size:12px;color:var(--text-muted);position:relative;z-index:1;}
    .divider::before{content:'';position:absolute;left:0;top:50%;width:100%;height:1px;background:var(--border);}
    .error{background:#7f1d1d44;border:1px solid #991b1b55;color:#fca5a5;border-radius:7px;
            padding:10px 14px;font-size:13px;margin-bottom:16px;display:none;}
    .error.show{display:block;}
    .session-note{text-align:center;font-size:11px;color:var(--text-muted);margin-top:18px;}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">🎯</div>
    <h1>Staffing Platform</h1>
    <p class="subtitle">Sign in to access the platform</p>

    <div id="err-box" class="error">{error}</div>

    {sso_block}

    <form method="POST" action="/api/auth/login">
      <input type="hidden" name="next" value="{next}">
      <div class="form-group">
        <label>Username</label>
        <input type="text" name="username" autocomplete="username" autofocus required>
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" autocomplete="current-password" required>
      </div>
      <button type="submit" class="btn">Sign In</button>
    </form>
    <p class="session-note">Session expires after 8 hours of inactivity.</p>
  </div>
  <script>
    const err = document.getElementById('err-box');
    if (err.textContent.trim()) err.classList.add('show');
  </script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: str = "", next: str = "/") -> HTMLResponse:
    """Render the login page. Redirect to home if already authenticated.
    When SAML is enabled, show an SSO button alongside the local login form."""
    from app.auth import SESSION_COOKIE, is_valid_session
    from app.config import settings as _cfg

    if is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url=next or "/", status_code=302)

    sso_block = ""

    html = (
        _LOGIN_HTML
        .replace("{error}", _esc(error))
        .replace("{next}", _esc(next or "/"))
        .replace("{sso_block}", sso_block)
    )
    return HTMLResponse(html)


@router.post("/api/auth/login", include_in_schema=False)
async def do_login(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
) -> RedirectResponse:
    """Validate credentials and set a session cookie."""
    if check_credentials(username, password):
        token = create_session()
        _logger.info("[auth] Login successful for user=%s", username)
        redirect_to = next if next.startswith("/") else "/"
        response = RedirectResponse(url=redirect_to, status_code=302)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return response

    _logger.warning("[auth] Failed login attempt for user=%s", username)
    safe_next = _esc_url(next)
    return RedirectResponse(
        url=f"/login?error=Invalid+username+or+password&next={safe_next}",
        status_code=302,
    )


@router.get("/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    """Revoke the current session and redirect to the login page.
    When OIDC is enabled, redirect through the Azure AD logout endpoint."""
    token = request.cookies.get(SESSION_COOKIE)
    revoke_session(token)
    _logger.info("[auth] User logged out")

    response = RedirectResponse(url="/login", status_code=302)

    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _esc_url(s: str) -> str:
    from urllib.parse import quote
    return quote(s or "/", safe="/")
