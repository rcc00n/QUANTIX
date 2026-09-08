FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY backend/arbiq ./arbiq
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN groupadd --gid 10001 arbiq && useradd --uid 10001 --gid arbiq --create-home arbiq
RUN mkdir -p /var/tmp/quantix-export && chown 10001:10001 /var/tmp/quantix-export
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY scripts/clock_diagnostics.py ./scripts/clock_diagnostics.py
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER arbiq
EXPOSE 8000
CMD ["uvicorn", "arbiq.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM build AS test
RUN uv sync --frozen --no-editable
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY tests /tests
COPY scripts ./scripts
ENV PATH="/app/.venv/bin:$PATH"
CMD ["pytest", "-c", "/app/pyproject.toml", "-q", "--tb=short", "/tests"]
