"""Runtime loop helpers."""
from __future__ import annotations

import time
from typing import Optional

from .errors import CommandError

def _check_wall_clock(deadline: Optional[float]) -> None:
    """Raise CommandError if a wall-clock deadline has elapsed.

    No-op when ``deadline`` is ``None`` (unlimited).
    """
    if deadline is not None and time.monotonic() > deadline:
        raise CommandError("Wall-clock timeout exceeded.")
