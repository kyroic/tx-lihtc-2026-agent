"""
V5.8 - Fast Tie-Breaker using pdftotext

Instead of pdfplumber (which parses entire PDF structure), use pdftotext CLI
which streams text extraction much faster.

Then extract only the Tie-Breaker pages with pdfplumber.
"""

from __future__ import annotations

import json
import os
import re
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
    # Broader fallback patterns for non-standard headers (e.g. "Tie-Breakers")
    broad_patterns = [
        r'Tie[- ]?Breakers?',
    ]

    found = []

    try:
        # Single pdftotext pass with form feeds for page splitting
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
        for i, page_text in enumerate(pages, 1):
            # First try exact titles (most reliable)
            if any(title in page_text for title in exact_titles):
                found.append(i)
            # Then try broad regex patterns for non-standard headers
            elif any(re.search(p, page_text) for p in broad_patterns):
                found.append(i)
            if len(found) >= 15:  # Cap slightly higher for broad patterns
                break

        if found:
            return found

        # If page-splitting found nothing, try the first-pass approach
        # (some PDFs don't have reliable form feeds)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name

        subprocess.run(
            ["pdftotext", "-layout", "-nopgbrk", str(pdf_path), tmp_path],
            capture_output=True,
            timeout=60,
        )

        raw_text = Path(tmp_path).read_text(errors="ignore")
        Path(tmp_path).unlink(missing_ok=True)

        # Line-based scanning: find tiebreaker lines and estimate page
        lines = raw_text.split("\n")
        for lineno, line in enumerate(lines):
            if any(title in line for title in exact_titles) or \
               any(re.search(p, line) for p in broad_patterns):
                # Rough page estimate: assume ~60 lines per page
                page_est = max(1, lineno // 60)
                if page_est not in found:
                    found.append(page_est)
            if len(found) >= 15:
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


def _build_coordinate_summary(tables: list, full_text: str) -> str:
    """
    Reconstruct a human-readable coordinate table from pdfplumber's
    fragmented table output.

    pdfplumber often splits the 4-row coordinate grid into narrow 2-column
    tables interleaved (amenity lat/lng, site boundary lat/lng, repeat).
    We group them by amenity so the LLM can read them easily.
    """
    import re

    # Collect 2-column coordinate tables (amenity or site boundary pairs)
    coord_tables: list[list[list[str]]] = []
    for t in tables:
        if not t or not t[0]:
            continue
        # A 2-col table where values look like coords
        is_coords = (
            len(t[0]) == 2
            and all(
                re.match(r'^-?\d{1,3}\.\d+$', str(c or ''))
                for row in t if row
                for c in row if c is not None
            )
        )
        if is_coords:
            coord_tables.append(t)

    if len(coord_tables) < 2:
        return "(no coordinate tables found)"

    # Detect amenity labels from full_text
    amenity_labels: list[str] = []
    for m in re.finditer(
        r'(Park|School|Grocery\s*Store|Library|Public\s*Library)',
        full_text, re.IGNORECASE
    ):
        label = m.group(1).strip()
        if label.lower() not in {l.lower() for l in amenity_labels}:
            amenity_labels.append(label)

    # Trim to 4
    amenity_labels = amenity_labels[:4]
    if len(amenity_labels) < 4:
        amenity_labels = ["Park", "School", "Grocery", "Library"]

    # Build summary: pair coord tables (amenity, site) for each amenity
    lines: list[str] = []
    for i, label in enumerate(amenity_labels):
        a_idx = i * 2
        s_idx = i * 2 + 1
        a_str = ""
        s_str = ""
        if a_idx < len(coord_tables) and coord_tables[a_idx] and coord_tables[a_idx][0]:
            a_str = ", ".join(str(c or "") for c in coord_tables[a_idx][0])
        if s_idx < len(coord_tables) and coord_tables[s_idx] and coord_tables[s_idx][0]:
            s_str = ", ".join(str(c or "") for c in coord_tables[s_idx][0])
        lines.append(
            f"  {label}: amenity_lat,amenity_lng = ({a_str}) | "
            f"site_lat,site_lng = ({s_str})"
        )

    if not lines:
        return "(could not parse coordinate tables)"

    return "COORDINATE TABLE (restructured):\n" + "\n".join(lines)


def _build_distance_summary(tables: list) -> str:
    """
    Restructure distance/score tables that pdfplumber may not detect
    as proper tables. Looks for raw table grids with amenity labels
    in the left column and numeric distance/score values in the right.
    """
    import re

    # Look for tables that match: [amenity_label, number] or [amenity_label, number, number]
    for t in tables:
        if not t or not t[0]:
            continue
        rows = []
        for row in t:
            if not row:
                continue
            # Filter out None-heavy rows
            clean = [str(c or "").strip() for c in row]
            if not any(clean):
                continue
            # Check if this looks like a distance row: label + number(s)
            has_label = any(
                re.search(r'(Park|School|Grocery|Library|Tie.Breaker)', c, re.IGNORECASE)
                for c in clean
            )
            has_num = any(
                re.match(r'^[\d,]+\.?\d*$', c)
                for c in clean
            )
            if has_label and has_num:
                rows.append(clean)

        if len(rows) >= 3:  # At least 3 data rows to be meaningful
            lines = ["DISTANCE TABLE (restructured):"]
            for row in rows:
                lines.append("  " + " | ".join(row))
            return "\n".join(lines)

    return ""


def _expand_tiebreaker_window(
    tiebreaker_pages: list[int],
    pdf_document,
    max_lookahead: int = 5,
) -> list[int]:
    """
    Agentically expand tiebreaker extraction to include continuation pages.

    Tie-breaker data (coordinates, distances, scores) often spans multiple
    pages beyond the header page. Scan forward from each header page and
    include any follow-on page that contains related tiebreaker content
    — numeric tables, amenity names, distance/score values — until we hit
    a clear section boundary like a new unrelated header.
    """
    import re

    expanded = set(tiebreaker_pages)

    for start_pn in tiebreaker_pages:
        for lookahead in range(1, max_lookahead + 1):
            pn = start_pn + lookahead
            if pn < 1 or pn > len(pdf_document.pages):
                break

            try:
                text = (pdf_document.pages[pn - 1].extract_text() or "").strip()
                tables = pdf_document.pages[pn - 1].extract_tables() or []
            except Exception:
                continue

            # Stop signals: clear section boundaries that end tiebreaker section
            stop_markers = [
                r"SPECIFICATIONS\s+AND\s+BUILDING",
                r"Development\s+Site\s+Information",
                r"RESOLUTION\s",
                r"April\s+\d{1,2},\s+20\d{2}",  # Letter date, not tiebreaker
            ]
            # Check stop signals only if the page has NO include signals
            # (some pages have both — e.g. distance table + TB#3 header)
            if any(re.search(m, text, re.IGNORECASE) for m in stop_markers):
                break

            # Include signals: this page has tiebreaker-related content
            include_signals = [
                r"Distance\s*\(feet\)",
                r"Tie[ -]?Breaker\s*:\s*[\d,]",
                r"\d{2}\.\d{5,}",  # Coordinate-like numbers
                r"(Park|School|Grocery|Library)\s+\d+",  # Amenity + distance
                r"Amenity",
                r"Site\s+Boundary",
            ]
            if not any(re.search(p, text, re.IGNORECASE) for p in include_signals):
                # If this page has no tiebreaker signals, stop looking forward
                break

            expanded.add(pn)

    return sorted(expanded)


# ── Post-extraction data cleaning ───────────────────────────────

def _clean_extracted_values(row: ExtractedRow, tiebreaker_content: list[dict]) -> None:
    """
    Normalize extracted values for consistent, clean output.
    Applied after LLM extraction but before validation.
    """
    import re

    # ── quartile: strip "q" suffix, validate numeric ─────────────
    q = (row.quartile.value or "").strip()
    if q:
        # "1q", "3q", "q1", "q3" → "1", "3"
        m = re.match(r'q?(\d+)\s*q?', q, re.IGNORECASE)
        if m:
            row.quartile.value = m.group(1)
        # If it's still non-numeric, try extracting first digit
        if not row.quartile.value.isdigit():
            m = re.search(r'\d+', row.quartile.value)
            if m:
                row.quartile.value = m.group(0)

    # ── tiebreaker_score: numeric only, strip commas ─────────────
    s = (row.tiebreaker_score.value or "").strip()
    if s:
        clean = s.replace(",", "").replace(" ", "")
        try:
            float(clean)
            row.tiebreaker_score.value = clean
        except ValueError:
            # Try to extract a number
            m = re.search(r'[\d,]+\.?\d*', s)
            if m:
                row.tiebreaker_score.value = m.group(0).replace(",", "")

    # ── distances: integer feet values ───────────────────────────
    for dist_key in ["distance_to_park", "distance_to_school",
                     "distance_to_grocery", "distance_to_library"]:
        v = (getattr(row, dist_key).value or "").strip()
        if not v:
            continue
        # Strip "feet", "ft", units, commas
        clean = re.sub(r'(?i)\s*(feet|ft|mile[s]?)\s*', '', v)
        clean = clean.replace(",", "").strip()
        try:
            # Store as integer string
            getattr(row, dist_key).value = str(int(float(clean)))
        except (ValueError, OverflowError):
            pass  # Leave as-is if unparseable

    # ── coordinates: standardize to ~6 decimal places ────────────
    for coord_key in ["site_lat", "site_lng", "park_lat", "park_lng",
                      "school_lat", "school_lng", "grocery_lat",
                      "grocery_lng", "library_lat", "library_lng"]:
        v = (getattr(row, coord_key).value or "").strip()
        if not v:
            continue
        try:
            num = float(v)
            # Preserve sign for longitude, round to 6dp
            getattr(row, coord_key).value = f"{num:.6f}"
        except (ValueError, OverflowError):
            pass  # Leave as-is if unparseable

    # ── emails: lowercase, strip whitespace ──────────────────────
    for email_key in ["contact_email"]:
        v = (getattr(row, email_key).value or "").strip()
        if v:
            getattr(row, email_key).value = v.lower()
            # Flag if no @
            if "@" not in v and v not in ("", "n/a", "none"):
                row.review_reasons.append(f"bad_email:{email_key}")

    # ── census_tract: normalize to FIPS format (11 digits) ───────
    ct = (row.census_tract.value or "").strip()
    if ct:
        # Strip non-digits, keep leading zeros
        digits = re.sub(r'[^\d]', '', ct)
        if len(digits) == 11:
            row.census_tract.value = digits
        else:
            # Flag if not FIPS-like
            row.census_tract.value = digits

    # ── poverty_rank: numeric percentile ─────────────────────────
    pr = (row.poverty_rank.value or "").strip()
    if pr:
        m = re.search(r'[\d.]+', pr)
        if m:
            row.poverty_rank.value = m.group(0)

    # ── property_rate: numeric ───────────────────────────────────
    pr2 = (row.property_rate.value or "").strip()
    if pr2:
        m = re.search(r'[\d.]+', pr2)
        if m:
            row.property_rate.value = m.group(0)

    # ── tiebreaker names: trim truncation markers ────────────────
    for name_key in ["tiebreaker_park", "tiebreaker_school",
                     "tiebreaker_grocery", "tiebreaker_library"]:
        v = (getattr(row, name_key).value or "").strip()
        if v:
            # Strip trailing artifacts
            v = re.sub(r'\s*…\s*$', '', v)
            v = re.sub(r'\s+\|\s*$', '', v)
            # Flag likely truncation (ends mid-word, looks short)
            if len(v) < 20 and v:
                # Check if it ends mid-word (no spaces near end)
                if " " not in v[-8:]:
                    row.review_reasons.append(f"possible_truncation:{name_key}")
            getattr(row, name_key).value = v


def extract_only_tiebreaker_pages(
    pdf_path: Path,
    tiebreaker_pages: list[int],
    max_standard_pages: int = 10,
) -> tuple[list[dict], list[dict], str, str]:
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

            # Agentically expand tiebreaker window: include continuation pages
            # that contain distance tables, coordinates, or scores.
            tiebreaker_set = set(_expand_tiebreaker_window(
                sorted(tiebreaker_set), pdf, max_lookahead=5
            ))

            # Now extract tiebreaker pages (expanded window)
            all_tb_tables = []
            all_tb_text = ""
            for pn in sorted(tiebreaker_set):
                if pn < 1 or pn > len(pdf.pages):
                    continue
                page = pdf.pages[pn - 1]
                try:
                    text = page.extract_text() or ""
                    tables = page.extract_tables() or []
                    all_tb_tables.extend(tables)
                    all_tb_text += text + "\n"
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

            # Build combined coordinate + distance summaries across ALL
            # tiebreaker pages (library coords often page N+1, label page N).
            combined_coord = _build_coordinate_summary(all_tb_tables, all_tb_text)
            combined_dist = _build_distance_summary(all_tb_tables)

    except Exception:
        pass

    return standard, tiebreaker, combined_coord, combined_dist


def extract_one_pdf_v5_8(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    max_pages: int = 15,  # Bumped from 10 to capture census_tract, quartile, poverty_rank
) -> ExtractedRow:
    """V5.8: Fast extraction using pdftotext + targeted page extraction."""

    # Step 1: Fast title search
    tiebreaker_pages = find_tiebreaker_pages_fast(pdf_path)

    # ── Step 2: Extract only necessary pages
    # Bump standard pages to 15 to capture census_tract/quartile/poverty_rank
    # which often appear in later standard pages.
    standard_pages, tiebreaker_content, combined_coord, combined_dist = extract_only_tiebreaker_pages(
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
        "Return ONLY valid JSON in the output schema format. Never invent values.\n"
        "Every non-empty value MUST include pages[] and a short quote.\n"
        "- standard_pages: Extract application_name, contact_name/email/phone, census_tract, quartile (digit only), poverty_rank, property_rate from labels in the text.\n"
        "- tiebreaker_pages_extracted + combined_coordinate_summary + combined_distance_summary: Extract ALL tiebreaker, coordinate, distance, and score fields.\n"
        "  Use the pre-parsed COORDINATE TABLE and DISTANCE TABLE blocks FIRST — they are already restructured for you. Fall back to raw text/tables only if needed.\n"
        "  Coordinates: decimal degrees, copy EXACTLY from pre-parsed tables. For site_lat/site_lng, use the Park row's site values (first amenity row).\n"
        "  Distances: whole feet, number only.\n"
        "  tiebreaker_score: the Tie-Breaker: total from DISTANCE TABLE, digits only.\n"
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
        "combined_coordinate_summary": combined_coord,
        "combined_distance_summary": combined_dist,
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

    # ── Post-extraction cleaning ──────────────────────────────────
    _clean_extracted_values(row, tiebreaker_content or [])

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
