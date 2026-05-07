from __future__ import annotations

import time
from pathlib import Path

from ..extract import extract_one_pdf
from .base import StrategyResult


class LlmTwoPassStrategy:
    """
    Variant idea:
    - Pass 1: normal extraction
    - Pass 2: re-run with same model but higher max_pages if key fields missing
    """

    name = "llm_two_pass"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        row1 = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=max_pages)
        missing_key = (not row1.contact_email.value.strip()) or (not row1.census_tract.value.strip())
        if not missing_key:
            return StrategyResult(row=row1, wall_time_s=round(time.time() - t0, 3), meta={"passes": 1})

        # Pass 2: expand page budget (cap to avoid runaway).
        row2 = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=min(60, max_pages * 2))
        return StrategyResult(row=row2, wall_time_s=round(time.time() - t0, 3), meta={"passes": 2, "expanded_pages": True})

