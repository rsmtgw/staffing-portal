"""
app/services/job_service.py
----------------------------
Persistent job tracking service for CSV uploads.
Stores job state in Cosmos DB so uploads survive pod restarts
and can be paused/stopped/resumed.

Falls back to an in-memory store when Cosmos DB is unavailable so that
uploads are never blocked by a database connectivity issue.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.config import settings

_logger = logging.getLogger(__name__)

_JOBS_CONTAINER_ID = "upload_jobs"
_JOBS_PARTITION_KEY = "/job_id"

# In-memory fallback store: job_id -> job_doc
_memory_store: dict[str, dict] = {}


def _cosmos_client():
    """Get Cosmos DB client."""
    from azure.cosmos import CosmosClient
    is_emulator = "localhost" in settings.cosmos_endpoint or "127.0.0.1" in settings.cosmos_endpoint
    return CosmosClient(
        url=settings.cosmos_endpoint,
        credential=settings.cosmos_key,
        connection_verify=not is_emulator,
    )


def _get_jobs_container():
    """Get or create the upload_jobs container. Raises on failure."""
    from azure.cosmos import PartitionKey
    db = _cosmos_client().create_database_if_not_exists(id=settings.cosmos_database)
    return db.create_container_if_not_exists(
        id=_JOBS_CONTAINER_ID,
        partition_key=PartitionKey(path=_JOBS_PARTITION_KEY),
    )


def _cosmos_available() -> bool:
    """Quick probe to see if Cosmos is reachable."""
    try:
        _get_jobs_container()
        return True
    except Exception as exc:
        _logger.warning("[job_service] Cosmos unavailable (%s). Using in-memory store.", exc)
        return False


class UploadJobService:
    """Manages persistent upload job state in Cosmos DB."""

    @staticmethod
    def create_job(
        upload_type: str,  # "demand" or "bench"
        total_rows: int,
        filename: str = "",
    ) -> str:
        """Create a new upload job. Returns job_id. CSV content passed separately to background task."""
        job_id = f"{upload_type}-{int(time.time() * 1000)}-{str(uuid4())[:8]}"
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        
        job_doc = {
            "id": job_id,
            "job_id": job_id,
            "upload_type": upload_type,
            "status": "running",  # running, paused, stopped, done, error
            "progress": 0,
            "total": total_rows,
            "errors": [],
            "warnings": [],
            "result": None,
            "error_message": None,
            "filename": filename,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "paused_at": None,
            # Control flags
            "pause_requested": False,
            "stop_requested": False,
            "resume_requested": False,
        }
        
        container = _get_jobs_container()
        container.create_item(body=job_doc)
        return job_id

    @staticmethod
    def get_job(job_id: str) -> dict | None:
        """Get job details by ID."""
        try:
            container = _get_jobs_container()
            return container.read_item(item=job_id, partition_key=job_id)
        except Exception:
            return None

    @staticmethod
    def update_progress(job_id: str, progress: int) -> None:
        """Update job progress counter."""
        job = UploadJobService.get_job(job_id)
        if job:
            now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            job["progress"] = progress
            job["updated_at"] = now
            container = _get_jobs_container()
            container.upsert_item(job)

    @staticmethod
    def update_job_status(
        job_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Mark job as done with result or error."""
        job = UploadJobService.get_job(job_id)
        if job:
            now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            job["status"] = status
            job["result"] = result
            job["error_message"] = error
            job["completed_at"] = now if status in ("done", "stopped", "error") else None
            job["updated_at"] = now
            job["pause_requested"] = False
            job["stop_requested"] = False
            job["resume_requested"] = False
            container = _get_jobs_container()
            container.upsert_item(job)

    @staticmethod
    def pause_job(job_id: str) -> bool:
        """Request job to pause. Worker checks this flag."""
        job = UploadJobService.get_job(job_id)
        if job and job["status"] == "running":
            job["pause_requested"] = True
            job["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            container = _get_jobs_container()
            container.upsert_item(job)
            return True
        return False

    @staticmethod
    def resume_job(job_id: str) -> bool:
        """Resume a paused job."""
        job = UploadJobService.get_job(job_id)
        if job and job["status"] == "paused":
            job["status"] = "running"
            job["resume_requested"] = True
            job["paused_at"] = None
            job["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            container = _get_jobs_container()
            container.upsert_item(job)
            return True
        return False

    @staticmethod
    def stop_job(job_id: str) -> bool:
        """Stop a running or paused job."""
        job = UploadJobService.get_job(job_id)
        if job and job["status"] in ("running", "paused"):
            job["stop_requested"] = True
            job["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            container = _get_jobs_container()
            container.upsert_item(job)
            return True
        return False

    @staticmethod
    def check_pause_requested(job_id: str) -> bool:
        """Check if pause has been requested. Worker should call this."""
        job = UploadJobService.get_job(job_id)
        return job and job.get("pause_requested", False)

    @staticmethod
    def check_stop_requested(job_id: str) -> bool:
        """Check if stop has been requested. Worker should call this."""
        job = UploadJobService.get_job(job_id)
        return job and job.get("stop_requested", False)

    @staticmethod
    def handle_pause(job_id: str) -> None:
        """Pause the job (called by worker when pause is requested)."""
        job = UploadJobService.get_job(job_id)
        if job:
            job["status"] = "paused"
            job["paused_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            job["pause_requested"] = False
            job["updated_at"] = job["paused_at"]
            container = _get_jobs_container()
            container.upsert_item(job)

    @staticmethod
    def list_active_jobs(upload_type: str | None = None) -> list[dict]:
        """List all active jobs (running or paused)."""
        container = _get_jobs_container()
        query = "SELECT * FROM c WHERE c.status IN ('running', 'paused')"
        if upload_type:
            query += f" AND c.upload_type = '{upload_type}'"
        query += " ORDER BY c.started_at DESC"
        
        return list(container.query_items(query=query, enable_cross_partition_query=True))

    @staticmethod
    def list_jobs_by_type(upload_type: str, limit: int = 50) -> list[dict]:
        """List recent jobs for a given type (demand or bench)."""
        container = _get_jobs_container()
        query = f"""
            SELECT * FROM c 
            WHERE c.upload_type = '{upload_type}'
            ORDER BY c.started_at DESC
            OFFSET 0 LIMIT {limit}
        """
        return list(container.query_items(query=query, enable_cross_partition_query=True))

    @staticmethod
    def add_error(job_id: str, error: str) -> None:
        """Append error to job's error list."""
        job = UploadJobService.get_job(job_id)
        if job:
            if not job.get("errors"):
                job["errors"] = []
            if len(job["errors"]) < 20:  # Keep only first 20 errors
                job["errors"].append(error)
            job["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            container = _get_jobs_container()
            container.upsert_item(job)

    @staticmethod
    def add_warning(job_id: str, warning: str) -> None:
        """Append warning to job's warning list."""
        job = UploadJobService.get_job(job_id)
        if job:
            if not job.get("warnings"):
                job["warnings"] = []
            if len(job["warnings"]) < 10:  # Keep only first 10 warnings
                job["warnings"].append(warning)
            job["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            container = _get_jobs_container()
            container.upsert_item(job)
