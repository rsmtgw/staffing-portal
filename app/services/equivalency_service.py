"""
app/services/equivalency_service.py
-----------------------------------
Cosmos DB-backed skill equivalency loader with in-memory TTL caching.

Equivalencies are stored as a single master document in Cosmos:
  {
    "id": "equivalencies_master",
    "type": "equivalencies",
    "version": 1,
    "last_updated": "2026-07-03T...",
    "data": {
      "formal_aliases": { "Python (Programming Language)": "Python", ... },
      "equivalencies": {
        "Python": ["Python 3", "Python 3.x", ...],
        ...
      }
    }
  }

In-memory cache with 5-minute TTL to avoid repeated Cosmos reads.
Invalidated on admin updates via invalidate_cache().
Graceful fallback to empty equivalencies if Cosmos unavailable.
"""

import json
import logging
import time
from typing import Optional

_logger = logging.getLogger(__name__)

# In-memory cache
_cache: Optional[dict] = None
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_cosmos_client():
    """Get the Azure Cosmos DB client from settings."""
    try:
        from app.config import settings
        from azure.cosmos import CosmosClient

        client = CosmosClient(
            url=settings.cosmos_endpoint,
            credential=settings.cosmos_key,
        )
        return client
    except Exception as exc:
        _logger.error("[equivalency_service] Failed to create Cosmos client: %s", exc)
        return None


def _load_from_cosmos() -> dict:
    """Load equivalencies master document from Cosmos DB.

    Returns:
        {
            "formal_aliases": {...},
            "equivalencies": {...}
        }

    Returns empty dicts if Cosmos unavailable or document not found.
    Falls back to JSON file if Cosmos document missing.
    """
    try:
        from app.config import settings

        client = _get_cosmos_client()
        if not client:
            _logger.warning("[equivalency_service] Cosmos client unavailable, trying JSON fallback")
            return _load_from_json_fallback()

        database = client.get_database_client(settings.cosmos_database)
        container = database.get_container_client(settings.cosmos_container)

        # Read master document
        doc = container.read_item(
            item="equivalencies_master",
            partition_key="Skill Equivalencies Master",  # Must match 'name' field
        )

        data = doc.get("data", {})
        formal_aliases = data.get("formal_aliases", {})
        equivalencies = data.get("equivalencies", {})

        _logger.debug(
            "[equivalency_service] Loaded %d formal aliases, %d equivalencies from Cosmos",
            len(formal_aliases),
            len(equivalencies),
        )

        return {
            "formal_aliases": formal_aliases,
            "equivalencies": equivalencies,
        }

    except Exception as exc:
        _logger.warning("[equivalency_service] Failed to load from Cosmos (%s), trying JSON fallback", str(exc)[:100])
        return _load_from_json_fallback()


def _load_from_json_fallback() -> dict:
    """Load equivalencies from skill_equivalencies.json as fallback.
    
    Returns empty dicts if JSON file not found or invalid.
    """
    try:
        from pathlib import Path
        
        # Try to find skill_equivalencies.json relative to this file
        json_path = Path(__file__).resolve().parent.parent.parent / "conf" / "skill_equivalencies.json"
        if not json_path.exists():
            _logger.warning("[equivalency_service] JSON fallback file not found: %s", json_path)
            return {"formal_aliases": {}, "equivalencies": {}}
        
        with open(json_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        
        formal_aliases = raw.get("_formal_aliases", {})
        equivalencies = {
            k: v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, list)
        }
        
        _logger.info(
            "[equivalency_service] Loaded %d formal aliases, %d equivalencies from JSON fallback",
            len(formal_aliases),
            len(equivalencies),
        )
        
        return {
            "formal_aliases": formal_aliases,
            "equivalencies": equivalencies,
        }
    except Exception as exc:
        _logger.error("[equivalency_service] JSON fallback failed: %s", exc)
        return {"formal_aliases": {}, "equivalencies": {}}


def get_equivalencies() -> dict:
    """Return equivalencies from cache or Cosmos, with auto-refresh on TTL expiry.

    Returns:
        {
            "formal_aliases": {...},  # formal name → canonical (exact match)
            "equivalencies": {...}    # canonical → [substitutes] (equivalent match)
        }
    """
    global _cache, _cache_timestamp

    now = time.time()
    if _cache is not None and (now - _cache_timestamp) < _CACHE_TTL_SECONDS:
        _logger.debug("[equivalency_service] Cache hit (age=%.1fs)", now - _cache_timestamp)
        return _cache

    _logger.debug("[equivalency_service] Cache miss or TTL expired — loading from Cosmos")
    _cache = _load_from_cosmos()
    _cache_timestamp = now
    return _cache


def invalidate_cache() -> None:
    """Invalidate in-memory cache after admin updates."""
    global _cache, _cache_timestamp
    _cache = None
    _cache_timestamp = 0.0
    _logger.info("[equivalency_service] Cache invalidated")


def save_equivalencies_to_cosmos(data: dict) -> bool:
    """Save or update the equivalencies master document in Cosmos.

    Args:
        data: {
            "formal_aliases": {...},
            "equivalencies": {...}
        }

    Returns:
        True if successful, False otherwise.
    """
    try:
        from app.config import settings
        from datetime import datetime

        client = _get_cosmos_client()
        if not client:
            _logger.error("[equivalency_service] Cosmos client unavailable for save")
            return False

        database = client.get_database_client(settings.cosmos_database)
        container = database.get_container_client(settings.cosmos_container)

        doc = {
            "id": "equivalencies_master",
            "name": "Skill Equivalencies Master",  # Required for /name partition key
            "type": "equivalencies",
            "version": 1,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "data": data,
        }

        container.upsert_item(doc)
        _logger.info(
            "[equivalency_service] Saved %d formal aliases, %d equivalencies to Cosmos",
            len(data.get("formal_aliases", {})),
            len(data.get("equivalencies", {})),
        )
        invalidate_cache()
        return True

    except Exception as exc:
        _logger.error("[equivalency_service] Failed to save to Cosmos: %s", exc)
        return False


def load_and_cache_from_json(json_path: str) -> bool:
    """Load equivalencies from JSON file and save to Cosmos (for deployment seeding).

    Args:
        json_path: Path to skill_equivalencies.json

    Returns:
        True if successful, False otherwise.
    """
    try:
        import json as _json
        from pathlib import Path

        path = Path(json_path)
        if not path.exists():
            _logger.error("[equivalency_service] JSON file not found: %s", json_path)
            return False

        with open(path, "r", encoding="utf-8") as fh:
            raw = _json.load(fh)

        data = {
            "formal_aliases": raw.get("_formal_aliases", {}),
            "equivalencies": {
                k: v for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, list)
            },
        }

        return save_equivalencies_to_cosmos(data)

    except Exception as exc:
        _logger.error("[equivalency_service] Failed to load JSON: %s", exc)
        return False
