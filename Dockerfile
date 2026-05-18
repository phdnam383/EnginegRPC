# ══════════════════════════════════════════════════════════════════════════════
# Alarm Cluster Engine — gRPC Server
# ══════════════════════════════════════════════════════════════════════════════
#
# Build:
#   docker build -t alarm-cluster-engine:latest .
#
# Run locally:
#   docker run -p 50051:50051 -p 8080:8080 alarm-cluster-engine:latest
#
# The model (models/embeddings.npz, ~215 KB) is baked into the image.
# For production, mount a PVC at /app/models so the model can be updated
# without rebuilding the image (see k8s/pvc.yaml).
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.12-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies (cached layer) ────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application source ───────────────────────────────────────────────────
COPY proto/  proto/
COPY *.py    ./

# ── Bake the pre-trained embedding model into the image ───────────────────────
# Only embeddings.npz is needed at serving time (~215 KB).
COPY models/embeddings.npz models/embeddings.npz

# ── Runtime config ────────────────────────────────────────────────────────────
ENV GRPC_PORT=50051 \
    HEALTH_PORT=8080 \
    MODEL_DIR=/app/models \
    MAX_WORKERS=10

# gRPC port + HTTP health probe port
EXPOSE 50051 8080

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["python", "server.py"]
