"""Allow running as `python -m vhs_tool`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
