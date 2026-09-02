"""issuebot: an issue-to-PR agent orchestrator built on Claude and GitHub."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("issuebot")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"
