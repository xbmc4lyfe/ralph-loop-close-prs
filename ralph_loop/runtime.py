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


def _round_numbers(max_rounds: int):
    if max_rounds <= 0:
        round_number = 1
        while True:
            yield round_number
            round_number += 1
    else:
        for round_number in range(1, max_rounds + 1):
            yield round_number
