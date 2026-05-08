from __future__ import annotations

import re
import time
from pathlib import Path

import pdfplumber

from ..extract import extract_one_pdf, FieldEvidence
from .base import StrategyResult


EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b")
TRACT_RE = re.compile(r"(?i)\b(?:census\s+tract|tract)\b[^\d]{0,15}(\d{1,4}(?:\.\d{1,4})?)")


def _read_first_pages(pdf_path: Path, max_pages: int) -> str:
    out = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for p in pdf.pages[:max_pages]:
            try:
                t = p.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                out.append(t)
    return "\n\n".join(out)


class RegexThenLlmStrategy:
    """
    Variant idea:
    - Use cheap regex to prefill easy fields (email, tract).
    - Run LLM extraction anyway, but keep regex-prefill when LLM returns empty.
    """

    name = "regex_then_llm"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        text = _read_first_pages(pdf_path, max_pages=min(10, max_pages))
        m_email = EMAIL_RE.search(text or "")
        m_tract = TRACT_RE.search(text or "")

        pre_email = m_email.group(0).lower() if m_email else ""
        pre_tract = m_tract.group(1) if m_tract else ""

        row = extract_one_pdf(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=max_pages)

        used = {}
        if pre_email and not row.contact_email.value.strip():
            row.contact_email = FieldEvidence(value=pre_email, confidence=0.5, pages=[1], quote="regex_match")
            used["contact_email"] = "regex_prefill"
        if pre_tract and not row.census_tract.value.strip():
            row.census_tract = FieldEvidence(value=pre_tract, confidence=0.5, pages=[1], quote="regex_match")
            used["census_tract"] = "regex_prefill"

        if used:
            row.review_reasons.append("used_regex_prefill")
            row.needs_review = True

        return StrategyResult(row=row, wall_time_s=round(time.time() - t0, 3), meta={"prefill": used})

