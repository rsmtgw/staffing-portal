"""
app package — Staffing Platform FastAPI application.

Entry points:
    uvicorn app:app --reload --port 8000
    uvicorn app.main:app --reload --port 8000
"""

# Lazy import — uvicorn resolves `app:app` by importing this package and then
# looking up the `app` attribute. We import here so the attribute is available,
# but using __getattr__ to avoid triggering circular imports at package load time.

def __getattr__(name: str):
    if name == "app":
        from app.main import app  # noqa: PLC0415
        return app
    raise AttributeError(f"module 'app' has no attribute {name!r}")
