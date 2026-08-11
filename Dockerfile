# syntax=docker/dockerfile:1
#
# One image, two processes: the HTTP process (Slack webhook + REST API) and
# the worker+sweeper loop. Which one runs is chosen by the container command
# in docker-compose.yml, not by anything baked into this image.

# ---- builder: compile the Python venv, no build toolchain kept afterwards ----
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
# Install the project itself (not just its requirements) into the venv.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# ---- runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# Node.js is a runtime dependency, not a build toolchain: the worker spawns
# the real `claude` CLI (an npm package, @anthropic-ai/claude-code) as a
# subprocess, and that CLI needs a Node runtime to execute. npm itself is
# removed again once the package is installed globally.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y --auto-remove npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN groupadd --system aicom \
    && useradd --system --create-home --home-dir /home/aicom --gid aicom aicom

WORKDIR /app
# Alembic needs its config + migration scripts at runtime for `alembic
# upgrade head`; the application code itself is already installed in the
# venv above.
COPY alembic.ini ./
COPY alembic ./alembic

RUN mkdir -p /app/workspaces /app/artifacts \
    && chown -R aicom:aicom /app /home/aicom

USER aicom

# No credentials are baked in here. The worker container authenticates to
# Claude via the host's logged-in Claude session, mounted in at runtime as a
# volume (see docker-compose.yml and the README's Docker section) -- never
# copied into the image.

CMD ["python", "-m", "aicom.main"]
