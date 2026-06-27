# WorldMonitor — single application image for BOTH the API and the ingest driver
# (Gate B-4c / ADR 0051). The compose services pick the role via `command`:
#   api    -> uvicorn worldmonitor.api.main:create_app --factory ...
#   driver -> python -m worldmonitor.runner.driver
#
# 12-factor: no secrets are baked in — every value comes from the environment at run time.
# Non-root, slim base, deps installed reproducibly via uv (the project's package manager).
# Multi-stage: the builder carries the C toolchain + ICU dev headers that PyICU (a transitive
# followthemoney dep) compiles against; the runtime stage keeps only the shared libs.

# ----------------------------------------------------------------------------- #
# Builder — resolve + compile dependencies into /app/.venv
# ----------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Build toolchain + ICU headers (PyICU has no wheel; it builds against libicu).
# ca-certificates is installed here and COPYed into the runtime so the runtime needs no apt.
# Acquire::Retries + disabling HTTP pipelining harden the build against flaky mirrors/proxies
# (pipelining is a common source of truncated downloads / "Hash Sum mismatch").
RUN apt-get -o Acquire::Retries=8 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=8 -o Acquire::http::Pipeline-Depth=0 \
        install -y --no-install-recommends \
        build-essential pkg-config libicu-dev python3-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv as a static binary from the official distroless image (pinned to a minor line).
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# 1) Resolve + install dependencies first (cached unless the lock changes).
#    --no-install-project so this layer ignores app source churn; --frozen pins to uv.lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Add the application source and install the package itself into the venv.
COPY src ./src
RUN uv sync --frozen --no-dev

# ----------------------------------------------------------------------------- #
# Runtime — slim image with only the shared libs + the prebuilt venv
# ----------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# No runtime apt (keeps the image slim + the build resilient on flaky mirrors): copy ONLY the
# PyICU runtime shared libs and the CA bundle from the builder. The API/driver healthchecks use
# Python (stdlib urllib / the --healthcheck flag), so no curl is needed in the image.
COPY --from=builder /usr/lib/x86_64-linux-gnu/libicu*.so* /usr/lib/x86_64-linux-gnu/
COPY --from=builder /etc/ssl/certs /etc/ssl/certs

# The venv installs the project editable (src layout), so keep the SAME /app path + copy src.
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Non-root runtime user; pre-create the heartbeat dir the driver writes to.
RUN useradd --create-home --uid 10001 worldmonitor \
    && mkdir -p /var/run/worldmonitor \
    && chown -R worldmonitor:worldmonitor /app /var/run/worldmonitor

USER worldmonitor

EXPOSE 8000

# Default to the API; compose overrides `command` for the driver service.
CMD ["uvicorn", "worldmonitor.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
