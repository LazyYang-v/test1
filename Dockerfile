FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir --disable-pip-version-check uv==0.12.3
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/harness/pyproject.toml packages/harness/pyproject.toml
COPY packages/runtime/pyproject.toml packages/runtime/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-project --no-install-workspace

COPY apps/ apps/
COPY packages/ packages/
COPY migrations/ migrations/
RUN uv sync --frozen --no-dev --no-install-project

USER 10001:10001
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "agentforge_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
