# =============================================================================
# Azul Pagos Atlas — Dockerfile
# Registry: 293926505005.dkr.ecr.us-east-2.amazonaws.com/pago-azul
#
# Build:  docker build -t pago-azul .
# Run:    docker run -p 8000:8000 --env-file .env.prod pago-azul
#
# Certs mTLS: montar en /app/certs/ vía volumen o secret de ECS/EKS.
#   AZUL_CERT_PATH=/app/certs/iamatlas.crt
#   AZUL_KEY_PATH=/app/certs/iamatlas.key
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — builder: instala dependencias en un venv aislado
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Copiar solo el manifiesto primero (cache layer)
COPY requirements.txt .

# Instalar en venv limpio — no contamina la imagen final
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip --quiet \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime: imagen mínima sin herramientas de build
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Metadatos
LABEL maintainer="iamAtlas <devops@atlas.do>" \
      org.opencontainers.image.title="Azul Pagos Atlas" \
      org.opencontainers.image.version="0.4.0" \
      org.opencontainers.image.description="Sistema de pagos integrado con Azul Payment Gateway"

# Variables de entorno del runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # La app lee DATABASE_URL, AZUL_LOCAL_MODE, etc. del entorno del contenedor.
    # En producción estas vienen de ECS Task Definition o Parameter Store.
    DATABASE_URL="sqlite+aiosqlite:////data/azul_pagos.db" \
    PORT=8000

WORKDIR /app

# Copiar venv del stage builder
COPY --from=builder /opt/venv /opt/venv

# Copiar código fuente
COPY app/       ./app/
COPY routers/   ./routers/
COPY azul_client.py   .
COPY azul_config.py   .

# Directorio para la base de datos SQLite persistente (montar como volumen en ECS)
# En producción usar DATABASE_URL con PostgreSQL
RUN mkdir -p /data /app/certs

# Usuario no-root para el runtime (PCI DSS recomendación)
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home appuser \
    && chown -R appuser:appgroup /app /data

USER appuser

# Puerto expuesto (documentación — ECS mapea el containerPort)
EXPOSE 8000

# Health check — llama al endpoint /health del propio FastAPI
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Entrypoint con uvicorn
# Workers: 1 para SQLite (no soporta concurrencia multi-proceso sin PostgreSQL)
# En producción con PostgreSQL: aumentar --workers a $(nproc) o usar gunicorn
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info", \
     "--access-log"]
