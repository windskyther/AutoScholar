FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS autoscholar-base

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

FROM autoscholar-base AS production
ARG SOURCE_REVISION=working-tree
ARG LOCK_SHA256
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
    org.opencontainers.image.title=AutoScholar \
    org.opencontainers.image.source=https://github.com/windskyther/AutoScholar \
    io.autoscholar.uv-lock-sha256=$LOCK_SHA256
RUN groupadd --gid 10001 autoscholar && useradd --uid 10001 --gid 10001 --no-create-home autoscholar \
    && mkdir -p /data/documents /data/workspaces /models \
    && chown 10001:10001 /data/documents /data/workspaces /models
RUN python -m autoscholar.deployment.manifest --revision "$SOURCE_REVISION" --lock "$LOCK_SHA256"
ENV HOME=/tmp/home XDG_CACHE_HOME=/models/cache HF_HOME=/models/huggingface FASTEMBED_CACHE_PATH=/models/fastembed
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=4s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=3)"

# Keep the development default target compatible with compose.yaml.
FROM autoscholar-base AS api
