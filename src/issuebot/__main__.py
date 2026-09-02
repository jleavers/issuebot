"""Allow ``python -m issuebot``."""

import sys

from issuebot.cli import main

if __name__ == "__main__":
    sys.exit(main())
