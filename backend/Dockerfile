# Build context: repository root (see deploy/docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1
WORKDIR /app/backend

COPY backend/pyproject.toml backend/uv.lock ./
# Optional secret "extra_ca": CA bundle for networks with a TLS-inspecting proxy.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY shared/schemas /app/shared/schemas

RUN useradd --system --uid 10001 backend
USER backend
ENV PATH="/opt/venv/bin:$PATH" MM_SCHEMAS_DIR=/app/shared/schemas
CMD ["python", "-m", "app.ingest"]
