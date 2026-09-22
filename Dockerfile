# Multi-stage: build the virtualenv with uv, then copy only the venv and the
# source into a slim runtime image that has no build tools and no uv.
#
# One image runs every service. docker-compose.yml picks the command per
# service (uvicorn for the API and the mock, `gateway worker`, `gateway
# uploader`, `alembic upgrade head`), so there is exactly one thing to build,
# scan and version.

# --- builder -----------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer, so editing source does not
# reinstall the world.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY mock_ads_api ./mock_ads_api
COPY migrations ./migrations
COPY alembic.ini README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime -----------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Design section 9: the container runs as a non-root user. A compromised
# process in this container cannot write outside /app or bind low ports.
RUN groupadd --system --gid 1001 gateway \
    && useradd --system --uid 1001 --gid gateway --home /app --shell /usr/sbin/nologin gateway

WORKDIR /app
COPY --from=builder --chown=gateway:gateway /app/.venv ./.venv
COPY --from=builder --chown=gateway:gateway /app/src ./src
COPY --from=builder --chown=gateway:gateway /app/mock_ads_api ./mock_ads_api
COPY --from=builder --chown=gateway:gateway /app/migrations ./migrations
COPY --from=builder --chown=gateway:gateway /app/alembic.ini ./alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER gateway

# Each service overrides this; the default is the API so `docker run` of
# the bare image does something sensible.
EXPOSE 8080
CMD ["uvicorn", "gateway.api.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080"]
