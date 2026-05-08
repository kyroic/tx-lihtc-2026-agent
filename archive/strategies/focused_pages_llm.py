from __future__ import annotations

import time
from pathlib import Path

import pdfplumber

from ..extract import extract_one_pdf
from .base import StrategyResult


def _guess_relevant_page_count(pdf_path: Path) -> int:
    """
    Heuristic page-budget reducer:
    - Many key fields appear early; keep small N unless the PDF is short.
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            total = len(pdf.pages)
    except Exception:
        total = 0
    if total <= 0:
        return 25
    if total <= 25:
        return total
    return 18


class FocusedPagesLlmStrategy:
    """
    Variant idea:
    - Use a smaller page budget by default to reduce cost/latency.
    - If key fields are missing, rely on eval loop to decide whether to increase.
    """

    name = "focused_pages_llm"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        effective_pages = min(max_pages, _guess_relevant_page_count(pdf_path))
        row = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=effective_pages)
        return StrategyResult(row=row, wall_time_s=round(time.time() - t0, 3), meta={"max_pages_used": effective_pages})

