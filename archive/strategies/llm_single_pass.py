from __future__ import annotations

import time
from pathlib import Path

from ..extract import extract_one_pdf
from .base import StrategyResult


class LlmSinglePassStrategy:
    name = "llm_single_pass"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        row = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=max_pages)
        return StrategyResult(row=row, wall_time_s=round(time.time() - t0, 3), meta={})

