"""
scripts/seed.py
---------------
Seed the Cosmos DB bench roster and staffing requests with sample data for
development and testing.  Replaces manual JSON file editing.

Usage::
    python scripts/seed.py
    python scripts/seed.py --force   # re-seed even if data already exists
    python scripts/seed.py --only roster
    python scripts/seed.py --only requests
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add repo root to sys.path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
import importlib

load_dotenv(Path(__file__).parent.parent / ".env")

# Import settings after loading .env
settings = importlib.import_module("app.config").settings

SAMPLE_ROSTER = [
    {
        "id": "seed-001",
        "name": "Priya Sharma",
        "role": "Java Architect",
        "experience_years": 12,
        "location": "Bangalore",
        "availability": "Available now",
        "skills": ["Java", "Spring Boot", "Kubernetes", "Microservices", "AWS"],
        "contact": "priya.sharma@example.com",
    },
    {
        "id": "seed-002",
        "name": "Carlos Ruiz",
        "role": "Python Data Engineer",
        "experience_years": 7,
        "location": "Mexico City",
        "availability": "Available from May 1 2026",
        "skills": ["Python", "Apache Spark", "dbt", "BigQuery", "Airflow"],
        "contact": "carlos.ruiz@example.com",
    },
    {
        "id": "seed-003",
        "name": "Aiko Tanaka",
        "role": "Cloud Architect",
        "experience_years": 10,
        "location": "Tokyo",
        "availability": "Available now",
        "skills": ["Azure", "Terraform", "Kubernetes", "DevOps", "AKS"],
        "contact": "aiko.tanaka@example.com",
    },
    {
        "id": "seed-004",
        "name": "Daniel Osei",
        "role": "Frontend Developer",
        "experience_years": 5,
        "location": "Lagos",
        "availability": "Available now",
        "skills": ["React", "TypeScript", "Next.js", "Tailwind CSS", "GraphQL"],
        "contact": "daniel.osei@example.com",
    },
    {
        "id": "seed-005",
        "name": "Fatima Al-Rashid",
        "role": "ML Engineer",
        "experience_years": 8,
        "location": "Dubai",
        "availability": "Available from June 1 2026",
        "skills": ["Python", "TensorFlow", "PyTorch", "MLflow", "Azure ML"],
        "contact": "fatima.alrashid@example.com",
    },
]

_CAREER_LEVELS_FILE = Path(__file__).parent.parent / "conf" / "career_levels.json"
CAREER_LEVEL_DATA: list[dict] = json.loads(_CAREER_LEVELS_FILE.read_text(encoding="utf-8"))

SAMPLE_REQUESTS = [
    {
        "id": "ROLE-001",
        "role_id": "ROLE-001",
        "role": "Java Architect",
        "location": "Bangalore",
        "experience_years": 10,
        "skills": ["Java", "Spring Boot", "Microservices"],
        "start_date": "2026-05-01",
        "duration_months": 6,
        "allocation_percentage": 100,
        "primary_contact": "hiring@client.com",
        "availability": "Available now",
        "status": "pending",
        "readiness": "READY",
        "missing_fields": [],
    },
]


def _seed_career_levels(container, force: bool) -> int:
    seeded = 0
    for doc in CAREER_LEVEL_DATA:
        try:
            if not force:
                try:
                    container.read_item(item=doc["id"], partition_key=doc["level"])
                    print(f"  [SKIP] {doc['level']} already exists")
                    continue
                except Exception:
                    pass
            container.upsert_item(doc)
            print(f"  [OK]   Seeded: {doc['level']} — {doc['title']}")
            seeded += 1
        except Exception as exc:
            print(f"  [ERR]  {doc['level']}: {exc}")
    return seeded


def _seed_roster(container, force: bool) -> int:
    seeded = 0
    for candidate in SAMPLE_ROSTER:
        try:
            if not force:
                try:
                    container.read_item(item=candidate["id"], partition_key=candidate["name"])
                    print(f"  [SKIP] {candidate['name']} already exists")
                    continue
                except Exception:
                    pass
            container.upsert_item(candidate)
            print(f"  [OK]   Seeded: {candidate['name']} — {candidate['role']}")
            seeded += 1
        except Exception as exc:
            print(f"  [ERR]  {candidate['name']}: {exc}")
    return seeded


def _seed_requests(container, force: bool) -> int:
    seeded = 0
    for req in SAMPLE_REQUESTS:
        try:
            if not force:
                try:
                    container.read_item(item=req["id"], partition_key=req["role_id"])
                    print(f"  [SKIP] {req['role_id']} already exists")
                    continue
                except Exception:
                    pass
            container.upsert_item(req)
            print(f"  [OK]   Seeded: {req['role_id']} — {req['role']}")
            seeded += 1
        except Exception as exc:
            print(f"  [ERR]  {req['role_id']}: {exc}")
    return seeded


def _seed_equivalencies(force: bool) -> int:
    """Load equivalencies from conf/skill_equivalencies.json and save to Cosmos.

    Uses app.services.equivalency_service.load_and_cache_from_json() to handle
    the actual Cosmos save.
    """
    equiv_json_path = Path(__file__).parent.parent / "conf" / "skill_equivalencies.json"
    if not equiv_json_path.exists():
        print(f"  [ERR]  Equivalencies JSON not found: {equiv_json_path}")
        return 0

    try:
        from app.services.equivalency_service import load_and_cache_from_json

        success = load_and_cache_from_json(str(equiv_json_path))
        if success:
            print(f"  [OK]   Seeded equivalencies from {equiv_json_path.name}")
            return 1
        else:
            print(f"  [ERR]  Failed to save equivalencies to Cosmos")
            return 0
    except Exception as exc:
        print(f"  [ERR]  {exc}")
        return 0


def seed(only: str | None = None, force: bool = False) -> None:
    from azure.cosmos import CosmosClient, PartitionKey
    from azure.cosmos.exceptions import CosmosResourceExistsError

    print(f"\nConnecting to Cosmos: {settings.cosmos_endpoint}")
    client = CosmosClient(
        url=settings.cosmos_endpoint,
        credential=settings.cosmos_key,
        connection_verify=False,
    )
    db = client.create_database_if_not_exists(settings.cosmos_database)

    def _get_or_create(container_id: str, partition_key: str):
        try:
            return db.create_container(
                id=container_id,
                partition_key=PartitionKey(path=partition_key),
            )
        except CosmosResourceExistsError:
            return db.get_container_client(container_id)

    total = 0
    if only in (None, "career_levels"):
        print("\n--- Seeding career level expectations ---")
        container = _get_or_create("career_level_expectations", "/level")
        total += _seed_career_levels(container, force)

    if only in (None, "roster"):
        print("\n--- Seeding bench roster ---")
        container = _get_or_create("bench_roster", "/name")
        total += _seed_roster(container, force)

    if only in (None, "requests"):
        print("\n--- Seeding staffing requests ---")
        container = _get_or_create("staffing_requests", "/role_id")
        total += _seed_requests(container, force)

    if only in (None, "equivalencies"):
        print("\n--- Seeding skill equivalencies ---")
        total += _seed_equivalencies(force)

    if only in (None, "prompts"):
        print("\n--- Seeding agent prompts (force) ---")
        import importlib as _il
        _seed_prompts = _il.import_module("scripts.seed_prompts")
        _seed_prompts.seed(force=True)

    print(f"\nDone. {total} record(s) seeded.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Cosmos DB with sample data")
    parser.add_argument("--force", action="store_true", help="Re-seed existing records")
    parser.add_argument("--only", choices=["career_levels", "roster", "requests", "equivalencies", "prompts"], help="Seed only one dataset")
    args = parser.parse_args()
    seed(only=args.only, force=args.force)
