# syntax=docker/dockerfile:1
# XBOCT: one process (web + Telegram + scheduler + daily backup), one worker, non-root.
# Build:  docker build --build-arg XBOCT_RELEASE=$(git rev-parse --short HEAD) -t xboct:<rev> .
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app
# Dependencies first (cached layer), strictly from uv.lock, without dev/qa groups.
# An optional build secret `build_ca` is only for build hosts behind a TLS-inspecting
# proxy; it is mounted for this step and never stored in the image.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY evals ./evals
COPY src ./src
# The project itself is built by hatchling (fetched here), so the same optional secret.
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev \
 && groupadd --system --gid 10001 xboct \
 && useradd --system --uid 10001 --gid xboct --home-dir /nonexistent --shell /usr/sbin/nologin xboct \
 && mkdir -p /data /backups \
 && chown xboct:xboct /data /backups \
 && chmod 700 /data /backups

ARG XBOCT_RELEASE=dev
LABEL org.opencontainers.image.title="xboct" org.opencontainers.image.revision="${XBOCT_RELEASE}"
ENV XBOCT_RELEASE=${XBOCT_RELEASE} \
    PATH=/app/.venv/bin:${PATH} \
    APP_ENV=production \
    DATABASE_URL=sqlite+aiosqlite:////data/xboct.db \
    BACKUP_DIR=/backups \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

USER xboct
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"]
CMD ["xboct", "serve"]
