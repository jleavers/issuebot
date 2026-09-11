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

# Optional PostgreSQL server binaries, for a target repository whose tests need a real server
# (#62). Empty -- the default -- installs nothing, so the image keeps exactly the contents it
# has without the argument. Debian trixie ships PostgreSQL 17 only, so the version comes from
# PGDG, with the same keyring-and-list shape as the gh stanza above. postgresql-common lands
# first so that create_main_cluster can be turned off before the server package's postinst
# runs: its "main" cluster would be root-owned, and the sessions make their own anyway.
# /opt/postgresql is a stable name for the versioned directory, so the ENV below can put it on
# PATH without expanding POSTGRES_VERSION inside the ${VAR:+...} that keeps it off the default
# image's PATH. The profile.d line is not a duplicate of that ENV: hooks run under `bash -lc`
# and Debian's /etc/profile *overwrites* PATH for a login shell, so without it the hooks that
# drive the cluster could not find initdb however the image's own PATH is set.
# Both put the directory ahead of /usr/bin, where postgresql-client-common's pg_wrapper links
# live: called with no registered cluster -- and create_main_cluster is off, so there is none
# -- every one of them prints "No existing cluster is suitable as a default target" before
# exec'ing the real binary, which is a warning an agent would waste a turn chasing.
# initdb --version is asserted here for the same reason claude --version is below: a renamed
# package or a moved repository has to fail the build, not the first session that tries to
# start a cluster.
ARG POSTGRES_VERSION=""
RUN if [ -n "${POSTGRES_VERSION}" ]; then \
      curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /etc/apt/keyrings/pgdg.asc \
   && chmod go+r /etc/apt/keyrings/pgdg.asc \
   && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "${VERSION_CODENAME}")-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
   && apt-get update \
   && apt-get install -y --no-install-recommends postgresql-common \
   && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf \
   && apt-get install -y --no-install-recommends "postgresql-${POSTGRES_VERSION}" \
   && rm -rf /var/lib/apt/lists/* \
   && ln -s "/usr/lib/postgresql/${POSTGRES_VERSION}" /opt/postgresql \
   && printf 'PATH="/opt/postgresql/bin:$PATH"\n' > /etc/profile.d/issuebot-postgresql.sh \
   && chmod 0644 /etc/profile.d/issuebot-postgresql.sh \
   && /opt/postgresql/bin/initdb --version; \
    fi

RUN useradd --create-home --uid 1000 --shell /bin/bash issuebot \
 && install -d -o issuebot -g issuebot /workspaces /home/issuebot/.claude /app

COPY --from=builder --chown=issuebot:issuebot /app /app

USER issuebot
# initdb, pg_ctl and postgres live in the versioned directory alone -- /usr/bin holds only
# wrappers such as pg_ctlcluster, which need root -- so the hooks that run a per-workspace
# cluster need it on PATH. Added only when the argument was set, so the default image's PATH is
# the one it has always been.
ENV HOME=/home/issuebot \
    PATH="/home/issuebot/.local/bin:/app/.venv/bin:${POSTGRES_VERSION:+/opt/postgresql/bin:}${PATH}"

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
