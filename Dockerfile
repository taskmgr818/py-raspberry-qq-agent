FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY third_party/ ./third_party/
RUN uv sync --frozen --no-dev --no-editable

ENTRYPOINT ["py-raspberry-qq-agent", "--config", "/app/config.yaml"]
CMD ["run"]
