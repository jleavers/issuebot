# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.14-slim

FROM ghcr.io/astral-sh/uv:0.12.11 AS uv

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG CLAUDE_CODE_VERSION=2.1.263
ARG ISSUEBOT_VERSION=0.1.0
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash issuebot \
 && install -d -o issuebot -g issuebot /workspaces /home/issuebot/.claude /app

COPY --from=builder --chown=issuebot:issuebot /app /app

USER issuebot
ENV HOME=/home/issuebot \
    PATH="/home/issuebot/.local/bin:/app/.venv/bin:${PATH}"

# The flag assertion is the point of pinning: a release that drops --permission-prompts
# breaks an unattended worker at runtime, so fail the build instead.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
 && claude --version \
 && claude --help | grep -q -- '--permission-prompts'

WORKDIR /app
# Mount the DIRECTORY holding WORKFLOW.md here, never the file itself: a single-file bind
# mount pins the inode, so an atomic save on the host leaves the container reading the old
# one (#46). compose.yaml mounts ./configs and sets this same value.
ENV ISSUEBOT_WORKFLOW=/configs/WORKFLOW.md
VOLUME ["/workspaces", "/home/issuebot/.claude"]

LABEL org.opencontainers.image.source="https://github.com/jleavers/issuebot" \
      org.opencontainers.image.description="issuebot: issue-to-PR agent orchestrator" \
      org.opencontainers.image.version="${ISSUEBOT_VERSION}"

ENTRYPOINT ["issuebot"]
CMD ["validate"]
