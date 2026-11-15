# ==========================================
# Stage 1: Builder
# ==========================================
FROM python:3.10-slim AS builder

WORKDIR /build

# Install build dependencies (only what is needed to compile deps like lxml, cryptography etc.)
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       libmemcached-dev \
       zlib1g-dev \
       libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels and requirements first (better layer caching)
COPY wheels/ /wheels/
COPY requirements.txt .

# Install internal wheels first, then remaining dependencies
RUN pip install --user --no-cache-dir /wheels/*.whl \
    && pip install --user --no-cache-dir -r requirements.txt


# ==========================================
# Stage 2: Runtime
# ==========================================
FROM python:3.10-slim AS runtime

LABEL maintainer="Engineering Team <engineering@aviso.com>" \
      version="1.0.0" \
      description="Aviso Core Django Service"

# Create non-root user
RUN adduser --disabled-password --gecos '' appuser

# GIT_* identity: aviso.utils.fileUtils shells out to `git commit` for the tenant
# config backup, which refuses to run without an author/committer. Set as env
# vars rather than `git config --global` so there is no $HOME/.gitconfig to own
# or keep in sync. CONFIG_REPO is under /home/appuser, not the legacy
# /opt/gnana/config, because this container runs as non-root appuser. It is the
# parent directory: the entrypoint clones one repo per stack beneath it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AVISO_APPS=aviso_core \
    PATH="/home/appuser/.local/bin:$PATH" \
    GIT_AUTHOR_NAME="aviso-core" \
    GIT_AUTHOR_EMAIL="gnana@aviso.com" \
    GIT_COMMITTER_NAME="aviso-core" \
    GIT_COMMITTER_EMAIL="gnana@aviso.com" \
    CONFIG_REPO=/home/appuser/config \
    GIT_ASKPASS=/usr/local/bin/git-askpass

WORKDIR /app

# Install runtime-only system libraries.
# git: the tenant config backup in aviso.utils.fileUtils shells out to the git
# binary. python:3.10-slim does not ship it, which is why config backups have
# been failing since this service moved off the legacy python:3.6.5-jessie base
# image. ca-certificates: required to clone the config repo over HTTPS.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       libpq5 \
       libmemcached11 \
       git \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Feed the token to git over stdin rather than embedding it in the remote URL,
# which would write it into .git/config and into any logged remote.
RUN printf '#!/bin/sh\nexec echo "$CONFIG_REPO_TOKEN"\n' > /usr/local/bin/git-askpass \
    && chmod 0755 /usr/local/bin/git-askpass

# Copy installed Python packages from builder
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application code
COPY --chown=appuser:appuser . /app

# COPY --chown sets ownership on the copied files but leaves /app itself owned
# by root, so the SDK package and its version file could not be written.
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/static \
    && mkdir -p "$CONFIG_REPO" \
    && chown -R appuser:appuser /app "$CONFIG_REPO"

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Workers = 2*vCPU + 1 = 33 (gunicorn's standard sync formula for 16 vCPU), timeout 1200s.
# sync workers (process-isolated) preserve legacy thread-safety assumptions.
# --worker-tmp-dir /dev/shm and stderr error logging are Fargate-specific adjustments.
# Access logging is intentionally disabled to avoid CloudWatch volume/cost.
CMD ["gunicorn", "aviso_core.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "33", \
     "--timeout", "1200", \
     "--max-requests", "100", \
     "--max-requests-jitter", "20", \
     "--keep-alive", "0", \
     "--worker-tmp-dir", "/dev/shm", \
     "--error-logfile", "-"]
