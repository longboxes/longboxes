# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# System deps:
#  - unar: GPL RAR extraction for CBR files (used from Phase 2 onward).
#    Both the stdlib CBR reader and comicbox call through ``rarfile``,
#    which honors the ``UNRAR_TOOL = "unar"`` shim set in ``cbr.py`` +
#    ``comicbox_reader.py``. Comicbox's docs ask for ``unrar`` on
#    the PATH but the shim makes it use ``unar`` instead — keeps the
#    image GPL-clean.
#  - build-essential / curl: useful for packages with C extensions and debugging.
RUN apt-get update && apt-get install -y --no-install-recommends \
        unar \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv from the official image
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

# Important: the venv must live OUTSIDE /app, because docker-compose
# bind-mounts the host project directory over /app at runtime and would
# otherwise wipe it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies first (cached unless pyproject.toml / uv.lock change).
# --no-install-project so we only install deps, not the longboxes package itself;
# at runtime, Python imports `app` from CWD (/app), which the bind mount populates.
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-install-project

# Copy app source for the image (used when running without a bind mount).
COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
