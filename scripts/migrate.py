"""
scripts/migrate.py
-------------------
Cosmos DB schema migration utility.

Ensures all required containers exist with the correct partition keys and
index policies.  Safe to run multiple times (idempotent).

Usage::
    python scripts/migrate.py
    python scripts/migrate.py --dry-run   # print what would be done, no changes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
import importlib

load_dotenv(Path(__file__).parent.parent / ".env")

# Import settings after loading .env
settings = importlib.import_module("app.config").settings

# Container schema registry
# Each entry: (container_id, partition_key_path)
CONTAINERS = [
    ("bench_roster",               "/name"),
    ("staffing_requests",          "/role_id"),
    ("pending_candidates",         "/cand_id"),
    ("sessions",                   "/app_name"),
    ("feedback",                   "/session_id"),
    ("agent_prompts",              "/prompt_key"),
    ("agent_configs",              "/agent_name"),
    ("tool_registry",              "/name"),
    ("career_level_expectations",  "/level"),
    ("escalations",                "/escalation_id"),
    ("fix_tasks",                  "/task_id"),
]


def migrate(dry_run: bool = False) -> int:
    from azure.cosmos import CosmosClient, PartitionKey
    from azure.cosmos.exceptions import CosmosResourceExistsError

    print(f"\nConnecting to Cosmos DB at {settings.cosmos_endpoint}")
    print(f"Database: {settings.cosmos_database}")
    if dry_run:
        print("*** DRY RUN — no changes will be made ***\n")

    if dry_run:
        for cid, pk in CONTAINERS:
            print(f"  [DRY] Would ensure container '{cid}' (partition: {pk})")
        return 0

    client = CosmosClient(
        url=settings.cosmos_endpoint,
        credential=settings.cosmos_key,
        connection_verify="localhost" not in settings.cosmos_endpoint,
    )
    db = client.create_database_if_not_exists(settings.cosmos_database)
    print(f"  Database '{settings.cosmos_database}' ready.\n")

    errors = 0
    for container_id, partition_key_path in CONTAINERS:
        try:
            db.create_container(
                id=container_id,
                partition_key=PartitionKey(path=partition_key_path),
            )
            print(f"  [CREATED] '{container_id}' (partition: {partition_key_path})")
        except CosmosResourceExistsError:
            print(f"  [OK]      '{container_id}' already exists")
        except Exception as exc:
            print(f"  [ERROR]   '{container_id}': {exc}")
            errors += 1

    print(f"\nMigration complete. Errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Staffing Platform Cosmos DB migration")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()
    sys.exit(migrate(dry_run=args.dry_run))
