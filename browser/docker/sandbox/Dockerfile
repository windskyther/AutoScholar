ARG PYTHON_RUNTIME_IMAGE=python:3.12.16-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
FROM ${PYTHON_RUNTIME_IMAGE}
ARG OS_UPDATE_GENERATION=cached

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN test -n "$OS_UPDATE_GENERATION" \
    && python -c "from pathlib import Path; p=Path('/etc/apt/sources.list.d/debian.sources'); p.write_text(p.read_text().replace('http://deb.debian.org','https://deb.debian.org'))" \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY docker/sandbox/requirements.txt /opt/autoscholar/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes \
      --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /opt/autoscholar/requirements.txt
RUN python -m pip uninstall -y pip

COPY docker/sandbox/static_check.py /opt/autoscholar/static_check.py
COPY docker/sandbox/prepare_mnist.py /opt/autoscholar/prepare_mnist.py

RUN useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace/source /datasets/mnist \
    && chown -R 65532:65532 /workspace
RUN find / -xdev -type f -perm /6000 -exec chmod a-s '{}' +
ENV TERM=dumb

WORKDIR /workspace/source
USER 65532:65532
ARG SOURCE_REVISION=working-tree
ARG PYTHON_RUNTIME_IMAGE
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
    org.opencontainers.image.title=AutoScholar-CPU-Sandbox \
    org.opencontainers.image.source=https://github.com/windskyther/AutoScholar \
    io.autoscholar.runtime-base=$PYTHON_RUNTIME_IMAGE

CMD ["python", "-c", "import torch, torchvision; print(torch.__version__, torchvision.__version__)"]
