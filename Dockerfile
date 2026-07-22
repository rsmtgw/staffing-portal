# ============================================================
# Staffing Portal — Multi-stage Docker build
# Targets: Azure Container Apps / any OCI-compliant registry
#
# Stage 1 (python-builder) : pip install + clone matching engine
# Stage 2 (runtime)        : lean runtime image
# ============================================================

# ---- Stage 1: Python dependency builder ----------------------------
FROM python:3.11-slim AS python-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./

# Clone the matching engine (sibling repo — no pip package available)
RUN git clone --branch match-maker_002 --depth 1 \
    https://github.com/jaynro/AI-Staffing-Matchmaker.git \
    /ai-staffing-matchmaker

RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Stage 2: runtime image ----------------------------------------
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

# Copy entrypoint script and make it executable
COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Switch to non-root user
USER appuser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
