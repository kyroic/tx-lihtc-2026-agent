from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..extract import ExtractedRow


@dataclass
class StrategyResult:
    row: ExtractedRow
    wall_time_s: float
    meta: dict


class ExtractStrategy(Protocol):
    """
    Strategy interface for running the agent multiple ways.
    """

    name: str

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        ...

