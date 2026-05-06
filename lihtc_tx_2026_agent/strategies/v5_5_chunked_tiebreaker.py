"""
V5.5 - Chunked Tie-Breaker Extraction Strategy

Key insight: 100% of PDFs contain Tie-Breaker pages, but they're scattered throughout
(pages 100-400+), not just in the first 50 pages.

Approach:
1. First pass: Search ALL pages for "Tie-Breaker" keyword (lightweight text search)
2. Second pass: Extract Tie-Breaker pages thoroughly (tables + full text)
3. Third pass: LLM extraction with explicit Tie-Breaker page content included

This avoids memory issues from processing 400+ page PDFs at once while ensuring
we capture the critical distance/coordinate data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

from ..extract import ExtractedRow, field_from_obj, sha256_file
from ..model_client import chat_completions, extract_json_content
from .base import ExtractStrategy, StrategyResult


@dataclass
class FieldEvidence:
    value: str = ""
    confidence: float = 0.0
    pages: list[int] = field(default_factory=list)
    quote: str = ""


def norm_ws(s: str) -> str:
    return " ".join((s or "").split())


def coaching_append_from_env() -> str:
    raw = (os.environ.get("LIHTC_COACHING_APPEND") or "").strip()
    if not raw:
        return ""
    return (
        "\n\n=== Iteration coaching (from prior eval; follow strictly) ===\n"
        + norm_ws(raw)[:12000]
    )


def find_tiebreaker_pages(pdf_path: Path) -> list[int]:
    """
    First pass: Lightweight search for Tie-Breaker pages across ALL pages.
    Returns list of page numbers (1-indexed) that contain Tie-Breaker keywords.
    """
    tiebreaker_keywords = [
        "tie-breaker",
        "tiebreaker",
        "tie breaker",
        "Tie-Breaker Information",
        "Competitive HTC Only",
    ]
    
    found_pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            try:
                text = (page.extract_text() or "").lower()
                if any(kw.lower() in text for kw in tiebreaker_keywords):
                    found_pages.append(idx)
            except Exception:
                continue
    return found_pages


def extract_chunked_pages(
    pdf_path: Path,
    page_numbers: list[int],
    chunk_size: int = 50,
) -> list[dict[str, Any]]:
    """
    Second pass: Extract specific pages in chunks to avoid memory issues.
    Returns list of page dicts with full text and tables.
    """
    extracted = []
    
    # Sort and dedupe pages
    pages_to_extract = sorted(set(page_numbers))
    
    # Process in chunks
    for i in range(0, len(pages_to_extract), chunk_size):
        chunk = pages_to_extract[i : i + chunk_size]
        
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num in chunk:
                if page_num < 1 or page_num > len(pdf.pages):
                    continue
                    
                page = pdf.pages[page_num - 1]
                try:
                    text = page.extract_text() or ""
                    tables = page.extract_tables() or []
                    
                    # Format tables as text
                    table_text = []
                    for table_idx, table in enumerate(tables):
                        if table:
                            rows = []
                            for row in table:
                                if row:
                                    cells = [str(cell or "").strip() for cell in row if cell is not None]
                                    if cells:
                                        rows.append(" | ".join(cells))
                            if rows:
                                table_text.append(f"[Table {table_idx + 1}]\n" + "\n".join(rows))
                    
                    extracted.append({
                        "page": page_num,
                        "text": text.strip()[:8000],  # Generous limit
                        "tables": table_text,
                        "full_text": text.strip(),  # Keep full for Tie-Breaker pages
                    })
                except Exception:
                    continue
    
    return extracted


def extract_all_pages_lightweight(pdf_path: Path, max_pages: int = 50) -> list[dict[str, Any]]:
    """
    Extract first N pages with basic text (for non-Tie-Breaker content like application name, etc.)
    """
    pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages[:max_pages], start=1):
            try:
                text = page.extract_text() or ""
                pages.append({"page": idx, "text": text.strip()[:4000], "tables": []})
            except Exception:
                continue
    return pages


def extract_one_pdf_v5_5(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    max_pages: int = 50,
) -> ExtractedRow:
    """V5.5: Chunked Tie-Breaker extraction."""
    # Step 1: Find all Tie-Breaker pages across entire PDF
    tiebreaker_pages = find_tiebreaker_pages(pdf_path)
    
    # Step 2: Extract first N pages (standard content)
    standard_pages = extract_all_pages_lightweight(pdf_path, max_pages=max_pages)
    
    # Step 3: Extract Tie-Breaker pages thoroughly (chunked)
    tiebreaker_content = extract_chunked_pages(pdf_path, tiebreaker_pages, chunk_size=50)
    
    # Build page hints for standard fields
    def has_any(t: str, needles: list[str]) -> bool:
        return any(n in t for n in needles)
    
    page_hints = {
        "application_name": [],
        "contact": [],
        "census_tract": [],
        "tiebreaker_pages": tiebreaker_pages,  # Explicitly tell LLM which pages are Tie-Breaker
    }
    
    for p in standard_pages:
        pn = int(p.get("page") or 0)
        txt = norm_ws(str(p.get("text") or "")).lower()
        if not pn or not txt:
            continue
        
        if has_any(txt, ["development name", "project name", "property name", "application name"]):
            page_hints["application_name"].append(pn)
        if has_any(txt, ["contact", "prepared by", "authorized representative"]):
            page_hints["contact"].append(pn)
        if has_any(txt, ["census tract", "tract", "geoid"]):
            page_hints["census_tract"].append(pn)
    
    # Build system prompt
    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote copied from the PDF text.\n"
        "- CRITICAL: The 'tiebreaker_pages' field tells you which pages contain Tie-Breaker Information.\n"
        "- For tiebreaker_* fields (park, school, grocery, library), focus on those pages.\n"
        "- Distance values are often in format like '300 feet', '500 feet', '0.3 miles', etc.\n"
        "- Coordinates may appear as decimal numbers (latitude/longitude).\n"
    ) + coaching_append_from_env()
    
    # Build user prompt with both standard and Tie-Breaker content
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
        "standard_pages": standard_pages,  # First N pages
        "tiebreaker_pages_extracted": tiebreaker_content,  # Tie-Breaker pages with full content
        "tiebreaker_page_numbers": tiebreaker_pages,
        "output_schema_example": schema_hint,
    }
    
    # Call LLM
    resp = chat_completions(
        project_id=project_id,
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)}],
        temperature=0.0,
    )

    out = extract_json_content(resp)

    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.5_chunked_tiebreaker",
    )

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

    required = {
        "application_name": row.application_name.value,
        "contact_email": row.contact_email.value,
        "census_tract": row.census_tract.value,
    }
    for k, v in required.items():
        if not (v or "").strip():
            row.review_reasons.append(f"missing:{k}")

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
        f = getattr(row, k)
        if f.value and (not f.pages or not f.quote.strip()):
            row.review_reasons.append(f"missing_evidence:{k}")

    row.needs_review = bool(row.review_reasons)
    return row


class V5_5ChunkedTieBreakerStrategy(ExtractStrategy):
    """V5.5: Chunked Tie-Breaker extraction with full page content."""
    
    name = "v5_5_chunked_tiebreaker"
    description = "V5.5: Searches ALL pages for Tie-Breaker keywords, extracts those pages thoroughly in chunks, includes full content in LLM prompt"
    
    def extract(
        self,
        *,
        project_id: str,
        model: str,
        pdf_path: Path,
        max_pages: int = 50,
    ) -> StrategyResult:
        import time
        start = time.time()
        row = extract_one_pdf_v5_5(
            project_id=project_id,
            model=model,
            pdf_path=pdf_path,
            max_pages=max_pages,
        )
        return StrategyResult(row=row, wall_time_s=time.time() - start, meta={})
