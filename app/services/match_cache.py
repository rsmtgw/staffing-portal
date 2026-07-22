"""
services/match_cache.py
-----------------------
Cache layer for matching/scoring results to avoid redundant LLM calls.

Scoring is expensive (multiple LLM calls per demand). This module caches match
results using demand role_id as key and stores with timestamp. Results are
considered valid if demand hasn't been modified since last scoring.

Cache behavior:
- Check if cached results exist for role_id
- Return cached results if demand hasn't changed (timestamp-based)
- Invalidate cache on demand update
- Store new results with timestamp
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

_logger = logging.getLogger(__name__)

# In-memory cache: {role_id: {"timestamp": ISO8601, "demand_hash": str, "results": [...]}}
_match_cache: dict[str, dict[str, Any]] = {}

# TTL for cache entries (24 hours)
_CACHE_TTL_HOURS = 24


def _hash_demand_state(demand: dict) -> str:
    """Create a hash of the demand state to detect changes.
    
    Hashes only fields that affect scoring:
    - role_description, role_title
    - skill_analysis (primary/secondary/other/inferred skills)
    - career_level, demand_capability, etc.
    
    Excludes: created_at, updated_at, timestamps, metadata
    """
    scoring_fields = {
        "role_title": demand.get("role_title") or "",
        "role_description": demand.get("role_description") or "",
        "skill_analysis": demand.get("skill_analysis") or {},
        "career_level": demand.get("career_level") or "",
        "accenture_level": demand.get("accenture_level") or "",
        "demand_capability": demand.get("demand_capability") or "",
        "demand_industry": demand.get("demand_industry") or "",
        "primary_skill": demand.get("primary_skill") or "",
        "secondary_skills": demand.get("secondary_skills") or [],
        "must_have_skills": demand.get("must_have_skills") or [],
        "nice_to_have_skills": demand.get("nice_to_have_skills") or [],
    }
    
    # JSON dump with sorted keys for consistent hashing
    json_str = json.dumps(scoring_fields, sort_keys=True, default=str)
    return hashlib.sha256(json_str.encode()).hexdigest()


def get_cached_match_results(
    role_id: str,
    demand: dict,
    ttl_hours: int = _CACHE_TTL_HOURS,
) -> list[dict] | None:
    """Return cached match results if they exist and are still valid.
    
    Args:
        role_id: The demand role_id
        demand: Current demand document (to check for changes)
        ttl_hours: Cache TTL in hours (default 24)
    
    Returns:
        Cached match results (list of MatchResult dicts) if valid, else None
    """
    if role_id not in _match_cache:
        _logger.debug(f"[match_cache] Cache miss: {role_id} not in cache")
        return None
    
    cached = _match_cache[role_id]
    
    # Check TTL
    cached_at = cached.get("timestamp")
    if cached_at:
        try:
            cached_dt = datetime.fromisoformat(cached_at)
            if datetime.utcnow() - cached_dt > timedelta(hours=ttl_hours):
                _logger.info(f"[match_cache] Cache expired for {role_id} (TTL {ttl_hours}h exceeded)")
                del _match_cache[role_id]
                return None
        except Exception as exc:
            _logger.warning(f"[match_cache] Could not parse cached timestamp for {role_id}: {exc}")
            return None
    
    # Check if demand has changed
    current_hash = _hash_demand_state(demand)
    cached_hash = cached.get("demand_hash")
    if current_hash != cached_hash:
        _logger.info(f"[match_cache] Cache invalid for {role_id} — demand changed")
        del _match_cache[role_id]
        return None
    
    _logger.info(f"[match_cache] Cache HIT for {role_id} (age={cached.get('timestamp')})")
    return cached.get("results", [])


def set_cached_match_results(
    role_id: str,
    demand: dict,
    results: list[dict],
) -> None:
    """Store match results in cache with demand state hash and timestamp.
    
    Args:
        role_id: The demand role_id
        demand: The demand document
        results: List of MatchResult dicts to cache
    """
    try:
        demand_hash = _hash_demand_state(demand)
        _match_cache[role_id] = {
            "timestamp": datetime.utcnow().isoformat(),
            "demand_hash": demand_hash,
            "results": results,
        }
        _logger.info(f"[match_cache] Cached {len(results)} results for {role_id}")
    except Exception as exc:
        _logger.warning(f"[match_cache] Could not cache results for {role_id}: {exc}")


def invalidate_cache(role_id: str) -> None:
    """Manually invalidate cache entry for a demand.
    
    Call this when a demand is updated.
    """
    if role_id in _match_cache:
        del _match_cache[role_id]
        _logger.info(f"[match_cache] Invalidated cache for {role_id}")


def invalidate_all_cache() -> None:
    """Clear entire match cache.
    
    Call this when bench roster is updated.
    """
    count = len(_match_cache)
    _match_cache.clear()
    _logger.info(f"[match_cache] Cleared entire cache ({count} entries)")


def get_cache_stats() -> dict[str, Any]:
    """Return cache statistics for monitoring."""
    return {
        "entries": len(_match_cache),
        "role_ids": list(_match_cache.keys()),
        "memory_est_bytes": sum(
            len(json.dumps(v).encode())
            for v in _match_cache.values()
        ),
    }
