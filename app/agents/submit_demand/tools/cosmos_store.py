"""
cosmos_store.py
---------------
Azure Cosmos DB store for all staffing platform data.

Containers:
  bench_roster       — candidate profiles        (partition key: /name)
  staffing_requests  — staffing demand requests   (partition key: /role_id)
  pending_candidates — partial records awaiting clarification (partition key: /cand_id)
  agent_prompts      — agent instruction prompts  (partition key: /prompt_key)

Public API:
  Bench roster:
    load_roster()                                        → list[dict]
    save_roster(roster)                                  → None
    upsert_candidate(entry)                              → str
    load_candidate(name, role)                           → dict | None

  Staffing requests:
    load_requests()                                      → list[dict]
    save_requests(requests)                              → None
    upsert_request(entry)                                → None
    load_request_by_id(role_id)                          → dict | None

  Pending candidates (awaiting clarification):
    save_pending_candidate(entry, sender_email, missing) → str  (CAND-XXXX id)
    load_pending_candidate(cand_id)                      → dict | None
    remove_pending_candidate(cand_id)                    → None

  Agent prompts:
    load_all_prompts()                                   → list[dict]
    load_prompt(key)                                     → dict | None
    upsert_prompt(key, content, description)             → dict

The Cosmos DB emulator uses a self-signed TLS cert; SSL verification is
disabled automatically for localhost/127.0.0.1 endpoints.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
COSMOS_ENDPOINT: str = os.getenv("COSMOS_ENDPOINT", "https://localhost:8081")
COSMOS_KEY: str = os.getenv("COSMOS_KEY", "")
COSMOS_DATABASE: str = os.getenv("COSMOS_DATABASE", "staffing_agent")
COSMOS_CONTAINER: str = os.getenv("COSMOS_CONTAINER", "bench_roster")
COSMOS_REQUESTS_CONTAINER: str = os.getenv("COSMOS_REQUESTS_CONTAINER", "staffing_requests")
COSMOS_PENDING_CONTAINER: str = os.getenv("COSMOS_PENDING_CONTAINER", "pending_candidates")
COSMOS_PROMPTS_CONTAINER: str = os.getenv("COSMOS_PROMPTS_CONTAINER", "agent_prompts")

_SYSTEM_KEYS = {"_rid", "_self", "_etag", "_attachments", "_ts"}

# ── Connection helpers ────────────────────────────────────────────────────────
# Singletons — created once and reused for the lifetime of the process.
# Creating a new CosmosClient (and calling create_database_if_not_exists /
# create_container_if_not_exists) on every request adds ~200-500ms of
# unnecessary network overhead and is the primary cause of slow / hanging
# demand uploads.

_CLIENT = None  # type: Any
_CONTAINERS: dict[str, Any] = {}


def _cosmos_client():
    global _CLIENT
    if _CLIENT is None:
        from azure.cosmos import CosmosClient
        is_emulator = "localhost" in COSMOS_ENDPOINT or "127.0.0.1" in COSMOS_ENDPOINT
        _CLIENT = CosmosClient(
            url=COSMOS_ENDPOINT,
            credential=COSMOS_KEY,
            connection_verify=not is_emulator,
        )
    return _CLIENT


def _get_container(container_name: str, partition_key_path: str):
    """Return a cached Cosmos container client, creating it on first call."""
    if container_name not in _CONTAINERS:
        from azure.cosmos import PartitionKey
        db = _cosmos_client().create_database_if_not_exists(id=COSMOS_DATABASE)
        _CONTAINERS[container_name] = db.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path=partition_key_path),
        )
    return _CONTAINERS[container_name]


def _get_roster_container():
    return _get_container(COSMOS_CONTAINER, "/name")


def _get_requests_container():
    return _get_container(COSMOS_REQUESTS_CONTAINER, "/role_id")


def _get_pending_container():
    return _get_container(COSMOS_PENDING_CONTAINER, "/cand_id")


def _get_prompts_container():
    return _get_container(COSMOS_PROMPTS_CONTAINER, "/prompt_key")


def _strip_system(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _SYSTEM_KEYS and k != "id"}


def _make_candidate_id(name: str, role: str) -> str:
    """Deterministic document id so upsert works as update-or-insert.

    Cosmos DB forbids these characters in an id: / \\ ? # and tab.
    We replace all of them (and spaces) with underscores, then collapse
    consecutive underscores so the id stays readable.
    """
    import re as _re
    raw = f"{name}::{role}".lower()
    sanitised = _re.sub(r"[/\\?#\t\s]+", "_", raw)   # replace illegal chars + whitespace
    sanitised = _re.sub(r"_+", "_", sanitised)         # collapse duplicate underscores
    return sanitised.strip("_")


# ── Bench Roster ──────────────────────────────────────────────────────────────

def load_roster() -> list[dict]:
    """Load all candidates from the bench_roster Cosmos container.

    Deduplicates by name — if the same person appears under multiple roles
    (legacy data), keeps the entry with the most recent ``updated_at``.
    """
    container = _get_roster_container()
    all_items = [_strip_system(item) for item in container.read_all_items()]

    seen: dict[str, dict] = {}   # normalised name → best entry
    for item in all_items:
        key = (item.get("name") or "").strip().lower()
        if not key:
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = item
        else:
            # Keep whichever was updated more recently
            if (item.get("updated_at") or "") > (existing.get("updated_at") or ""):
                seen[key] = item
    return list(seen.values())


def save_roster(roster: list[dict]) -> None:
    """Upsert every candidate entry into the bench_roster container."""
    container = _get_roster_container()
    for entry in roster:
        _upsert_candidate_doc(container, entry)


def _sanitise_partition_key(value: str) -> str:
    """Cosmos DB partition key values must not contain / \\ ? # or tab."""
    import re as _re
    return _re.sub(r"[/\\?#\t]+", "_", value)


def upsert_candidate(entry: dict) -> str:
    """Add or update a candidate. Returns: 'added:name' | 'updated:name' | 'unchanged:name' | 'skip:missing'.

    Dedup strategy — find existing doc in this order:
      1. Exact (name, role) match → same doc ID
      2. Same name with a *different* role → update existing doc (role may have changed)
      3. Same employee_id (if supplied) → update regardless of name/role
    This prevents the same person from having multiple entries.
    
    Note: When a candidate is updated, all match cache entries are invalidated
    since the bench roster has changed.
    """
    if not entry.get("name") or not entry.get("role"):
        return "skip:missing"

    container = _get_roster_container()
    doc_id = _make_candidate_id(entry["name"], entry["role"])
    pk = _sanitise_partition_key(entry["name"])

    # ── 1. Try exact (name, role) match first ───────────────────────────────
    try:
        existing = container.read_item(item=doc_id, partition_key=pk)
        changed = {k: v for k, v in entry.items() if v is not None and v != [] and existing.get(k) != v}
        if not changed:
            return f"unchanged:{entry['name']}"
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        existing.update(changed)
        existing["updated_at"] = now
        container.upsert_item(existing)
        
        # Invalidate all match cache since bench changed
        try:
            from app.services.match_cache import invalidate_all_cache  # noqa: PLC0415
            invalidate_all_cache()
        except Exception:
            pass  # Cache invalidation is optional
        
        return f"updated:{entry['name']}"
    except Exception:
        pass

    # ── 2. Check for same name with different role (partition key = name) ───
    existing_by_name = _find_existing_by_name(container, pk)
    if existing_by_name is None and entry.get("employee_id"):
        # ── 3. Check by employee_id across entire container ────────────────
        existing_by_name = _find_existing_by_eid(container, entry["employee_id"])

    if existing_by_name is not None:
        # Update existing doc in-place (preserves candidate_id, added_at, etc.)
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        for k, v in entry.items():
            if v is not None and v != []:
                existing_by_name[k] = v
        # Partition key (name) may have changed — keep old doc if name matches,
        # otherwise delete old and create new.
        old_pk = _sanitise_partition_key(existing_by_name.get("name", ""))
        existing_by_name["name"] = pk
        existing_by_name["role"] = entry["role"]
        new_doc_id = _make_candidate_id(pk, entry["role"])
        if existing_by_name["id"] != new_doc_id or old_pk != pk:
            # Role changed → delete old doc, insert with new id
            try:
                container.delete_item(item=existing_by_name["id"], partition_key=old_pk)
            except Exception:
                pass
            existing_by_name["id"] = new_doc_id
        existing_by_name["updated_at"] = now
        container.upsert_item(existing_by_name)
        
        # Invalidate all match cache since bench changed
        try:
            from app.services.match_cache import invalidate_all_cache  # noqa: PLC0415
            invalidate_all_cache()
        except Exception:
            pass  # Cache invalidation is optional
        
        return f"updated:{entry['name']}"

    # ── 4. Genuinely new candidate ──────────────────────────────────────────
    _upsert_candidate_doc(container, entry)
    
    # Invalidate all match cache since bench changed
    try:
        from app.services.match_cache import invalidate_all_cache  # noqa: PLC0415
        invalidate_all_cache()
    except Exception:
        pass  # Cache invalidation is optional
    
    return f"added:{entry['name']}"

def _find_existing_by_name(container: Any, pk: str) -> dict | None:
    """Return the first doc in bench_roster matching the given partition key (name)."""
    try:
        items = list(container.query_items(
            query="SELECT * FROM c WHERE c.name = @name",
            parameters=[{"name": "@name", "value": pk}],
            partition_key=pk,
            max_item_count=1,
        ))
        return items[0] if items else None
    except Exception:
        return None


def _find_existing_by_eid(container: Any, employee_id: str) -> dict | None:
    """Cross-partition lookup by employee_id (rare — only if name changed)."""
    try:
        items = list(container.query_items(
            query="SELECT * FROM c WHERE c.employee_id = @eid",
            parameters=[{"name": "@eid", "value": employee_id}],
            enable_cross_partition_query=True,
            max_item_count=1,
        ))
        return items[0] if items else None
    except Exception:
        return None


def load_candidate(name: str, role: str) -> dict | None:
    """Fetch a single candidate by name and role. Returns None if not found."""
    container = _get_roster_container()
    doc_id = _make_candidate_id(name, role)
    pk = _sanitise_partition_key(name)
    try:
        item = container.read_item(item=doc_id, partition_key=pk)
        return _strip_system(item)
    except Exception:
        return None


def patch_candidate(doc_id: str, name: str, fields: dict) -> dict | None:
    """Merge-patch a bench roster candidate. Falls back to insert if the doc was deleted."""
    _SYSTEM = {"id", "_rid", "_self", "_etag", "_attachments", "_ts"}
    container = _get_roster_container()
    pk = _sanitise_partition_key(name)
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    try:
        existing = container.read_item(item=doc_id, partition_key=pk)
        existing.update({k: v for k, v in fields.items() if k not in _SYSTEM})
        existing["updated_at"] = now
        container.upsert_item(existing)
        return _strip_system(existing)
    except Exception:
        pass

    # Document not found (e.g. was deleted) — recreate from submitted fields
    doc = {k: v for k, v in fields.items() if k not in _SYSTEM}
    doc["id"] = doc_id
    doc["name"] = name
    doc.setdefault("added_at", now)
    doc["updated_at"] = now
    try:
        container.upsert_item(doc)
        return _strip_system(doc)
    except Exception:
        return None


def delete_candidate(doc_id: str, name: str) -> bool:
    """Delete a candidate from bench_roster by document ID and name (partition key).

    Returns True if deleted, False if not found.
    """
    container = _get_roster_container()
    pk = _sanitise_partition_key(name)
    try:
        container.delete_item(item=doc_id, partition_key=pk)
        return True
    except Exception:
        return False


def _upsert_candidate_doc(container: Any, entry: dict) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    doc = {**entry, "id": _make_candidate_id(entry["name"], entry["role"])}
    # Cosmos partition key (name) must not contain / \ ? # or tab
    doc["name"] = _sanitise_partition_key(entry["name"])
    if "added_at" not in doc:
        doc["added_at"] = now
    # Assign a unique sequential candidate_id on first save so every bench
    # roster entry has a human-readable identifier (CAND-0001, CAND-0002, …).
    if not doc.get("candidate_id"):
        doc["candidate_id"] = _next_bench_candidate_id()
    doc["updated_at"] = now
    container.upsert_item(doc)


# ── Staffing Requests ─────────────────────────────────────────────────────────

def load_requests() -> list[dict]:
    """Load all staffing requests from the staffing_requests Cosmos container."""
    container = _get_requests_container()
    return [_strip_system(item) for item in container.read_all_items()]


def save_requests(requests: list[dict]) -> None:
    """Upsert every request entry into the staffing_requests container."""
    container = _get_requests_container()
    for req in requests:
        _upsert_request_doc(container, req)


def upsert_request(entry: dict) -> None:
    """Add or update a single staffing request document."""
    container = _get_requests_container()
    _upsert_request_doc(container, entry)


def load_request_by_id(role_id: str) -> dict | None:
    """Fetch a single staffing request by role_id. Returns None if not found."""
    container = _get_requests_container()
    try:
        item = container.read_item(item=role_id, partition_key=role_id)
        return _strip_system(item)
    except Exception:
        return None


def patch_request(role_id: str, fields: dict) -> dict | None:
    """Merge-patch a staffing request. Returns the updated doc or None if not found."""
    _SYSTEM = {"id", "_rid", "_self", "_etag", "_attachments", "_ts"}
    container = _get_requests_container()
    try:
        existing = container.read_item(item=role_id, partition_key=role_id)
        for k, v in fields.items():
            if k in _SYSTEM:
                continue
            # Deep-merge dicts so nested sub-fields (staffing_intake, skill_analysis)
            # are merged rather than replaced wholesale.
            if isinstance(v, dict) and isinstance(existing.get(k), dict):
                existing[k] = {**existing[k], **v}
            else:
                existing[k] = v
        existing["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        container.upsert_item(existing)
        return _strip_system(existing)
    except Exception:
        return None


def delete_request(role_id: str) -> bool:
    """Delete a staffing request by role_id. Returns True if deleted, False if not found."""
    container = _get_requests_container()
    try:
        container.delete_item(item=role_id, partition_key=role_id)
        return True
    except Exception:
        return False


def delete_all_candidates() -> int:
    """Delete every document in the bench_roster container. Returns count deleted."""
    container = _get_roster_container()
    items = list(container.read_all_items())
    deleted = 0
    for item in items:
        try:
            container.delete_item(item=item["id"], partition_key=item.get("name", ""))
            deleted += 1
        except Exception:
            pass
    return deleted


def delete_all_requests() -> int:
    """Delete every document in the staffing_requests container. Returns count deleted."""
    container = _get_requests_container()
    items = list(container.read_all_items())
    deleted = 0
    for item in items:
        try:
            container.delete_item(item=item["id"], partition_key=item.get("role_id", ""))
            deleted += 1
        except Exception:
            pass
    return deleted


def _upsert_request_doc(container: Any, entry: dict) -> None:
    """Upsert a staffing request, always overwriting the existing role_id document."""
    doc = {**entry, "id": entry["role_id"]}
    container.upsert_item(doc)


# ── Pending Candidates (awaiting clarification) ───────────────────────────────

def _next_cand_id() -> str:
    container = _get_pending_container()
    nums = []
    for item in container.read_all_items():
        cid = item.get("cand_id", "") or ""
        if cid.upper().startswith("CAND-"):
            try:
                nums.append(int(cid.split("-")[1]))
            except (IndexError, ValueError):
                pass
    return f"CAND-{(max(nums, default=0) + 1):04d}"


def _next_bench_candidate_id() -> str:
    """Generate the next sequential candidate_id for the bench roster (e.g. CAND-0006).

    Reads all existing bench roster entries and finds the highest numeric CAND-XXXX
    so new candidates always get a unique, incrementing ID.
    """
    container = _get_roster_container()
    nums = []
    for item in container.read_all_items():
        cid = item.get("candidate_id", "") or ""
        if cid.upper().startswith("CAND-"):
            try:
                nums.append(int(cid.split("-")[1]))
            except (IndexError, ValueError):
                pass
    return f"CAND-{(max(nums, default=0) + 1):04d}"


def save_pending_candidate(entry: dict, sender_email: str, missing_fields: list) -> str:
    """Save a partial candidate record awaiting clarification. Returns the CAND-XXXX id."""
    cand_id = _next_cand_id()
    upsert_pending_candidate(cand_id, entry, sender_email, missing_fields)
    return cand_id


def upsert_pending_candidate(cand_id: str, entry: dict, sender_email: str, missing_fields: list) -> str:
    """Create or update a pending candidate record for an existing CAND-XXXX id.

    Increments ``clarification_count`` on each update so callers can decide when
    to escalate to a human admin.
    """
    container = _get_pending_container()
    # Preserve existing created_at and increment clarification_count
    existing_count = 0
    existing_created = datetime.now().isoformat(timespec="seconds")
    try:
        existing = container.read_item(item=cand_id, partition_key=cand_id)
        existing_count = existing.get("clarification_count", 0)
        existing_created = existing.get("created_at", existing_created)
    except Exception:
        pass

    doc = {
        "id": cand_id,
        "cand_id": cand_id,
        "sender_email": sender_email,
        "missing_fields": missing_fields,
        "entry": entry,
        "clarification_count": existing_count + 1,
        "created_at": existing_created,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    container.upsert_item(doc)
    return cand_id


def load_pending_candidate(cand_id: str) -> dict | None:
    """Load a pending candidate by CAND-XXXX id. Returns None if not found."""
    container = _get_pending_container()
    for cid in [cand_id, cand_id.upper(), cand_id.lower()]:
        try:
            item = container.read_item(item=cid, partition_key=cid)
            return _strip_system(item)
        except Exception:
            pass
    return None


def remove_pending_candidate(cand_id: str) -> None:
    """Remove a completed pending candidate record."""
    container = _get_pending_container()
    for cid in [cand_id, cand_id.upper(), cand_id.lower()]:
        try:
            container.delete_item(item=cid, partition_key=cid)
            return
        except Exception:
            pass


# ── Agent Prompts ─────────────────────────────────────────────────────────────

def load_all_prompts() -> list[dict]:
    """Return all agent prompt documents from the agent_prompts container."""
    container = _get_prompts_container()
    return [_strip_system(item) for item in container.read_all_items()]


def load_prompt(key: str) -> dict | None:
    """Fetch a single prompt document by its prompt_key. Returns None if not found."""
    container = _get_prompts_container()
    try:
        item = container.read_item(item=key, partition_key=key)
        return _strip_system(item)
    except Exception:
        return None


def upsert_prompt(key: str, content: str, description: str = "") -> dict:
    """Add or update a prompt document. Bumps version on each update.

    Args:
        key         : Unique prompt key, e.g. "RESUME_PARSER_INSTRUCTION".
        content     : Full prompt text.
        description : Short human-readable description of the prompt's purpose.

    Returns:
        The saved prompt document (without Cosmos system keys).
    """
    container = _get_prompts_container()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    existing_version = 0
    try:
        existing = container.read_item(item=key, partition_key=key)
        existing_version = existing.get("version", 0)
    except Exception:
        pass  # New prompt — starts at version 1

    doc = {
        "id": key,
        "prompt_key": key,
        "content": content,
        "description": description,
        "version": existing_version + 1,
        "updated_at": now,
    }
    container.upsert_item(doc)
    return _strip_system(doc)


# ── Agent Configs ─────────────────────────────────────────────────────────────

COSMOS_AGENT_CONFIGS_CONTAINER: str = os.getenv("COSMOS_AGENT_CONFIGS_CONTAINER", "agent_configs")


def _get_agent_configs_container():
    from azure.cosmos import PartitionKey
    db = _cosmos_client().create_database_if_not_exists(id=COSMOS_DATABASE)
    return db.create_container_if_not_exists(
        id=COSMOS_AGENT_CONFIGS_CONTAINER,
        partition_key=PartitionKey(path="/agent_name"),
    )


def list_agent_configs() -> list[dict]:
    """Return all agent config documents."""
    container = _get_agent_configs_container()
    return [_strip_system(item) for item in container.read_all_items()]


def load_agent_config(agent_name: str) -> dict | None:
    """Fetch a single agent config by agent_name. Returns None if not found."""
    container = _get_agent_configs_container()
    try:
        item = container.read_item(item=agent_name, partition_key=agent_name)
        return _strip_system(item)
    except Exception:
        return None


def upsert_agent_config(config: dict) -> dict:
    """Add or update an agent config document. Bumps version on each update.

    Required keys in config: agent_name, model, description, prompt_key,
    tools (list[str]), sub_agents (list[str]), enabled (bool).

    Returns the saved document (without Cosmos system keys).
    """
    agent_name = config.get("agent_name")
    if not agent_name:
        raise ValueError("agent_name is required")

    container = _get_agent_configs_container()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    existing_version = 0
    try:
        existing = container.read_item(item=agent_name, partition_key=agent_name)
        existing_version = existing.get("version", 0)
    except Exception:
        pass  # New config — starts at version 1

    doc: dict = {
        **config,
        "id": agent_name,
        "agent_name": agent_name,
        "version": existing_version + 1,
        "updated_at": now,
    }
    container.upsert_item(doc)
    return _strip_system(doc)


def delete_agent_config(agent_name: str) -> bool:
    """Delete an agent config document from Cosmos by agent_name.

    Returns True if the document was deleted, False if it was not found.
    """
    container = _get_agent_configs_container()
    try:
        container.delete_item(item=agent_name, partition_key=agent_name)
        return True
    except Exception:
        return False


# ── Report Cache ──────────────────────────────────────────────────────────────
# Persists scored bench-match results across server restarts (scale-to-zero).
# Cosmos has a 2 MB per-document limit; the full scoring result is ~24 MB so
# we use Azure Blob Storage instead (no size limit, same connection string).
#
# Blob container: "report-cache"  (created automatically on first write)
# Blob name:      <cache_key>.json.gz  (gzip-compressed JSON)
# Explicit invalidation (bench/demand mutations) is the primary eviction
# mechanism; blobs also carry a 24h Content-MD5 for integrity.

_REPORT_CACHE_BLOB_CONTAINER = os.getenv("REPORT_CACHE_BLOB_CONTAINER", "report-cache")


def _get_blob_service_client():
    from azure.storage.blob import BlobServiceClient
    conn_str = os.getenv(
        "AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true"
    )
    return BlobServiceClient.from_connection_string(conn_str)


def _report_blob_name(cache_key: str) -> str:
    """Convert a cache key to a safe blob name."""
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "_", cache_key)
    return f"{safe}.json.gz"


def load_report_cache(cache_key: str) -> dict | None:
    """Return the cached scored result for *cache_key*, or None on miss/error."""
    import gzip
    import json as _json
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        svc = _get_blob_service_client()
        blob = svc.get_blob_client(
            container=_REPORT_CACHE_BLOB_CONTAINER,
            blob=_report_blob_name(cache_key),
        )
        data = blob.download_blob().readall()
        result = _json.loads(gzip.decompress(data).decode("utf-8"))
        _log.info("[report_cache] Blob hit for key=%r (%d bytes compressed)", cache_key, len(data))
        return result
    except Exception as exc:
        _log.debug("[report_cache] Blob miss for key=%r: %s", cache_key, exc)
        return None


def save_report_cache(cache_key: str, result: dict) -> None:
    """Persist a scored result to Azure Blob Storage (gzip-compressed JSON)."""
    import gzip
    import json as _json
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        svc = _get_blob_service_client()
        # Create container if it doesn't exist
        container_client = svc.get_container_client(_REPORT_CACHE_BLOB_CONTAINER)
        try:
            container_client.create_container()
        except Exception:
            pass  # already exists
        raw = _json.dumps(result, ensure_ascii=False).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=6)
        container_client.upload_blob(
            name=_report_blob_name(cache_key),
            data=compressed,
            overwrite=True,
            content_settings=None,
        )
        _log.info(
            "[report_cache] Saved to Blob key=%r  raw=%dMB  compressed=%dKB",
            cache_key,
            len(raw) // (1024 * 1024),
            len(compressed) // 1024,
        )
    except Exception as exc:
        _log.warning("[report_cache] Failed to save to Blob key=%r: %s", cache_key, exc)


def delete_report_cache_all() -> int:
    """Delete all report-cache blobs.  Returns count deleted."""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        svc = _get_blob_service_client()
        container_client = svc.get_container_client(_REPORT_CACHE_BLOB_CONTAINER)
        blobs = list(container_client.list_blobs())
        count = 0
        for b in blobs:
            container_client.delete_blob(b.name)
            count += 1
        _log.info("[report_cache] Cleared %d blob cache entries", count)
        return count
    except Exception as exc:
        _log.warning("[report_cache] Failed to clear Blob cache: %s", exc)
        return 0

