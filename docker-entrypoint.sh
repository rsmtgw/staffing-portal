#!/bin/bash
set -e

echo "=== Staffing Platform Docker Entrypoint ==="

# Wait for Cosmos DB to be ready (if COSMOS_ENDPOINT is set)
if [ -n "$COSMOS_ENDPOINT" ]; then
    echo "Waiting for Cosmos DB to be available..."
    # Give it a few seconds for the emulator/cloud to be ready
    sleep 2
fi

# Run database migrations
echo "Running database migrations..."
python scripts/migrate.py || {
    echo "Migration failed, but continuing..."
}

# Seed the database
echo "Seeding database..."
python scripts/seed.py || {
    echo "Seeding failed, but continuing..."
}

echo "=== Starting FastAPI server ==="
# Start the FastAPI server
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
