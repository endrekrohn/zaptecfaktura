# Multi-stage build: install dependencies with uv, then copy the app and its
# virtualenv into a slim image without uv.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Use the system interpreter in both images.
ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
ADD src/ /app/src
ADD pyproject.toml /app
ADD README.md /app
ADD uv.lock /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm

RUN useradd --system --create-home app && mkdir -p /data && chown app:app /data
COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app templates/ /app/templates
COPY --chown=app:app static/ /app/static

ENV PATH="/app/.venv/bin:$PATH" \
    DATABASE_PATH=/data/zaptecfaktura.db

USER app
VOLUME ["/data"]
EXPOSE 8000

CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000", "/app/src/main.py"]
