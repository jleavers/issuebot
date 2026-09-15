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
 && apt-get install -y --no-install-recommends ca-certificates curl git sudo \
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
# runs: its "main" cluster would be root-owned, and the sessions make their own anyway. The
# drop-in rather than createcluster.conf itself, which is a dpkg conffile carrying defaults
# worth keeping; its last line already includes the directory. Both halves are asserted, since
# the postinst only *skips* the cluster and a setting it stopped reading would say nothing.
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
   && mkdir -p /etc/postgresql-common/createcluster.d \
   && echo "create_main_cluster = false" \
        > /etc/postgresql-common/createcluster.d/99-issuebot.conf \
   && test "$(pg_conftool /etc/postgresql-common/createcluster.conf show -bs create_main_cluster)" = off \
   && apt-get install -y --no-install-recommends "postgresql-${POSTGRES_VERSION}" \
   && test ! -e "/var/lib/postgresql/${POSTGRES_VERSION}/main" \
   && rm -rf /var/lib/apt/lists/* \
   && ln -s "/usr/lib/postgresql/${POSTGRES_VERSION}" /opt/postgresql \
   && printf 'PATH="/opt/postgresql/bin:$PATH"\n' > /etc/profile.d/issuebot-postgresql.sh \
   && chmod 0644 /etc/profile.d/issuebot-postgresql.sh \
   && /opt/postgresql/bin/initdb --version; \
    fi

# Optional Node runtime, for a target repository whose tests execute its own client-side
# JavaScript (#64). Empty -- the default -- installs nothing, so the image keeps exactly the
# contents it has without the argument. The official build from nodejs.org rather than an apt
# repository: this needs one major, not a distribution's idea of one, and the tarball carries
# npm with it. NODE_VERSION is a major, so the build resolves it to whatever patch nodejs.org
# holds -- the same shape of knob as POSTGRES_VERSION, and the same reason: an operator pins
# the line their target repository's CI runs on, not a patch they would then have to chase.
# .tar.gz rather than .tar.xz, so nothing here depends on whether the slim image carries
# xz-utils. The checksum comes from the same SHASUMS256.txt the filename is read out of, and
# --ignore-missing checks the one file that was downloaded against it.
# /opt/node is a stable name for the versioned directory, exactly as /opt/postgresql is, so the
# ENV below can put it on PATH without expanding NODE_VERSION inside the ${VAR:+...} that keeps
# it off the default image's PATH; and the profile.d line is there for the same reason as the
# PostgreSQL one, since hooks run under `bash -lc` and Debian's /etc/profile overwrites PATH
# for a login shell.
# node --version and npm --version are asserted here for the reason initdb --version and
# claude --version are: a moved download or a renamed archive has to fail the build, not the
# first session that runs `npm ci`.
# --no-same-owner because nodejs.org's tarballs store uid 1001 (their `iojs` build user), which
# tar honours when it runs as root: without it the whole runtime would belong to a uid that is
# in no /etc/passwd entry here, and a release built under 1000 would hand the agent -- which
# runs unattended, model-authored code -- write access to its own interpreter. root-owned and
# 0755, as the apt half already is.
# The pin moves by hand. A tarball fetched by URL is invisible to Dependabot, the same way
# MIN_CLAUDE_VERSION is; nodejs.org's own release schedule is the thing to watch. A major on
# its own is the only accepted value -- latest-v24.21.0.x does not exist, and curl would fail
# the build with nothing but exit 22 to say why.
ARG NODE_VERSION=""
RUN if [ -n "${NODE_VERSION}" ]; then \
      case "${NODE_VERSION}" in \
        *[!0-9]*) echo "NODE_VERSION is a major on its own, e.g. 24, not ${NODE_VERSION}" >&2; exit 1 ;; \
      esac \
   && arch="$(dpkg --print-architecture)" \
   && case "${arch}" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "no nodejs.org build for ${arch}" >&2; exit 1 ;; \
      esac \
   && dist="https://nodejs.org/dist/latest-v${NODE_VERSION}.x" \
   && cd /tmp \
   && curl -fsSL "${dist}/SHASUMS256.txt" -o SHASUMS256.txt \
   && tarball="$(grep -E "node-v[0-9.]+-linux-${node_arch}\.tar\.gz$" SHASUMS256.txt | awk '{print $2}')" \
   && test -n "${tarball}" \
   && curl -fsSLO "${dist}/${tarball}" \
   && sha256sum -c --ignore-missing SHASUMS256.txt \
   && tar --no-same-owner -xzf "${tarball}" -C /opt \
   && rm -f "${tarball}" SHASUMS256.txt \
   && ln -s "/opt/$(basename "${tarball}" .tar.gz)" /opt/node \
   && printf 'PATH="/opt/node/bin:$PATH"\n' > /etc/profile.d/issuebot-node.sh \
   && chmod 0644 /etc/profile.d/issuebot-node.sh \
   && /opt/node/bin/node --version \
   && PATH="/opt/node/bin:${PATH}" /opt/node/bin/npm --version; \
    fi

# Two accounts, one privilege each (#75). `issuebot` (uid 1000) is the worker: it holds
# GH_TOKEN, the database URL and the Slack webhook, parses what the session writes and decides
# every label move. `agent` (uid 1001) is the session: `claude -p`, every hook, the clone and
# the post-clone setup run as it, and the login it authenticates with lives in its own home
# (compose mounts `claude-home` at /home/agent/.claude). Nothing the session can read or
# write at its own uid is an input to the worker: /app is root's and writable by neither,
# /home/issuebot and /home/agent are closed to the other account, /proc/<worker>/environ is
# unreadable across the uid line, and the worker's state inside a workspace sits in sticky
# directories it owns. The worker stays unprivileged: sudo carries exactly one rule, issuebot
# may become agent and nobody else, and the binary is executable by root and group issuebot
# alone, so the session's uid cannot invoke sudo at all -- not even to be refused by it.
# `closefrom_override` is for the one descriptor the worker passes across the uid change, the
# session's environment (issuebot.agent.runas); `!use_pty` keeps a turn's stream-json byte for
# byte when `docker compose run` gives the worker a terminal.
# The split needs one thing of git. A workspace directory is the worker's and sticky, so the
# session cannot unlink the state kept there, while the clone inside it is the session's own --
# and git refuses to work in a repository whose worktree belongs to another account (`detected
# dubious ownership`, which `git config --local` reports downstream as the baffling "--local
# can only be used inside a git repository"). Without the exception below every git command a
# session runs fails, the post-clone setup first. It is scoped to the workspace root rather
# than a bare `*`: it says that under /workspaces a repository owned by another account is
# still ours, and the only other account that can own anything there is the worker, which is
# the more privileged side of the line. The path is the image's own -- the `install -d` below,
# the VOLUME further down and compose's mount -- so a `workspace.root` pointed elsewhere inside
# the container would need its own entry.
# A third account, `web` (uid 1002), for the dashboard (#102). compose builds the `web` service
# from this image and selects it with `user: web`; nothing in the image runs as it by default,
# since `USER issuebot` below is the worker and `validate`. The dashboard takes HTTP from a
# browser and needs no privilege transition at all, so it must not carry the worker's: outside
# group issuebot it cannot execute sudo, let alone use the rule, and it owns nothing either
# account writes -- its home is closed to both of them and theirs to it, and /app is root's.
# `nologin` because no shell is ever opened as it: `issuebot web` is the one process, and a
# `docker compose exec web` still runs whatever command it names.
RUN useradd --create-home --uid 1000 --shell /bin/bash issuebot \
 && useradd --create-home --uid 1001 --shell /bin/bash agent \
 && useradd --create-home --uid 1002 --shell /usr/sbin/nologin web \
 && chmod 0750 /home/issuebot /home/agent /home/web \
 && install -d -m 0755 -o issuebot -g issuebot /workspaces \
 && git config --system --add safe.directory '/workspaces/*' \
 && install -d -m 0700 -o agent -g agent /home/agent/.claude \
 && printf '%s\n' \
      'Defaults:issuebot !use_pty, !syslog, !lecture, closefrom_override' \
      'issuebot ALL=(agent) NOPASSWD: ALL' \
      > /etc/sudoers.d/issuebot \
 && chmod 0440 /etc/sudoers.d/issuebot \
 && visudo -cf /etc/sudoers.d/issuebot \
 && chgrp issuebot /usr/bin/sudo \
 && chmod 4750 /usr/bin/sudo

# The worker's code, venv and interpreter: root's, readable by both accounts and writable by
# neither (the bytecode is compiled in the builder, so nothing needs to write here at runtime).
COPY --from=builder /app /app

# claude installed once, root-owned under /opt/claude, on PATH for both accounts through
# /usr/local/bin. The installer puts everything under $HOME, so it is given one; a plain root
# with no SUDO_USER is the case it allows. The install runs as `issuebot` below and as
# `agent` through the delegation, so a binary either account could not run fails the build.
ARG CLAUDE_INSTALL_HOME=/opt/claude
RUN mkdir -p "${CLAUDE_INSTALL_HOME}" \
 && curl -fsSL https://claude.ai/install.sh | HOME="${CLAUDE_INSTALL_HOME}" bash -s "${CLAUDE_CODE_VERSION}" \
 && ln -s "${CLAUDE_INSTALL_HOME}/.local/bin/claude" /usr/local/bin/claude \
 && rm -rf "${CLAUDE_INSTALL_HOME}/.claude" "${CLAUDE_INSTALL_HOME}/.claude.json" \
 && chmod -R a+rX "${CLAUDE_INSTALL_HOME}"

USER issuebot
# initdb, pg_ctl and postgres live in the versioned directory alone -- /usr/bin holds only
# wrappers such as pg_ctlcluster, which need root -- so the hooks that run a per-workspace
# cluster need it on PATH; node and npm live in the unpacked tarball and are on no PATH at all
# without this. Each is added only when its own argument was set, so the default image's PATH is
# the one it has always been.
# LANG is not part of that: the base image sets no locale at all -- the official python images
# used to set one and no longer do -- which leaves every process in the container on C. A bare
# `initdb` takes the cluster's encoding from the locale, so on C it builds a SQL_ASCII database,
# which hands psycopg bytes where a suite expects str and errors out every postgres-marked test
# in teardown (#66). Set for every build rather than inside the ${POSTGRES_VERSION:+...} above,
# because the locale is not the server's business: git, psql, sort and the agent's own shell all
# read it. C.UTF-8 (which `locale -a` spells C.utf8) is built into glibc on trixie, so there is
# nothing to install for it. LANG and not LC_ALL: LC_ALL overrides every category, which would
# stop a target repository's own LC_* settings from taking effect.
# No HOME here: Docker sets it from /etc/passwd for whichever account runs, so `--user agent`
# (the login recipe in the README) gets /home/agent and the worker /home/issuebot.
# ISSUEBOT_AGENT_USER is `agent.run_as`'s fallback (resolve.py): the session runs as `agent`
# in every container built from this image unless a WORKFLOW.md says otherwise.
ENV LANG=C.UTF-8 \
    ISSUEBOT_AGENT_USER=agent \
    PATH="/app/.venv/bin:${POSTGRES_VERSION:+/opt/postgresql/bin:}${NODE_VERSION:+/opt/node/bin:}${PATH}"

# The flag assertions are the point of pinning: a release that drops --permission-prompts
# or --strict-mcp-config breaks an unattended worker at runtime -- the first by prompting
# where nobody can answer, the second by loading whatever MCP config the session account's
# home holds (#119) -- and one that drops --disallowedTools silently widens the session's
# tool set (#109), so fail the build instead. --permission-prompts and --strict-mcp-config
# are passed on every turn and neither is a setting, which is what puts them here rather
# than in `validate`; --disallowedTools carries the setting that fixes the tool set. The
# last line is the delegation itself, as the worker will use it: sudo, the account, and
# claude under it.
RUN claude --version \
 && claude --help | grep -q -- '--permission-prompts' \
 && claude --help | grep -q -- '--disallowedTools' \
 && claude --help | grep -q -- '--strict-mcp-config' \
 && test "$(sudo -n -u agent id -u)" = 1001 \
 && sudo -n -H -u agent claude --version

WORKDIR /app
# Mount the DIRECTORY holding WORKFLOW.md here, never the file itself: a single-file bind
# mount pins the inode, so an atomic save on the host leaves the container reading the old
# one (#46). compose.yaml mounts ./configs and sets this same value.
ENV ISSUEBOT_WORKFLOW=/configs/WORKFLOW.md
VOLUME ["/workspaces", "/home/agent/.claude"]

LABEL org.opencontainers.image.source="https://github.com/jleavers/issuebot" \
      org.opencontainers.image.description="issuebot: issue-to-PR agent orchestrator" \
      org.opencontainers.image.version="${ISSUEBOT_VERSION}"

ENTRYPOINT ["issuebot"]
CMD ["validate"]
