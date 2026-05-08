"""
V5.7 - Targeted Tie-Breaker Extraction

Key optimization: Search for exact page title FIRST, then only extract those pages.

Page title to find: "Tie-Breaker Information (Competitive HTC Only)"

This is MUCH faster than V5.5's approach of scanning all pages.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

from ..extract import ExtractedRow, field_from_obj, sha256_file, norm_ws
from ..model_client import chat_completions, extract_json_content
from .base import ExtractStrategy, StrategyResult


def coaching_append_from_env() -> str:
    raw = (os.environ.get("LIHTC_COACHING_APPEND") or "").strip()
    if not raw:
        return ""
    return "\n\n=== Iteration coaching ===\n" + norm_ws(raw)[:12000]


def find_tiebreaker_page_by_title(pdf_path: Path) -> list[int]:
    """
    Fast scan: find pages containing the EXACT title.
    Returns list of page numbers (1-indexed).
    """
    exact_titles = [
        "Tie-Breaker Information (Competitive HTC Only)",
        "Tie-Breaker Information",
        "Tie Breaker Information",
    ]
    
    found = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                try:
                    text = page.extract_text() or ""
                    # Check for exact title match
                    if any(title in text for title in exact_titles):
                        found.append(idx)
                except Exception:
                    continue
    except Exception:
        pass
    
    return found


def extract_targeted_pages(
    pdf_path: Path,
    page_numbers: list[int],
    max_pages: int = 50,
) -> tuple[list[dict], list[dict]]:
    """
    Extract only the targeted pages + first N pages for context.
    Returns (standard_pages, tiebreaker_pages).
    """
    standard = []
    tiebreaker = []
    
    # First N pages for general info
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx in range(min(max_pages, len(pdf.pages))):
                page = pdf.pages[idx]
                try:
                    text = page.extract_text() or ""
                    tables = page.extract_tables() or []
                    table_text = []
                    for t in tables:
                        if t:
                            rows = [" | ".join(str(c or "") for c in row if c is not None) for row in t if row]
                            if rows:
                                table_text.append("\n".join(rows))
                    
                    standard.append({
                        "page": idx + 1,
                        "text": text.strip()[:4000],
                        "tables": table_text,
                    })
                except Exception:
                    continue
    except Exception:
        pass
    
    # Targeted Tie-Breaker pages
    tiebreaker_set = set(page_numbers)
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pn in tiebreaker_set:
                if pn < 1 or pn > len(pdf.pages):
                    continue
                page = pdf.pages[pn - 1]
                try:
                    text = page.extract_text() or ""
                    tables = page.extract_tables() or []
                    table_text = []
                    for t in tables:
                        if t:
                            rows = [" | ".join(str(c or "") for c in row if c is not None) for row in t if row]
                            if rows:
                                table_text.append("\n".join(rows))
                    
                    tiebreaker.append({
                        "page": pn,
                        "text": text.strip()[:8000],
                        "tables": table_text,
                        "full_text": text.strip(),
                    })
                except Exception:
                    continue
    except Exception:
        pass
    
    return standard, tiebreaker


def extract_one_pdf_v5_7(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    max_pages: int = 50,
) -> ExtractedRow:
    """V5.7: Targeted extraction - find Tie-Breaker page by title, extract only those."""
    
    # Step 1: Find Tie-Breaker pages by exact title
    tiebreaker_pages = find_tiebreaker_page_by_title(pdf_path)
    
    # Step 2: Extract only targeted pages
    standard_pages, tiebreaker_content = extract_targeted_pages(pdf_path, tiebreaker_pages, max_pages)
    
    # Step 3: Build prompt
    page_hints = {
        "application_name": [],
        "contact": [],
        "census_tract": [],
        "tiebreaker_pages": tiebreaker_pages,
    }
    
    for p in standard_pages:
        pn = int(p.get("page") or 0)
        txt = norm_ws(str(p.get("text") or "")).lower()
        if not pn or not txt:
            continue
        
        if any(kw in txt for kw in ["development name", "project name", "property name", "application name"]):
            page_hints["application_name"].append(pn)
        if any(kw in txt for kw in ["contact", "prepared by", "authorized representative"]):
            page_hints["contact"].append(pn)
        if any(kw in txt for kw in ["census tract", "tract", "geoid"]):
            page_hints["census_tract"].append(pn)
    
    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote copied from the PDF text.\n"
        "- CRITICAL: The 'tiebreaker_pages' field tells you which pages contain Tie-Breaker Information.\n"
        "- For tiebreaker_* fields (park, school, grocery, library), focus on those pages.\n"
        "- Distance values are often in format like '300 feet', '500 feet', '0.3 miles', etc.\n"
        "- Look for tables on Tie-Breaker pages - they contain distance data.\n"
    ) + coaching_append_from_env()
    
    schema_hint = {
        "application_name": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "contact_name": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "contact_email": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "contact_phone": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "tiebreaker_park": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "tiebreaker_school": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "tiebreaker_grocery": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "tiebreaker_library": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "quartile": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "property_rate": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "poverty_rank": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "census_tract": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
    }
    
    user_data = {
        "pdf_filename": pdf_path.name,
        "page_hints": page_hints,
        "standard_pages": standard_pages,
        "tiebreaker_pages_extracted": tiebreaker_content,
        "tiebreaker_page_numbers": tiebreaker_pages,
        "output_schema_example": schema_hint,
    }
    
    # Step 4: Call LLM
    resp = chat_completions(
        project_id=project_id,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)},
        ],
        temperature=0.0,
    )
    
    out = extract_json_content(resp)
    
    # Step 5: Build row
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.7_targeted_tiebreaker",
    )
    
    for k in schema_hint.keys():
        setattr(row, k, field_from_obj(out.get(k)))
    
    # Validation
    required = {
        "application_name": row.application_name.value,
        "contact_email": row.contact_email.value,
        "census_tract": row.census_tract.value,
    }
    for k, v in required.items():
        if not (v or "").strip():
            row.review_reasons.append(f"missing:{k}")
    
    for k in schema_hint.keys():
        f = getattr(row, k)
        if f.value and (not f.pages or not f.quote.strip()):
            row.review_reasons.append(f"missing_evidence:{k}")
    
    row.needs_review = bool(row.review_reasons)
    return row


class V5_7TargetedTieBreakerStrategy(ExtractStrategy):
    """V5.7: Targeted Tie-Breaker extraction - find by title, extract only those pages."""
    
    name = "v5_7_targeted_tiebreaker"
    description = "V5.7: Finds Tie-Breaker pages by exact title match, extracts only those pages (much faster than V5.5)"
    
    def extract(
        self,
        *,
        project_id: str,
        model: str,
        pdf_path: Path,
        max_pages: int = 50,
    ) -> StrategyResult:
        start = time.time()
        row = extract_one_pdf_v5_7(
            project_id=project_id,
            model=model,
            pdf_path=pdf_path,
            max_pages=max_pages,
        )
        return StrategyResult(row=row, wall_time_s=time.time() - start, meta={})
