# syntax=docker/dockerfile:1

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.12.14-slim-trixie AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create and activate a venv
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12.14-slim-trixie AS runtime

WORKDIR /app

# Create a non-root user
RUN addgroup --system teardrop && adduser --system --ingroup teardrop teardrop

# Copy the venv from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# Copy application code
COPY --chown=teardrop:teardrop . .

# Pre-create the writable keys directory for the non-root app user. The keys/
# dir is gitignored (not in the build context), so it must be created here;
# otherwise lifespan's generate_keypair() fails with PermissionError at startup.
RUN mkdir -p /app/keys && chown teardrop:teardrop /app/keys

USER teardrop

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

CMD ["uvicorn", "teardrop.main:app", "--host", "0.0.0.0", "--port", "8000"]
