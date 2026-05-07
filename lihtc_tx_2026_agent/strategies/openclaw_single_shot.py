from __future__ import annotations

import time
from pathlib import Path

from ..extract import ExtractedRow, field_from_obj
from .base import StrategyResult
from .openclaw_client import run_openclaw_agent


class OpenClawSingleShotStrategy:
    """
    OpenClaw reads the PDF and returns the full JSON extraction in one shot.
    """

    name = "openclaw_single_shot"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        agent = "main"
        msg = (
            "Open the PDF at this local path and extract the requested fields.\n"
            "Return ONLY valid JSON (no markdown, no commentary).\n"
            "Rules: Never invent values; if not present, value=\"\" and confidence=0.\n"
            "Every non-empty value MUST include pages[] and a short quote copied from the PDF.\n\n"
            f"pdf_path: {str(pdf_path)}\n"
            f"max_pages_hint: {max_pages}\n"
            "schema:\n"
            "{\n"
            '  "application_name": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "contact_name": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "contact_email": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "contact_phone": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "tiebreaker_park": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "tiebreaker_school": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "tiebreaker_grocery": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "tiebreaker_library": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "quartile": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "property_rate": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "poverty_rank": {"value":"","confidence":0,"pages":[],"quote":""},\n'
            '  "census_tract": {"value":"","confidence":0,"pages":[],"quote":""}\n'
            "}\n"
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
        row.needs_review = True  # force review unless labels prove otherwise
        return StrategyResult(row=row, wall_time_s=round(time.time() - t0, 3), meta={"agent": agent})

