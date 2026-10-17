# ---------------------------------------------------------------------------
# AI Incident & Log Triage Service - Level 2 container image
#
# Design goals (technical_and_architectural_specification.md section 6.2):
#   * multi-stage build: a builder stage that resolves dependencies, and a
#     runtime stage that contains only the virtualenv + application source -
#     no compiler, no pip cache, no build toolchain;
#   * deterministic install: requirements.lock.hashed.txt is the full
#     dependency closure with SHA-256 wheel hashes, installed with
#     --require-hashes, so a compromised or substituted artifact cannot be
#     installed silently;
#   * non-root runtime: the process runs as appuser, UID/GID 10001, with no
#     home directory and a nologin shell;
#   * arm64/amd64 neutrality: the image is built for an explicit platform
#     (--platform linux/amd64) so the identical image runs on Fargate.
#
# Build:  docker build --platform linux/amd64 -t ai-incident-triage:local .
# Run:    docker run --rm -p 8000:8000 --env-file .env.docker ai-incident-triage:local
# ---------------------------------------------------------------------------

# --- Stage 1: build the virtualenv from the hashed lock ---------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv

COPY requirements.lock.hashed.txt /tmp/requirements.lock.hashed.txt

# --no-deps: the lock file is a complete closure, so pip must not resolve
#            anything that is not hashed in it.
# --require-hashes: every artifact must match a recorded SHA-256.
RUN /opt/venv/bin/pip install \
        --require-hashes \
        --no-deps \
        -r /tmp/requirements.lock.hashed.txt

# --- Stage 2: minimal runtime ----------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# appuser: 10001:10001, no home, no shell. Never root at runtime.
RUN groupadd --gid 10001 appuser \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app /app/app

USER 10001:10001

EXPOSE 8000

# Container-level liveness probe (ECS/ALB also health-check /health; this one
# makes `docker run` and any orchestrator that reads HEALTHCHECK behave).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# --no-access-log: access logging is emitted as structured JSON by the
# correlation-id middleware in app/main.py, so uvicorn's duplicate plain-text
# access log is switched off (same as the Level 1 systemd unit).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
