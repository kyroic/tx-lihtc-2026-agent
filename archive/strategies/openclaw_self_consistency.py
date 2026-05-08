from __future__ import annotations

import time
from pathlib import Path

from ..extract import ExtractedRow, field_from_obj
from .base import StrategyResult
from .openclaw_client import run_openclaw_agent


class OpenClawSelfConsistencyStrategy:
    """
    Run OpenClaw twice and keep the extraction that fills more required fields.
    """

    name = "openclaw_self_consistency"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        agent = "main"

        msg = (
            "Open the PDF at this local path and extract the requested fields.\n"
            "Return ONLY valid JSON (no markdown).\n"
            "Rules: Never invent values; if not present, value=\"\" and confidence=0.\n"
            "Every non-empty value MUST include pages[] and a short quote copied from the PDF.\n\n"
            f"pdf_path: {str(pdf_path)}\n"
            "schema keys: application_name, contact_name, contact_email, contact_phone, "
            "tiebreaker_park, tiebreaker_school, tiebreaker_grocery, tiebreaker_library, "
            "quartile, property_rate, poverty_rank, census_tract\n"
        )

        o1 = run_openclaw_agent(agent=agent, message=msg, timeout_s=900)
        o2 = run_openclaw_agent(agent=agent, message=msg, timeout_s=900)

        def score(o: dict) -> int:
            s = 0
            for k in ("application_name", "contact_email", "census_tract"):
                v = ((o.get(k) or {}).get("value") or "").strip()
                if v:
                    s += 1
            return s

        best = o2 if score(o2) >= score(o1) else o1

        row = ExtractedRow(source_pdf_path=str(pdf_path), source_pdf_sha256="")
        for k in (
            "application_name",
            "contact_name",
            "contact_email",
            "contact_phone",
            "tiebreaker_park",
            "tiebreaker_school",
            "tiebreaker_grocery",
            "tiebreaker_library",
            "quartile",
            "property_rate",
            "poverty_rank",
            "census_tract",
        ):
            setattr(row, k, field_from_obj(best.get(k)))
        row.needs_review = True
        return StrategyResult(row=row, wall_time_s=round(time.time() - t0, 3), meta={"agent": agent, "runs": 2})

