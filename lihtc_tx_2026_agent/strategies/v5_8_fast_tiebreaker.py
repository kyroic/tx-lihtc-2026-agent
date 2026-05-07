"""
V5.8 - Fast Tie-Breaker using pdftotext

Instead of pdfplumber (which parses entire PDF structure), use pdftotext CLI
which streams text extraction much faster.

Then extract only the Tie-Breaker pages with pdfplumber.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
    return "\n\n=== Coaching ===\n" + norm_ws(raw)[:12000] if raw else ""


def find_tiebreaker_pages_fast(pdf_path: Path) -> list[int]:
    """
    Use pdftotext to extract text with page markers, then search for title.
    Much faster than pdfplumber for large PDFs.
    """
    exact_titles = [
        "Tie-Breaker Information (Competitive HTC Only)",
        "Tie-Breaker Information",
        "Tie Breaker Information",
    ]

    try:
        # pdftotext with layout + page markers
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name

        subprocess.run(
            ["pdftotext", "-layout", "-nopgbrk", str(pdf_path), tmp_path],
            capture_output=True,
            timeout=60,
        )

        text = Path(tmp_path).read_text(errors="ignore")
        Path(tmp_path).unlink(missing_ok=True)

        # pdftotext doesn't add page markers by default, so we need another approach
        # Let's try with page breaks
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name

        subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), tmp_path],
            capture_output=True,
            timeout=60,
        )

        text = Path(tmp_path).read_text(errors="ignore")
        Path(tmp_path).unlink(missing_ok=True)

        # Split by page markers (form feed)
        pages = text.split("\f")
        found = []
        for i, page_text in enumerate(pages, 1):
            if any(title in page_text for title in exact_titles):
                found.append(i)
                if len(found) >= 10:  # Cap
                    break

        return found

    except Exception as e:
        # Fallback to pdfplumber
        return find_tiebreaker_pages_fallback(pdf_path)


def find_tiebreaker_pages_fallback(pdf_path: Path) -> list[int]:
    """Fallback using pdfplumber (slower but reliable)."""
    exact_titles = [
        "Tie-Breaker Information (Competitive HTC Only)",
        "Tie-Breaker Information",
    ]
    found = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                try:
                    text = page.extract_text() or ""
                    if any(title in text for title in exact_titles):
                        found.append(idx)
                        if len(found) >= 10:
                            break
                except Exception:
                    continue
    except Exception:
        pass
    return found


def extract_only_tiebreaker_pages(
    pdf_path: Path,
    tiebreaker_pages: list[int],
    max_standard_pages: int = 10,
) -> tuple[list[dict], list[dict]]:
    """
    Extract only:
    - First N pages (for app name, contact, etc.)
    - Tie-Breaker pages only

    Skip all other pages to save time.
    """
    standard = []
    tiebreaker = []
    tiebreaker_set = set(tiebreaker_pages)

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx in range(min(max_standard_pages, len(pdf.pages))):
                page = pdf.pages[idx]
                pn = idx + 1
                if pn in tiebreaker_set:
                    continue  # Will extract as tiebreaker
                try:
                    text = page.extract_text() or ""
                    standard.append({
                        "page": pn,
                        "text": text.strip()[:4000],
                        "tables": [],
                    })
                except Exception:
                    continue

            # Now extract only Tie-Breaker pages
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


def extract_one_pdf_v5_8(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    max_pages: int = 10,  # Reduced - only need first few pages for context
) -> ExtractedRow:
    """V5.8: Fast extraction using pdftotext + targeted page extraction."""

    # Step 1: Fast title search
    tiebreaker_pages = find_tiebreaker_pages_fast(pdf_path)

    # Step 2: Extract only necessary pages
    standard_pages, tiebreaker_content = extract_only_tiebreaker_pages(
        pdf_path, tiebreaker_pages, max_standard_pages=max_pages
    )

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
        if any(kw in txt for kw in ["development name", "project name", "property name"]):
            page_hints["application_name"].append(pn)
        if any(kw in txt for kw in ["contact", "prepared by"]):
            page_hints["contact"].append(pn)
        if any(kw in txt for kw in ["census tract", "tract", "geoid"]):
            page_hints["census_tract"].append(pn)

    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote.\n"
        "- CRITICAL: Focus on tiebreaker_pages for tiebreaker_* fields.\n"
        "- Distance values: '300 feet', '500 feet', '0.3 miles', etc.\n"
        "- Coordinates: decimal degrees like '29.7604', '-95.3698'. Preserve exactly as shown.\n"
        "- tiebreaker_score: the aggregate numeric score for the entire project (often a sum or total).\n"
        "- Each amenity may have a distance and lat/lng pair in the tiebreaker tables.\n"
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
        "distance_to_park": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "park_lat": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "park_lng": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "distance_to_school": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "school_lat": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "school_lng": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "distance_to_grocery": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "grocery_lat": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "grocery_lng": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "distance_to_library": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "library_lat": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "library_lng": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "site_lat": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "site_lng": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
        "tiebreaker_score": {"value": "", "confidence": 0.0, "pages": [], "quote": ""},
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

    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.8_fast_tiebreaker",
    )

    for k in schema_hint.keys():
        setattr(row, k, field_from_obj(out.get(k)))

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

    # Phase 2: flag incomplete coordinate pairs
    coord_pairs = [
        ("site_lat", "site_lng"),
        ("park_lat", "park_lng"),
        ("school_lat", "school_lng"),
        ("grocery_lat", "grocery_lng"),
        ("library_lat", "library_lng"),
    ]
    for lat_k, lng_k in coord_pairs:
        lat_v = getattr(row, lat_k).value.strip() if getattr(row, lat_k).value else ""
        lng_v = getattr(row, lng_k).value.strip() if getattr(row, lng_k).value else ""
        if (lat_v and not lng_v) or (lng_v and not lat_v):
            row.review_reasons.append(f"incomplete_coords:{lat_k}/{lng_k}")

    row.needs_review = bool(row.review_reasons)
    return row


class V5_8FastTieBreakerStrategy(ExtractStrategy):
    """V5.8: Fast Tie-Breaker using pdftotext + minimal page extraction."""

    name = "v5_8_fast_tiebreaker"
    description = "V5.8: Uses pdftotext CLI for fast title search, extracts only Tie-Breaker pages (not all pages)"

    def extract(
        self,
        *,
        project_id: str,
        model: str,
        pdf_path: Path,
        max_pages: int = 10,
    ) -> StrategyResult:
        start = time.time()
        row = extract_one_pdf_v5_8(
            project_id=project_id,
            model=model,
            pdf_path=pdf_path,
            max_pages=max_pages,
        )
        return StrategyResult(row=row, wall_time_s=time.time() - start, meta={})
