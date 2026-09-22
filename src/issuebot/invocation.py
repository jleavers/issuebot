"""How this deployment invokes issuebot, for the one-line remedies its messages name.

A leaf module, like ``issuebot.dsn`` and ``issuebot.pipes``, and for the same reason: four
places across three packages name the same remedy -- ``validate``'s ``github.labels`` check,
the worker's startup complaint, and the ``label not found`` error both GitHub adapters raise
-- and a remedy the operator cannot paste is not one.

Docker is the standard deployment (README, "validate and create the labels"), and inside the
image ``issuebot`` is nobody's command: the route in is ``docker compose run --rm worker
<subcommand>``, which is how the operator ran the ``validate`` that printed the hint (#169).
On a host it is the other way round -- ``uv run issuebot ...`` is the development route there,
and an operator who has built no image cannot run a compose service at all -- so the wording
is derived from where the process is rather than fixed either way.

``/etc/issuebot`` is what answers that, and it is the image's own fact rather than a guess:
the build creates the directory, beside the session-account list it writes inside it, and
nothing on a host does. Read at call time, never at import, so the suite can point the
constant somewhere of its own -- the rule ``config.resolve.SESSION_ACCOUNTS_FILE`` already
follows for that file, since the suite runs inside the image as well as on a host.
"""

from pathlib import Path

# The directory the image's build creates (Dockerfile: `install -d -m 0755 /etc/issuebot`).
# It is root's, so a session cannot conjure one, and its presence means this process runs in a
# container built from that image -- where compose is how issuebot's commands are run.
CONTAINER_MARKER = Path("/etc/issuebot")

# The service a one-off command is run as. `web` and `egress` run from the same image, but the
# commands that name a remedy are the worker's, and so is the README's own recipe.
COMPOSE_SERVICE = "worker"


def in_container() -> bool:
    """Whether this process is running in a container built from the issuebot image."""
    return CONTAINER_MARKER.is_dir()


def run_hint(subcommand: str) -> str:
    """The clause that tells the operator how to run ``issuebot <subcommand>`` here.

    Two shapes rather than one, because one of them carries the verb already: on a host the
    clause is an imperative, ``run issuebot labels ensure``, while the compose form is a
    command beginning with ``run`` itself and stands on its own, as ``docker compose build
    worker`` does in the pool-size complaint.
    """
    if in_container():
        return f"docker compose run --rm {COMPOSE_SERVICE} {subcommand}"
    return f"run issuebot {subcommand}"
