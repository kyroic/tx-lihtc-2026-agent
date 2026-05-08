from __future__ import annotations

import time
from pathlib import Path

from ..extract import ExtractedRow, field_from_obj
from .base import StrategyResult
from .openclaw_client import run_openclaw_agent


class OpenClawChecklistStrategy:
    """
    Prompt variant: forces a fixed checklist scan before answering.
    """

    name = "openclaw_checklist"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        agent = "main"
        msg = (
            "Open the PDF at this local path. Before extracting, do a checklist scan:\n"
            "1) Identify where the applicant/contact section is\n"
            "2) Identify where the development/project name is\n"
            "3) Identify where location/tract info is\n"
            "4) Identify where tie-breaker information is\n"
            "5) Identify where quartile/property rate/poverty metrics are\n\n"
            "Then extract the fields.\n"
            "Return ONLY JSON (no markdown).\n"
            "Rules: Never invent values; if missing, value=\"\" and confidence=0.\n"
            "Every non-empty value MUST include pages[] and a quote.\n\n"
            f"pdf_path: {str(pdf_path)}\n"
            "schema keys: application_name, contact_name, contact_email, contact_phone, "
            "tiebreaker_park, tiebreaker_school, tiebreaker_grocery, tiebreaker_library, "
            "quartile, property_rate, poverty_rank, census_tract\n"
        )
        out = run_openclaw_agent(agent=agent, message=msg, timeout_s=900)

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
            setattr(row, k, field_from_obj(out.get(k)))
        row.needs_review = True
        return StrategyResult(row=row, wall_time_s=round(time.time() - t0, 3), meta={"agent": agent, "checklist": True})

