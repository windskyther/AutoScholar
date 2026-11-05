ARG PYTHON_RUNTIME_IMAGE=python:3.12.16-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
FROM ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 AS uv-tool
FROM ${PYTHON_RUNTIME_IMAGE} AS runtime-base
# Apply current signed distro security fixes over HTTPS, including the build stage.
RUN python -c "from pathlib import Path; p=Path('/etc/apt/sources.list.d/debian.sources'); p.write_text(p.read_text().replace('http://deb.debian.org','https://deb.debian.org'))" \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

FROM runtime-base AS autoscholar-base
COPY --from=uv-tool /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "autoscholar.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

FROM autoscholar-base AS git-mcp
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
CMD ["uvicorn", "autoscholar.tool_platform.git_server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8093", "--no-access-log"]

FROM runtime-base AS production
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=autoscholar-base /app /app
ARG SOURCE_REVISION=working-tree
ARG LOCK_SHA256
ARG PYTHON_RUNTIME_IMAGE
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
    org.opencontainers.image.title=AutoScholar \
    org.opencontainers.image.source=https://github.com/windskyther/AutoScholar \
    io.autoscholar.uv-lock-sha256=$LOCK_SHA256 \
    io.autoscholar.runtime-base=$PYTHON_RUNTIME_IMAGE
RUN groupadd --gid 10001 autoscholar && useradd --uid 10001 --gid 10001 --no-create-home autoscholar \
    && mkdir -p /data/documents /data/workspaces /models \
    && chown 10001:10001 /data/documents /data/workspaces /models
RUN find / -xdev -type f -perm /6000 -exec chmod a-s '{}' +
RUN python -m autoscholar.deployment.manifest --revision "$SOURCE_REVISION" --lock "$LOCK_SHA256"
RUN /usr/local/bin/python -m pip uninstall -y pip
ENV HOME=/tmp/home XDG_CACHE_HOME=/models/cache HF_HOME=/models/huggingface FASTEMBED_CACHE_PATH=/models/fastembed TERM=dumb
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=4s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=3)"
EXPOSE 8000
CMD ["uvicorn", "autoscholar.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]

# Keep the development default target compatible with compose.yaml.
FROM autoscholar-base AS api
