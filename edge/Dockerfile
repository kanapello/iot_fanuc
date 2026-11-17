# Build context: repository root (see deploy/docker-compose*.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1
WORKDIR /app

COPY edge/pyproject.toml edge/uv.lock ./
# Optional secret "extra_ca": CA bundle for networks with a TLS-inspecting proxy.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project

COPY edge/collector ./collector
COPY edge/scenarios ./scenarios
COPY edge/config.sim.a.yaml edge/config.sim.b.yaml ./

RUN useradd --system --uid 10001 collector && mkdir -p /app/data && chown collector /app/data
USER collector
ENV PATH="/opt/venv/bin:$PATH"
CMD ["python", "-m", "collector.main", "--config", "/app/config.yaml"]
