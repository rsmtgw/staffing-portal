"""
app/config.py
-------------
Settings for the Staffing Match Dashboard.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path, override=True)
# Fall back to sibling staffing-agent repo for COSMOS_KEY only (shared dev environment)
if not os.environ.get("COSMOS_KEY"):
    _agent_env = Path(__file__).parent.parent.parent / "staffing-agent" / ".env"
    if _agent_env.exists():
        from dotenv import dotenv_values
        _agent_vals = dotenv_values(_agent_env)
        if _agent_vals.get("COSMOS_KEY"):
            os.environ["COSMOS_KEY"] = _agent_vals["COSMOS_KEY"]


class Settings:
    # ---- Azure Cosmos DB ---------------------------------------------------
    use_cosmos: bool = os.environ.get("USE_COSMOS", "true").lower() == "true"
    cosmos_endpoint: str = os.environ.get("COSMOS_ENDPOINT", "https://localhost:8081")
    cosmos_key: str = os.environ.get("COSMOS_KEY", "")
    cosmos_database: str = os.environ.get("COSMOS_DATABASE", "staffing_agent")
    cosmos_container: str = os.environ.get("COSMOS_CONTAINER", "bench_roster")
    cosmos_report_cache_container: str = os.environ.get(
        "REPORT_CACHE_BLOB_CONTAINER", "report-cache"
    )

    # ---- Azure OpenAI (AI-Analyze feature) ---------------------------------
    azure_ai_foundry_endpoint: str = os.environ.get(
        "AZURE_AI_FOUNDRY_ENDPOINT", "https://openai-interviewassists.openai.azure.com/"
    )
    azure_ai_foundry_key: str = (
        os.environ.get("AZURE_AI_FOUNDRY_KEY")
        or os.environ.get("AZURE_OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    azure_openai_api_version: str = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-14")
    azure_chat_deployment: str = os.environ.get("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")
    azure_embed_deployment: str = os.environ.get("AZURE_EMBED_DEPLOYMENT", "text-embedding-3-large")

    # ---- Skill embedding similarity ----------------------------------------
    skill_embed_similarity_threshold: float = float(
        os.environ.get("SKILL_EMBED_SIMILARITY_THRESHOLD", "0.90")
    )
    skill_embed_enabled: bool = os.environ.get("SKILL_EMBED_ENABLED", "false").lower() == "true"
    skill_equiv_llm_revalidate: bool = os.environ.get(
        "SKILL_EQUIV_LLM_REVALIDATE", "true"
    ).lower() == "true"

    # ---- Web UI authentication ---------------------------------------------
    portal_username: str = os.environ.get("PORTAL_USERNAME", "admin")
    portal_password: str = os.environ.get("PORTAL_PASSWORD", "admin")

    # ---- Application -------------------------------------------------------
    app_title: str = "Staffing Match Dashboard"
    app_version: str = "1.0.0"
    cors_origins: list[str] = ["*"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# ---------------------------------------------------------------------------
# Runtime feature flags (Cosmos-backed with local file fallback)
# ---------------------------------------------------------------------------

import json as _json
import time as _time
from pathlib import Path as _Path

_FLAGS_PATH = _Path(__file__).resolve().parents[1] / "conf" / "runtime_flags.json"
_flags_cache: dict = {}
_flags_cache_ts: float = 0.0
_FLAGS_CACHE_TTL: float = 60.0
_FLAGS_COSMOS_CONTAINER = "app_settings"
_FLAGS_COSMOS_DOC_ID = "runtime_flags"
_FLAGS_COSMOS_PARTITION = "/id"


def _load_flags_from_cosmos() -> dict | None:
    try:
        from app.agents.submit_demand.tools.cosmos_store import _cosmos_client
        from azure.cosmos import PartitionKey
        client = _cosmos_client()
        db_name = os.environ.get("COSMOS_DATABASE", "staffing_agent")
        db = client.create_database_if_not_exists(id=db_name)
        container = db.create_container_if_not_exists(
            id=_FLAGS_COSMOS_CONTAINER,
            partition_key=PartitionKey(path=_FLAGS_COSMOS_PARTITION),
        )
        doc = container.read_item(item=_FLAGS_COSMOS_DOC_ID, partition_key=_FLAGS_COSMOS_DOC_ID)
        return {k: v for k, v in doc.items() if not k.startswith("_") and k != "id"}
    except Exception:
        return None


def _load_flags() -> dict:
    global _flags_cache, _flags_cache_ts
    now = _time.monotonic()
    if now - _flags_cache_ts < _FLAGS_CACHE_TTL and _flags_cache:
        return _flags_cache
    cosmos_flags = _load_flags_from_cosmos()
    if cosmos_flags:
        _flags_cache = cosmos_flags
        _flags_cache_ts = now
        return _flags_cache
    try:
        raw = _FLAGS_PATH.read_text(encoding="utf-8")
        loaded = _json.loads(raw)
        _flags_cache = {k: v for k, v in loaded.items() if not k.startswith("_")}
        _flags_cache_ts = now
    except Exception:
        pass
    return _flags_cache


def _save_flags_to_cosmos(flags: dict) -> None:
    try:
        from app.agents.submit_demand.tools.cosmos_store import _cosmos_client
        from azure.cosmos import PartitionKey
        client = _cosmos_client()
        db_name = os.environ.get("COSMOS_DATABASE", "staffing_agent")
        db = client.create_database_if_not_exists(id=db_name)
        container = db.create_container_if_not_exists(
            id=_FLAGS_COSMOS_CONTAINER,
            partition_key=PartitionKey(path=_FLAGS_COSMOS_PARTITION),
        )
        container.upsert_item({"id": _FLAGS_COSMOS_DOC_ID, **flags})
    except Exception:
        pass


def set_runtime_flag(name: str, value) -> None:
    global _flags_cache, _flags_cache_ts
    flags = dict(_load_flags())
    flags[name] = value
    _save_flags_to_cosmos(flags)
    try:
        out = {"_comment": "Runtime feature flags — editable via Admin UI."}
        out.update(flags)
        _FLAGS_PATH.write_text(_json.dumps(out, indent=2), encoding="utf-8")
    except Exception:
        pass
    _flags_cache = dict(flags)
    _flags_cache_ts = _time.monotonic()


def get_runtime_flag(name: str, default=None):
    flags = _load_flags()
    if name in flags:
        return flags[name]
    return getattr(settings, name, default)
