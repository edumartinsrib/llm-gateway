# Build a partir da raiz do repositório:  docker build -f gateway-api/Dockerfile .
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
# O venv é criado no mesmo caminho da imagem final (scripts têm shebang absoluto).
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY gateway-api/pyproject.toml gateway-api/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package gateway-api
COPY gateway-api gateway-api
RUN uv sync --frozen --no-dev --no-editable --package gateway-api

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home llmgw
COPY --from=build /app/.venv /app/.venv
COPY gateway-api/alembic.ini /app/alembic.ini
COPY gateway-api/migrations /app/migrations
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 LLMGW_PORT=8000
WORKDIR /app
USER llmgw
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
# Migrações Alembic antes de subir (idempotente; várias réplicas usam lock do Postgres).
CMD ["sh", "-c", "alembic upgrade head && exec llm-gateway"]
