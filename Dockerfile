# ============================================================
# Staffing Platform — Multi-stage Docker build
# Targets: Azure Container Apps / any OCI-compliant registry
#
# Stage 1 (ui-builder)      : Vite + React build → dist/
# Stage 2 (python-builder)  : pip install → /install
# Stage 3 (runtime)         : lean runtime image with UI + API
# ============================================================

# ---- Stage 1: React UI build ----------------------------------------
FROM node:20-slim AS ui-builder

WORKDIR /ui-build

# Install dependencies first (layer-cached unless package.json changes)
COPY app/ui/package.json app/ui/package-lock.json* ./
RUN npm ci --prefer-offline

# Copy source and build
COPY app/ui/ ./
RUN npm run build
# Output is in /ui-build/dist/

# ---- Stage 2: Python dependency builder ----------------------------
FROM python:3.11-slim AS python-builder

WORKDIR /build

# Install build tools for packages that compile C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./

# Clone the matching engine (sibling repo — no pip package available)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --branch match-maker_002 --depth 1 \
    https://github.com/jaynro/AI-Staffing-Matchmaker.git \
    /ai-staffing-matchmaker

RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Stage 3: runtime image ----------------------------------------
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=python-builder /install /usr/local

# Copy matching engine source from builder stage
COPY --from=python-builder /ai-staffing-matchmaker /ai-staffing-matchmaker

# Copy application source
COPY --chown=appuser:appuser . .

# Inject compiled React UI — served at /ui by FastAPI StaticFiles
COPY --from=ui-builder /ui-build/dist ./app/ui/dist

# Copy entrypoint script and make it executable
COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Switch to non-root user
USER appuser

# Runtime environment defaults (override at deploy time via env vars / secrets)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Expose FastAPI port
EXPOSE 8000

# Healthcheck (used by Azure Container Apps + docker-compose)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Entry point: run migrations + seeding, then start FastAPI
ENTRYPOINT ["/app/docker-entrypoint.sh"]

