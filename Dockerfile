# An example using multi-stage image builds to create a final image without uv.

# First, build the application in the `/app` directory.
# See `Dockerfile` for details.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Disable Python downloads, because we want to use the system interpreter
# across both images. If using a managed Python version, it needs to be
# copied from the build image into the final image; see `standalone.Dockerfile`
# for an example.
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

COPY --from=builder --chown=app:app /app /app
ADD templates/ /templates

ENV PATH="/app/.venv/bin:$PATH"

CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000", "/app/src/main.py"]