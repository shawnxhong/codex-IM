# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f deploy/docker/Dockerfile --target <bridge|agent|test> \
#   --build-arg CODEX_VERSION=<x.y.z> .
# One CODEX_VERSION pins both the SDK (bridge) and the Codex binary (agent) — design D5/D12.
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ARG CODEX_VERSION
RUN test -n "${CODEX_VERSION}" || (echo "build with --build-arg CODEX_VERSION=<x.y.z>" >&2; exit 1)
RUN apt-get update \
 && apt-get install -y --no-install-recommends socat tini git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Optional corporate root CAs: put *.crt files into deploy/docker/ca/ before building.
COPY deploy/docker/ca/ /usr/local/share/ca-certificates/codex-im/
RUN update-ca-certificates
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
WORKDIR /opt/codex-im
COPY pyproject.toml README.md ./
COPY src ./src

# --------------------------------------------------------------------------- bridge
FROM base AS bridge
ARG CODEX_VERSION
RUN pip install "openai-codex==${CODEX_VERSION}" . \
 && useradd --uid 10002 --create-home --shell /usr/sbin/nologin bridge \
 && mkdir -p /data \
 && chown bridge:bridge /data
USER bridge
ENV CODEX_IM_DATA_DIR=/data
ENTRYPOINT ["tini", "--", "python", "-m", "codex_im"]

# --------------------------------------------------------------------------- agent
FROM base AS agent
ARG CODEX_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends ripgrep curl procps less \
 && rm -rf /var/lib/apt/lists/*
# codex-im itself is installed without dependencies: the agent only runs agentd + agent_config (stdlib).
RUN pip install "openai-codex==${CODEX_VERSION}" \
 && pip install --no-deps . \
 && ln -s "$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" /usr/local/bin/codex \
 && useradd --uid 10001 --create-home --shell /bin/bash agent \
 && mkdir -p /workspace /home/agent/.codex \
 && chown agent:agent /workspace /home/agent/.codex
COPY deploy/docker/agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
COPY deploy/docker/codex-app-server.sh /usr/local/bin/codex-app-server
RUN chmod 0755 /usr/local/bin/agent-entrypoint.sh /usr/local/bin/codex-app-server
USER agent
ENV CODEX_HOME=/home/agent/.codex \
    CODEX_IM_WORKSPACE_ROOT=/workspace
WORKDIR /workspace
ENTRYPOINT ["tini", "--", "/usr/local/bin/agent-entrypoint.sh"]

# --------------------------------------------------------------------------- test (Ubuntu: make test)
FROM base AS test
ARG CODEX_VERSION
RUN pip install "openai-codex==${CODEX_VERSION}" ".[dev]"
COPY tests ./tests
RUN git config --global user.email test@codex-im && git config --global user.name codex-im-test
CMD ["python", "-m", "pytest", "-q"]
