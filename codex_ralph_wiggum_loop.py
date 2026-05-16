#!/usr/bin/env python3
"""Compatibility entry point for the Ralph loop CLI."""
from __future__ import annotations

import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "ERROR: Python 3.8+ is required (uses shlex.join); got {}.{}.\n".format(
            sys.version_info.major, sys.version_info.minor
        )
    )
    raise SystemExit(2)

from ralph_loop.cli import main
from ralph_loop.errors import CommandError


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as exc:
        sys.stderr.write("ERROR: {}\n".format(exc))
        raise SystemExit(1)
