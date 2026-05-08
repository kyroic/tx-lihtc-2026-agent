from __future__ import annotations

from .base import ExtractStrategy
from .v5_8_fast_tiebreaker import V5_8FastTieBreakerStrategy


def get_strategy(name: str = "") -> ExtractStrategy:
    """Returns the active extraction strategy (v5.8 fast tiebreaker only)."""
    return V5_8FastTieBreakerStrategy()


def list_strategies() -> list[str]:
    return ["v5_8_fast_tiebreaker"]
