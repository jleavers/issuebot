# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.14-slim

FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

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

# Optional Python toolchain, for a target repository whose tests are run with `uv` (#128's
# blocker). Empty -- the default -- installs nothing, so the image keeps exactly the contents
# it has without the argument. The third of these, and deliberately the same shape as the two
# above: an operator whose target repository is a Python project sets ISSUEBOT_UV_VERSION and
# rebuilds the worker, and one whose target repository is not carries none of it.
# The official build from the project's own releases rather than the `uv` image used by the
# builder stage: a COPY --from would put the binaries in every image and then remove them
# again, which leaves the layer behind and costs the default image ~35 MB for a toolchain it
# was told not to install. UV_VERSION is an exact release (`0.12.11`), not a major: uv is
# pre-1.0 and its minors are not interchangeable, so an operator pins the version their target
# repository's own CI runs rather than whatever is newest at build time.
# The pin moves by hand, for the reason NODE_VERSION's does: a tarball fetched by URL is
# invisible to Dependabot. The builder stage's `ghcr.io/astral-sh/uv` pin is Dependabot's and
# is a different thing -- that one builds issuebot, this one runs the target repository's
# suite, and they are entitled to differ.
# The checksum comes from the release's own .sha256 beside the tarball, so a download that is
# not what astral published fails the build rather than being installed.
# --no-same-owner for the reason the node tarball has it: tar honours stored ownership when it
# runs as root, and the agent runs unattended, model-authored code.
# uv --version is asserted here for the reason initdb --version, node --version and
# claude --version are: a moved download or a renamed asset has to fail the build, not the
# first session that runs `uv sync`.
# The profile script is the `PATH` line and nothing else. It used to carry a
# `UV_LINK_MODE=copy` default as well (#161), because on a deployment shaped like this one the
# hardlink uv would rather use could never work: uv's cache defaulted to $HOME/.cache/uv and a
# session account's home is in the container's own writable layer, while the venv it builds is
# `<workspace>/.venv` on the mounted volume. Two filesystems, and a hardlink cannot cross them,
# so every `uv sync` fell back to a full copy and warned three lines about it on the stderr of
# `after_create` -- the first hook of every session, logged in full on `hook_finished` and
# quoted into the run's error (`HookResult.summary`) when that hook fails.
# #164 removed the reason rather than the warning: the worker now puts uv's cache on the
# workspaces volume beside the venvs, one directory per session account, and hands it to every
# hook and every turn as `UV_CACHE_DIR` (`agent/uvcache.py`). One filesystem, so uv's own
# default link mode -- hardlink -- is both what it wants and what works, and a `copy` default
# here would now be the thing standing in its way: measured on the live worker, two workspaces'
# venvs came to 152 MB copied and 77 MB hardlinked from one cache, and the cache itself now
# outlives `docker compose up -d worker`.
# What is left for the login shell is `PATH`, and it needs this file *in addition to* the
# runtime `ENV`, not instead of it: Debian's /etc/profile overwrites `PATH` for a login shell,
# which is what every hook runs under, while the `ENV` is what the worker's own process carries
# -- and that is precisely what #164's second gate reads, since `ensure_uv_cache_dir` asks
# `which` about the `PATH` the session will be handed. The cache directory itself takes neither
# route but the environment issuebot builds, since it is per account and derived from
# `workspace.root`, so no static file could state it; it is `agent_environment`'s one computed
# entry, beside `GH_TOKEN`.
# A deployment that wants something else still has both routes #161 documented, and they are
# unchanged: `uv sync --link-mode=copy` in the hook line itself, or `UV_LINK_MODE=copy` in an
# `.issuebot/env` written from `before_run`, which covers the later hooks and every turn.
# `UV_LINK_MODE` is in neither `PASSTHROUGH_NAMES` nor `PROTECTED_ENV_NAMES`, and nor is
# `UV_CACHE_DIR`, so both are the deployment's to override from there. (The builder stage sets
# `UV_LINK_MODE=copy` for itself, above: that one is the buildkit cache mount, a different
# filesystem again, and a different image.)
ARG UV_VERSION=""
RUN if [ -n "${UV_VERSION}" ]; then \
      case "${UV_VERSION}" in \
        ''|*[!0-9.]*|.*|*.) echo "UV_VERSION is a release version, e.g. 0.12.11, not ${UV_VERSION}" >&2; exit 1 ;; \
      esac \
   && arch="$(dpkg --print-architecture)" \
   && case "${arch}" in \
        amd64) uv_arch=x86_64-unknown-linux-gnu ;; \
        arm64) uv_arch=aarch64-unknown-linux-gnu ;; \
        *) echo "no astral-sh/uv build for ${arch}" >&2; exit 1 ;; \
      esac \
   && dist="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}" \
   && cd /tmp \
   && curl -fsSLO "${dist}/uv-${uv_arch}.tar.gz" \
   && curl -fsSL "${dist}/uv-${uv_arch}.tar.gz.sha256" -o uv.sha256 \
   && sha256sum -c uv.sha256 \
   && tar --no-same-owner -xzf "uv-${uv_arch}.tar.gz" -C /tmp \
   && install -d -m 0755 /opt/uv/bin \
   && install -m 0755 "/tmp/uv-${uv_arch}/uv" "/tmp/uv-${uv_arch}/uvx" /opt/uv/bin/ \
   && rm -rf "/tmp/uv-${uv_arch}" "/tmp/uv-${uv_arch}.tar.gz" /tmp/uv.sha256 \
   && printf 'PATH="/opt/uv/bin:$PATH"\n' > /etc/profile.d/issuebot-uv.sh \
   && chmod 0644 /etc/profile.d/issuebot-uv.sh \
   && /opt/uv/bin/uv --version; \
    fi

# Optional PowerShell toolchain, for a target repository whose scripts and tests are
# PowerShell. Empty -- the default -- installs nothing, so the image keeps exactly the
# contents it has without the argument. The fourth of these, and deliberately the same shape
# as the three above: an operator whose target repository is a PowerShell project sets
# ISSUEBOT_PWSH_VERSION and rebuilds the worker, and one whose target repository is not
# carries none of it. It is the largest of the four by some way -- ~180 MB of .NET runtime
# and ~40 MB of ICU -- which is what makes staying behind the guard matter here rather than
# being a nicety.
# PWSH_VERSION is an exact release (`7.6.6`), not a major, for uv's reason and a harder one:
# GitHub publishes releases under their tags and there is no `latest-v7.x` to resolve, so a
# major on its own names nothing to download. The pin moves by hand, like NODE_VERSION's and
# UV_VERSION's: a tarball fetched by URL is invisible to Dependabot, and the release line to
# watch is the one the target repository's own CI runs.
# ICU first, because .NET reads its globalization data from it and the base image carries
# none: without it `pwsh` runs in invariant mode, where `"{0:N2}"` stops grouping and a suite
# that formats numbers or compares strings by culture quietly returns different answers than
# it does on the developer's machine. Resolved by name rather than pinned, since the package
# is named after the ABI (`libicu76` on trixie) and a PYTHON_IMAGE bump to the next Debian
# renames it -- a build that cannot find one fails here rather than at the first session.
# The checksum comes from the release's own `hashes.sha256`, which covers every asset of the
# release, so --ignore-missing as the node stanza has it. Microsoft writes that file as
# UTF-16 and `sha256sum -c` reads bytes, so it is transcoded first -- and in two steps rather
# than `curl | iconv`, because a pipeline's status is its *last* command's: a 404 from curl
# would exit 0 through iconv and leave an empty checksum file behind. `sha256sum -c` does
# refuse one of those ("no properly formatted checksum lines found", exit 1), so the build
# would still fail -- but one step later and for the wrong reason, which is not a thing to
# leave resting on the next command's manners.
# --no-same-owner for the reason the node and uv tarballs have it: tar honours stored
# ownership when it runs as root, and the agent runs unattended, model-authored code.
# The tarball extracts flat rather than into a versioned directory of its own, so the version
# goes in the path here and /opt/powershell/bin holds the one symlink -- which is what keeps
# the PATH entry the same shape as /opt/node/bin and /opt/uv/bin, and what lets a later
# release be added beside this one rather than over it.
# pwsh --version is asserted here for the reason initdb --version, node --version and
# uv --version are: a moved download or a renamed asset has to fail the build, not the first
# session that runs the suite.
ARG PWSH_VERSION=""
RUN if [ -n "${PWSH_VERSION}" ]; then \
      case "${PWSH_VERSION}" in \
        ''|*[!0-9.]*|.*|*.) echo "PWSH_VERSION is a release version, e.g. 7.6.6, not ${PWSH_VERSION}" >&2; exit 1 ;; \
      esac \
   && arch="$(dpkg --print-architecture)" \
   && case "${arch}" in \
        amd64) pwsh_arch=x64 ;; \
        arm64) pwsh_arch=arm64 ;; \
        *) echo "no PowerShell/PowerShell build for ${arch}" >&2; exit 1 ;; \
      esac \
   && apt-get update \
   && icu="$(apt-cache --names-only search '^libicu[0-9][0-9]*$' | awk '{print $1}' | sort -V | tail -n1)" \
   && test -n "${icu}" \
   && apt-get install -y --no-install-recommends "${icu}" \
   && rm -rf /var/lib/apt/lists/* \
   && dist="https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}" \
   && tarball="powershell-${PWSH_VERSION}-linux-${pwsh_arch}.tar.gz" \
   && cd /tmp \
   && curl -fsSLO "${dist}/${tarball}" \
   && curl -fsSL "${dist}/hashes.sha256" -o hashes.utf16 \
   && iconv -f UTF-16 -t UTF-8 hashes.utf16 > hashes.sha256 \
   && sha256sum -c --ignore-missing hashes.sha256 \
   && install -d -m 0755 "/opt/powershell/${PWSH_VERSION}" \
   && tar --no-same-owner -xzf "${tarball}" -C "/opt/powershell/${PWSH_VERSION}" \
   && chmod 0755 "/opt/powershell/${PWSH_VERSION}/pwsh" \
   && rm -f "${tarball}" hashes.sha256 hashes.utf16 \
   && install -d -m 0755 /opt/powershell/bin \
   && ln -s "/opt/powershell/${PWSH_VERSION}/pwsh" /opt/powershell/bin/pwsh \
   && printf 'PATH="/opt/powershell/bin:$PATH"\n' > /etc/profile.d/issuebot-pwsh.sh \
   && chmod 0644 /etc/profile.d/issuebot-pwsh.sh \
   && /opt/powershell/bin/pwsh --version; \
    fi

# The worker and the session are different accounts (#75), and so is one session from the
# next (#121). `issuebot` (uid 1000) is the worker: it holds GH_TOKEN, the database URL and
# the Slack webhook, parses what the session writes and decides every label move. `agent`
# (uid 1001) is the session: `claude -p`, every hook, the clone and
# the post-clone setup run as it, and the credential it authenticates with comes from the
# environment (#142): its home holds no login, because nobody logs into it. Nothing the
# session can read or write at its own uid is an input to the worker: /app is root's and
# writable by neither, /home/issuebot and /home/agent are closed to the other account,
# /proc/<worker>/environ is unreadable across the uid line, and the worker's state inside a
# workspace sits in sticky directories it owns. The worker stays unprivileged: sudo carries
# exactly one rule, issuebot may become a session account and nobody else, and the binary is
# executable by root and group issuebot alone, so a session's uid cannot invoke sudo at all --
# not even to be refused by it.
# `closefrom_override` is for the one descriptor the worker passes across the uid change, the
# session's environment (issuebot.agent.runas); `!use_pty` keeps a turn's stream-json byte for
# byte when `docker compose run` gives the worker a terminal.
# A pool of them, in fact (#121). `agent` alone is one uid for the whole deployment, so with
# `agent.max_concurrent_agents` above 1 every concurrent session shares it: the workspaces are
# siblings under a traversable root and each one is that account's to write, which is no
# boundary between an issue anybody may open and an honest issue's working tree. So the image's
# default is the pool, `agent-1` .. `agent-N` (uids 1011 upwards, ISSUEBOT_AGENT_POOL_SIZE) --
# `agent` alone is the single-account route, for an operator who wants one -- and
# `agent.run_as` may name the pool -- as a YAML list, or a comma-separated ISSUEBOT_AGENT_USER
# -- for the orchestrator to bind one member per running slot.
# Every session account is in group `agents`, and the sudo rule is `(%agents)`: the worker may
# become any of them and nothing else. The worker is *not* in `agents`, and the binary is still
# executable by root and group issuebot alone, so no session account can invoke sudo.
# The worker *is* a supplementary member of each session account's own group, which is the one
# thing `share_with` needs: POSIX lets the owner of a file change its group only to one it
# belongs to, and a workspace is the worker's directory given to the bound account's group
# (1770). That membership buys the worker nothing else -- each home is 0700 -- and it is the
# more privileged side of the line in any case.
# The pool accounts get a `.claude` of their own and no volume: a pool shares no login between
# its accounts on purpose, and takes its credential from the environment instead (#121, the
# spec). `agent` is no different (#142): its home holds no login either, since nobody logs
# into it, and every account -- pooled or the single `agent` -- reads the same credential from
# the environment.
# A third kind of account, `web` (uid 1002), for the dashboard (#102). compose builds the `web`
# service from this image and selects it with `user: web`; nothing in the image runs as it by
# default, since `USER issuebot` below is the worker and `validate`. The dashboard takes HTTP
# from a browser and needs no privilege transition at all, so it must not carry the worker's:
# outside group issuebot it cannot execute sudo, and outside group `agents` the rule names
# nothing it could become; it owns nothing the worker or a session writes -- its home is closed
# to all of them and theirs to it, and /app is root's. `nologin` because no shell is ever opened
# as it: `issuebot web` is the one process, and a `docker compose exec web` still runs whatever
# command it names.
# A fourth kind, `egress` (uid 1003), for the allow-listing proxy (#126), on the same reasoning
# as `web` and for a sharper reason. Under compose the worker's networks are all internal, so the proxy
# container is the one process in the deployment with a route to the open internet; it reads a
# host name out of a CONNECT line and relays bytes it never looks at. It holds no credential,
# runs no session and touches no workspace, so it gets an account that can reach none of them:
# outside group issuebot it cannot execute sudo, outside `agents` the rule names nothing it
# could become, and its home is its own. `nologin` for the reason `web` has it.
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
# The account list is the built fact and nothing else (#142): `pool` accumulates inside the
# loop that runs `useradd`, and that accumulator is what is written, so the file can neither
# name an account the build did not create nor omit one it did. It is not re-derived from
# ISSUEBOT_AGENT_POOL_SIZE afterwards, because `seq` and `test` do not agree about what a
# value is -- `3 ` with a trailing space is a true `-ge 1` and an invalid count to `seq`, so
# the old shape wrote an empty list for an image with no pool accounts, and an empty list
# resolves to the host route, where the session *is* the worker. That inverts #75. An empty
# pool -- a build with the argument below 1 -- falls back to `agent` alone, the single-account
# route, so every image keeps "the session is never the worker" true.
# `printf '%s\n' ${pool:-agent}` and `for account in agent ${pool}` are unquoted on purpose,
# the only unquoted expansions here: the word-splitting is what turns one accumulated string
# into one account per line and one loop iteration per account. The names are `agent-N`, so
# there is nothing in them to split on but the spaces that separate them.
ARG ISSUEBOT_AGENT_POOL_SIZE=3
RUN set -eu; \
    groupadd --system agents; \
    useradd --create-home --uid 1000 --shell /bin/bash issuebot; \
    useradd --create-home --uid 1001 --groups agents --shell /bin/bash agent; \
    useradd --create-home --uid 1002 --shell /usr/sbin/nologin web; \
    useradd --create-home --uid 1003 --shell /usr/sbin/nologin egress; \
    pool=''; \
    for n in $(seq 1 "${ISSUEBOT_AGENT_POOL_SIZE}"); do \
      useradd --create-home --uid "$((1010 + n))" --groups agents --shell /bin/bash "agent-${n}"; \
      pool="${pool} agent-${n}"; \
    done; \
    for account in agent ${pool}; do \
      chmod 0700 "/home/${account}"; \
      install -d -m 0700 -o "${account}" -g "${account}" "/home/${account}/.claude"; \
      usermod --append --groups "${account}" issuebot; \
    done; \
    install -d -m 0755 /etc/issuebot; \
    printf '%s\n' ${pool:-agent} > /etc/issuebot/session-accounts; \
    chmod 0444 /etc/issuebot/session-accounts; \
    chmod 0750 /home/issuebot /home/web /home/egress; \
    install -d -m 0755 -o issuebot -g issuebot /workspaces; \
    git config --system --add safe.directory '/workspaces/*'; \
    printf '%s\n' \
      'Defaults:issuebot !use_pty, !syslog, !lecture, closefrom_override' \
      'issuebot ALL=(%agents) NOPASSWD: ALL' \
      > /etc/sudoers.d/issuebot; \
    chmod 0440 /etc/sudoers.d/issuebot; \
    visudo -cf /etc/sudoers.d/issuebot; \
    chgrp issuebot /usr/bin/sudo; \
    chmod 4750 /usr/bin/sudo

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
# (CI's image probes, which run `docker run --user agent` against this image) gets /home/agent
# and the worker /home/issuebot.
# No ISSUEBOT_AGENT_USER: `agent.run_as` falls back to /etc/issuebot/session-accounts, written
# by the account loop above, so the image's default is the pool it built rather than a name
# that could outlive the accounts (#142). The variable still overrides it for an operator who
# wants one account, and WORKFLOW.md overrides both.
ENV LANG=C.UTF-8 \
    PATH="/app/.venv/bin:${POSTGRES_VERSION:+/opt/postgresql/bin:}${NODE_VERSION:+/opt/node/bin:}${UV_VERSION:+/opt/uv/bin:}${PWSH_VERSION:+/opt/powershell/bin:}${PATH}"

# The flag assertions are the point of pinning: a release that drops --permission-prompts
# or --strict-mcp-config breaks an unattended worker at runtime -- the first by prompting
# where nobody can answer, the second by loading whatever MCP config the session account's
# home holds (#119) -- and one that drops --disallowedTools silently widens the session's
# tool set (#109), so fail the build instead. --permission-prompts and --strict-mcp-config
# are passed on every turn and neither is a setting, which is what puts them here rather
# than in `validate`; --disallowedTools carries the setting that fixes the tool set, and
# --mcp-config is the only route left by which a server reaches a session, so a rename there
# would break those deployments one session at a time; --setting-sources is what keeps the
# clone's own CLAUDE.md and .claude/ from being claude's configuration (#107), and it is
# passed on every turn whatever the front matter says. Then the delegation itself, as the
# worker will use it: sudo, the account, and claude under it.
# The last two lines read the account list back (#142). It is what `agent.run_as` resolves to
# in every container, so a build that wrote a list naming nothing would ship an image whose
# sessions run as the worker, and one naming an account the `useradd` loop did not create
# would fail every run instead: non-empty, and every name in it an account that resolves
# here, asserted where the delegation is.
RUN claude --version \
 && claude --help | grep -q -- '--permission-prompts' \
 && claude --help | grep -q -- '--disallowedTools' \
 && claude --help | grep -q -- '--strict-mcp-config' \
 && claude --help | grep -q -- '--mcp-config <' \
 && claude --help | grep -q -- '--setting-sources' \
 && test "$(sudo -n -u agent id -u)" = 1001 \
 && sudo -n -H -u agent claude --version \
 && { [ "${ISSUEBOT_AGENT_POOL_SIZE:-0}" -lt 1 ] \
      || { test "$(sudo -n -u agent-1 id -u)" = 1011 && sudo -n -H -u agent-1 claude --version; }; } \
 && test -s /etc/issuebot/session-accounts \
 && { while read -r account; do id -u "${account}" >/dev/null || exit 1; done; } \
      < /etc/issuebot/session-accounts

WORKDIR /app
# Mount the DIRECTORY holding WORKFLOW.md here, never the file itself: a single-file bind
# mount pins the inode, so an atomic save on the host leaves the container reading the old
# one (#46). compose.yaml mounts ./configs and sets this same value.
ENV ISSUEBOT_WORKFLOW=/configs/WORKFLOW.md
VOLUME ["/workspaces"]

LABEL org.opencontainers.image.source="https://github.com/jleavers/issuebot" \
      org.opencontainers.image.description="issuebot: issue-to-PR agent orchestrator" \
      org.opencontainers.image.version="${ISSUEBOT_VERSION}"

ENTRYPOINT ["issuebot"]
CMD ["validate"]
