# One image for both services: the app (scheduler) and the read-only dashboard.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini config.yaml ./
RUN uv sync --frozen --no-dev

# Run as an ordinary user, not root.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data
USER app

CMD ["papertrader", "run"]
