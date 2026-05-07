from __future__ import annotations

import time
from pathlib import Path

from ..extract import extract_one_pdf
from .base import StrategyResult


class SelfConsistencyVoteStrategy:
    """
    Variant idea:
    - Run the extractor multiple times (cheap model) and choose the most common value per field.
    - This is a first stub: it runs 2 passes and prefers the second pass if it improves required fields.
    """

    name = "self_consistency_vote"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        r1 = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=max_pages)
        r2 = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=max_pages)

        def score(r):
            s = 0
            if r.application_name.value.strip():
                s += 1
            if r.contact_email.value.strip():
                s += 1
            if r.census_tract.value.strip():
                s += 1
            return s

        best = r2 if score(r2) >= score(r1) else r1
        return StrategyResult(row=best, wall_time_s=round(time.time() - t0, 3), meta={"runs": 2})

