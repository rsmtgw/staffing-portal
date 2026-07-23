"""
app/services/eval_cache.py
--------------------------
Cosmos DB-backed LLM evaluation cache.

Drop-in replacement for SkillKnowledgeBase's LanceDB cache — implements only
the two methods the AI-SM evaluator calls:
  kb.get_llm_evaluation(cache_key)  → dict | None
  kb.save_llm_evaluation(...)       → None

Passed as `kb=` to match_candidate_to_role() so borderline LLM scores
are cached permanently in Cosmos rather than the ephemeral container filesystem.
"""
from __future__ import annotations

import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

_CONTAINER_NAME = "llm_eval_cache"
_PARTITION_KEY  = "/cache_key"


def _get_container():
    from app.agents.submit_demand.tools.cosmos_store import _cosmos_client
    from azure.cosmos import PartitionKey
    import os
    client = _cosmos_client()
    db = client.create_database_if_not_exists(
        id=os.environ.get("COSMOS_DATABASE", "staffing_agent")
    )
    return db.create_container_if_not_exists(
        id=_CONTAINER_NAME,
        partition_key=PartitionKey(path=_PARTITION_KEY),
    )


class CosmosEvalCache:
    """Cosmos-backed cache with the same interface as SkillKnowledgeBase cache methods."""

    def get_llm_evaluation(self, cache_key: str) -> dict | None:
        if not cache_key:
            return None
        try:
            container = _get_container()
            doc = container.read_item(item=cache_key, partition_key=cache_key)
            return {"adjustment": doc["adjustment"], "reason": doc.get("reason", "")}
        except Exception:
            return None

    def save_llm_evaluation(
        self,
        cache_key: str,
        employee_id: str,
        role_id: str,
        adjustment: float,
        reason: str,
    ) -> None:
        if not cache_key:
            return
        try:
            container = _get_container()
            container.upsert_item({
                "id":          cache_key,
                "cache_key":   cache_key,
                "employee_id": employee_id,
                "role_id":     role_id,
                "adjustment":  float(adjustment),
                "reason":      reason,
                "timestamp":   datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
        except Exception as exc:
            _logger.warning("[eval_cache] Failed to save evaluation: %s", exc)


# Module-level singleton — one connection pool for the process lifetime
_cache: CosmosEvalCache | None = None


def get_eval_cache() -> CosmosEvalCache:
    global _cache
    if _cache is None:
        _cache = CosmosEvalCache()
    return _cache
