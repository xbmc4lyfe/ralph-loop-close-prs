"""Shared exceptions for the Ralph loop."""
from __future__ import annotations

class CommandError(RuntimeError):
    """Raised when a subprocess command fails."""
