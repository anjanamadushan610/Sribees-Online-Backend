# =============================================================================
# SRIBEESonline — FastAPI Backend Dockerfile
#
# Multi-stage build:
#   - "base"        : shared layer (system deps + Python packages)
#   - "development" : hot-reload for local Docker Compose
#   - "production"  : optimised image for Jenkins → Registry → Deploy
#
# Build targets:
#   docker build --target development -t sribees-api:dev .
#   docker build --target production  -t sribees-api:prod .
#   docker build -t sribees-api .          (defaults to production)
# =============================================================================

# ── Base stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# System dependencies (asyncpg, psycopg2, curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into the base layer (cached across stages)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# ── Development stage ────────────────────────────────────────────────────────
FROM base AS development

# Copy application code (overridden by volume mount in docker-compose)
COPY . .

# Create non-root user. /app/media must exist and be owned by appuser before any
# volume is mounted over it — see the production stage for why.
RUN mkdir -p /app/media \
    && adduser --disabled-password --gecos '' --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser
RUN mkdir -p logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Hot reload for development
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# ── Production stage ─────────────────────────────────────────────────────────
FROM base AS production

ARG BUILD_DATE=unknown
ARG GIT_COMMIT=unknown

LABEL org.opencontainers.image.title="SRIBEESonline API" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.vendor="SRIBEESonline"

# Copy application code
COPY . .

# Create the media directory IN THE IMAGE, before chown.
#
# This is load-bearing. docker-compose mounts the `media_data` volume at
# /app/media. When the mount target does not exist in the image, Docker creates
# it owned by root — and this container runs as non-root `appuser`, so every
# upload then fails with PermissionError (surfacing as a 502 on the upload
# route) while the app itself stays perfectly healthy.
#
# With the directory present and owned by appuser at build time, Docker
# initialises the empty volume from it and carries that ownership across.
RUN mkdir -p /app/media \
    && adduser --disabled-password --gecos '' --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser
RUN mkdir -p logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=10s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production: no reload, multiple workers via gunicorn
CMD ["gunicorn", "app.main:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
