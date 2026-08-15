# syntax=docker/dockerfile:1.7
# UX-Proto — agent-piloted visual prototyping. FastAPI backend (+ per-project
# subprocess mock backends) + Vite/React dashboard frontend, ported as-is
# from the monolith custom app (src/custom_apps/ux-proto) into a self-
# contained container-tier aw-app image.
#
# Unlike the monolith's dev Dockerfile (which bind-mounts src/ from the host
# via aw.json volumes), this image BAKES the application source in — a
# distributed marketplace image can't rely on a host bind mount. Runtime
# behavior is otherwise unchanged: the same entrypoint.sh runs uvicorn +
# `vite --host` inside the container.
#
# Published by .github/workflows/build.yml as ghcr.io/tekflox/aw-app-ux-proto:latest.
FROM python:3.13-slim

# System deps — git for any pip-from-git, curl for healthchecks, nodejs for Vite.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Backend deps.
COPY src/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Dedicated, isolated Chromium for renderer.py's faithful-screenshot path —
# deliberately NOT the shared aw-sandbox aw-browser/Playwright-MCP session
# (that one's shared across the whole workspace for many other things;
# this one only ever renders a captured DOM snapshot for this app, lazy-
# launched and idle-reaped, see renderer.py).
#
# `playwright install --with-deps` maps distro -> a hardcoded package list
# and doesn't recognize Debian trixie (python:3.13-slim's base), falling
# back to an ubuntu20.04 list with packages trixie doesn't have
# (ttf-ubuntu-font-family, ttf-unifont). Installing `chromium` itself from
# apt pulls in the correct runtime shared libs for whatever this image's
# actual suite is — Debian resolves that correctly regardless of exact
# package-name churn between releases — then playwright installs its OWN
# separate Chromium binary (we never invoke apt's chromium; only its deps).
# fonts-noto-color-emoji: without it, real Chromium has no color-emoji
# glyphs at all and renders every emoji (🏷 ➕ 👟, etc.) as an empty "tofu"
# box — html2canvas didn't have this gap because it ran inside the user's
# own real browser, which already has emoji fonts installed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && playwright install chromium

# Frontend deps.
COPY src/app/package.json src/app/package-lock.json* /tmp/frontend/
RUN cd /tmp/frontend && npm install --no-audit --no-fund && \
    mkdir -p /app/src/app && mv node_modules /app/src/app/node_modules

# Bake the application source into the image (self-contained — a
# distributed image can't rely on a host bind mount like the monolith's
# aw.json volumes do). node_modules/__pycache__/data are excluded from the
# build context (see .dockerignore) so this never clobbers what was just
# installed above.
COPY src/api /app/src/api
COPY src/app /app/src/app

EXPOSE 10021

# Runs both backend and frontend in parallel — unchanged from the ported
# entrypoint. Frontend serves on the published port; backend on
# 127.0.0.1:<PORT+10000> inside the container, proxied by Vite (see
# vite.config.js).
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
