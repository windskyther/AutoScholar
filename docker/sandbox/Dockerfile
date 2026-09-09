FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch==2.8.0 torchvision==0.23.0 \
    && python -m pip install --no-cache-dir pytest==8.4.2 ruff==0.12.12

COPY docker/sandbox/static_check.py /opt/autoscholar/static_check.py
COPY docker/sandbox/prepare_mnist.py /opt/autoscholar/prepare_mnist.py
COPY docker/sandbox/launch.py /opt/autoscholar/launch.py

RUN useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace/source /datasets/mnist \
    && chown -R 65532:65532 /workspace

WORKDIR /workspace/source
USER 65532:65532

CMD ["python", "-c", "import torch, torchvision; print(torch.__version__, torchvision.__version__)"]
