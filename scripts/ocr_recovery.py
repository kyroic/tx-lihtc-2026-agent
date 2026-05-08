#!/usr/bin/env python3
"""
OCR-based recovery for missing field values.

Takes the fixed CSV, identifies rows with missing values in key fields,
renders the relevant PDF pages as images, and sends them to an LLM
with vision capability for extraction.

Supports:
  - Tiebreaker coordinate/distance tables
  - Site Demographic page (poverty_rank, quartile, census_tract)
  - Contact info from cover page
  - Site coordinates from Site Information Form

Usage:
  python scripts/ocr_recovery.py --in out_verify/applications_fixed.csv --out out_verify/applications_ocr.csv --model gpt-4o
"""

import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Fields we can OCR-recover ──────────────────────────────────────

CONTACT_FIELDS = {"contact_name", "contact_email", "contact_phone"}  # contact_title not in TDHCA forms
SITE_FIELDS = {"site_lat", "site_lng"}
DEMOGRAPHIC_FIELDS = {"poverty_rank", "quartile", "census_tract"}
TIEBREAKER_FIELDS = {
    "tiebreaker_score", "tiebreaker_park", "tiebreaker_school",
    "tiebreaker_grocery", "tiebreaker_library",
    "distance_to_park", "distance_to_school", "distance_to_grocery", "distance_to_library",
    "park_lat", "park_lng", "school_lat", "school_lng",
    "grocery_lat", "grocery_lng", "library_lat", "library_lng",
}


# ── Page finding helpers ───────────────────────────────────────────

def _find_contact_page(pdf_path: Path) -> int:
    """Find the cover/contact info page (usually page 1)."""
    import pdfplumber
    try:
            for i in range(min(5, len(pdf.pages))):
                text = (pdf.pages[i].extract_text() or "").lower()
                if "contact name" in text or "application #" in text:
                    return i + 1
    except Exception:
        pass
    return 1  # Default to page 1


def _find_site_info_page(pdf_path: Path) -> int:
    """Find the Site Information Form Part I page (has site coords)."""
    import pdfplumber
    try:
            for i in range(len(pdf.pages)):
                text = (pdf.pages[i].extract_text() or "").lower()
                if "site information form part i" in text and ("latitude" in text or "longitude" in text or "development address" in text):
                    return i + 1
    except Exception:
        pass
    return -1


def _find_demographic_page(pdf_path: Path) -> int:
    """Find the Site Demographic Characteristics Report page."""
    import pdfplumber
    try:
            for i in range(len(pdf.pages)):
                text = (pdf.pages[i].extract_text() or "").lower()
                if "site demographic" in text and ("poverty" in text or "census" in text):
                    return i + 1
    except Exception:
        pass
    return -1


# ── Image rendering ────────────────────────────────────────────────

def _render_pages(pdf_path: Path, pages: list[int], resolution: int = 200) -> list[Path]:
    """Render specific PDF pages as PNG images. Returns list of temp file paths."""
    import pdfplumber
    images: list[Path] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pn in pages:
                if 1 <= pn <= len(pdf.pages):
                    img = pdf.pages[pn - 1].to_image(resolution=resolution)
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    img.save(tmp.name)
                    images.append(Path(tmp.name))
    except Exception as e:
        print(f"  [warn] pdfplumber render failed: {e}")
    return images


def _render_adjacent(pdf_path: Path, base_pn: int, radius: int = 2) -> list[Path]:
    """Render base_pn + surrounding pages."""
    pages = list(range(max(1, base_pn - radius), base_pn + radius + 1))
    return _render_pages(pdf_path, pages)


def _render_demographic_pages(pdf_path: Path) -> list[Path]:
    """Render the demographic page(s) for this PDF."""
    pn = _find_demographic_page(pdf_path)
    if pn < 1:
        return []
    return _render_adjacent(pdf_path, pn, radius=3)


def _render_tiebreaker_pages(pdf_path: Path) -> list[Path]:
    """Render tiebreaker + surrounding pages."""
    from lihtc_tx_2026_agent.strategies.v5_8_fast_tiebreaker import find_tiebreaker_pages_fast
    tb = find_tiebreaker_pages_fast(pdf_path)
    if not tb:
        # Fallback: search for "Tie-Breaker" keyword
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf_path)) as pdf:
                for i in range(len(pdf.pages)):
                    text = (pdf.pages[i].extract_text() or "").lower()
                    if "tie-breaker" in text:
                        tb = [i + 1]
                        break
        except Exception:
            pass
        if not tb:
            return []

    all_pages: set[int] = set()
    for pn in tb:
        for offset in range(-3, 4):
            all_pages.add(pn + offset)
    return _render_pages(pdf_path, sorted(all_pages))


# ── LLM calling ────────────────────────────────────────────────────

def _call_vision_llm(
    images: list[Path],
    prompt: str,
    model: str = "gpt-4o",
) -> Optional[dict[str, Any]]:
    """Send images to a vision-capable LLM. Returns parsed JSON."""
    if not images:
        return None

    # Direct OpenAI chat completions call
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("  [warn] No OPENAI_API_KEY set, skipping vision call")
        return None

    import urllib.request

    # Build content array
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for imp in images:
        with open(imp, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"},
        })

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        content_str = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content_str:
            return None
        # Try to parse JSON, with fallback to extracting from code blocks
        try:
            return json.loads(content_str)
        except json.JSONDecodeError:
            # Try extracting JSON from markdown code blocks
            import re
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content_str, re.DOTALL)
            if m:
                return json.loads(m.group(1))
            # Try finding raw JSON object
            m = re.search(r'\{[^{}]*\}', content_str, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            print(f"  [warn] Could not parse JSON: {content_str[:200]}")
            return None
    except Exception as e:
        print(f"  [warn] Vision LLM call failed: {e}")
        return None


# ── Extraction functions ───────────────────────────────────────────

CONTACT_PROMPT = """This is the cover/contact page of a Texas LIHTC housing tax credit application PDF.

Extract the following into JSON:
- contact_name: the full name of the contact person
- contact_title: their job title (e.g. "Manager", "Developer")
- contact_email: their email address
- contact_phone: their phone number (format as plain digits or (xxx) xxx-xxxx)

Only include fields where the value is clearly visible. Return JSON."""


DEMOGRAPHIC_PROMPT = """This is the Site Demographic Characteristics Report from a Texas LIHTC application PDF.

Extract:
- poverty_rank: The poverty rate number (a percentage like 19.61 or 32.45 or similar). Look for "Poverty Rate" label.
- quartile: The census tract quartile number (1-4). Look for "Quartile" or "Income Quartile".
- census_tract: The 11-digit census tract FIPS code (e.g. "48355005606"). Looks like 48XXX... or similar.

Only include fields that are clearly visible. Return JSON."""


TIEBREAKER_PROMPT = """These are the Tie-Breaker pages from a Texas LIHTC housing tax credit application PDF.

Extract into JSON:
- tiebreaker_park: park/amenity name (e.g. "Sugarberry Park")
- tiebreaker_school: school name (e.g. "Kennemer Middle School")
- tiebreaker_grocery: grocery store name (e.g. "Walmart Super Center")
- tiebreaker_library: library name (e.g. "Mountain Creek Branch Library")
- distance_to_park: distance in feet (number only)
- distance_to_school: distance in feet (number only)
- distance_to_grocery: distance in feet (number only)
- distance_to_library: distance in feet (number only)
- tiebreaker_score: the Tie-Breaker total/sum score
- park_lat: park amenity latitude (decimal degrees, NOT site boundary lat)
- park_lng: park amenity longitude
- school_lat: school amenity latitude
- school_lng: school amenity longitude
- grocery_lat: grocery amenity latitude
- grocery_lng: grocery amenity longitude
- library_lat: library amenity latitude
- library_lng: library amenity longitude

IMPORTANT: Look for the coordinate table with 4 columns (Site Lat, Site Lng, Amenity Lat, Amenity Lng). Extract the AMENITY coordinates (not the site coordinates). The amenity coordinates are typically in the 3rd and 4th columns.

Only include fields that are clearly visible. Return JSON."""


SITE_PROMPT = """This is a Site Information Form from a Texas LIHTC application PDF.

Extract:
- site_lat: the development site latitude (decimal degrees)
- site_lng: the development site longitude (decimal degrees)

Look for "Development Latitude" and "Development Longitude" labels.

Only include fields that are clearly visible. Return JSON."""


# ── Field-to-group mapping ─────────────────────────────────────────

def _group_missing(missing_fields: set[str]) -> set[str]:
    """Determine which page groups we need to OCR."""
    groups: set[str] = set()
    if missing_fields & CONTACT_FIELDS:
        groups.add("contact")
    if missing_fields & SITE_FIELDS:
        groups.add("site")
    if missing_fields & DEMOGRAPHIC_FIELDS:
        groups.add("demographic")
    if missing_fields & TIEBREAKER_FIELDS:
        groups.add("tiebreaker")
    return groups


# ── Main recovery logic ────────────────────────────────────────────

def recover_row(
    row: dict[str, str],
    pdf_path: Path,
    missing_fields: set[str],
    model: str = "gpt-4o",
) -> int:
    """Attempt OCR recovery for one row. Returns number of fields filled."""
    groups = _group_missing(missing_fields)
    if not groups:
        return 0

    recovered: dict[str, str] = {}
    images_to_clean: list[Path] = []

    try:
        for group in groups:
            if group == "contact":
                imp = _render_adjacent(pdf_path, _find_contact_page(pdf_path), radius=0)
                if imp:
                    images_to_clean.extend(imp)
                    result = _call_vision_llm(imp, CONTACT_PROMPT, model)
                    if result:
                        for field in CONTACT_FIELDS:
                            if field in missing_fields and result.get(field):
                                val = str(result[field]).strip()
                                if val and val.lower() not in ("n/a", "na", "none", ""):
                                    recovered[field] = val

            elif group == "site":
                imp = _render_adjacent(pdf_path, _find_site_info_page(pdf_path), radius=1)
                if imp:
                    images_to_clean.extend(imp)
                    result = _call_vision_llm(imp, SITE_PROMPT, model)
                    if result:
                        for field in SITE_FIELDS:
                            if field in missing_fields and result.get(field):
                                val = str(result[field]).strip()
                                if val and val.lower() not in ("n/a", "na", "none", ""):
                                    recovered[field] = val

            elif group == "demographic":
                imp = _render_demographic_pages(pdf_path)
                if imp:
                    images_to_clean.extend(imp)
                    result = _call_vision_llm(imp, DEMOGRAPHIC_PROMPT, model)
                    if result:
                        for field in DEMOGRAPHIC_FIELDS:
                            if field in missing_fields and result.get(field):
                                val = str(result[field]).strip()
                                if val and val.lower() not in ("n/a", "na", "none", ""):
                                    recovered[field] = val

            elif group == "tiebreaker":
                imp = _render_tiebreaker_pages(pdf_path)
                if imp:
                    images_to_clean.extend(imp)
                    result = _call_vision_llm(imp, TIEBREAKER_PROMPT, model)
                    if result:
                        for field in TIEBREAKER_FIELDS:
                            if field in missing_fields and result.get(field):
                                val = str(result[field]).strip()
                                if val and val.lower() not in ("n/a", "na", "none", ""):
                                    # Strip non-numeric chars from numeric fields
                                    if field.startswith("distance_") or field == "tiebreaker_score":
                                        val = "".join(c for c in val if c.isdigit() or c == ".")
                                    recovered[field] = val

    finally:
        # Clean up temp images
        for imp in images_to_clean:
            try:
                imp.unlink()
            except Exception:
                pass

    # Apply recovered values
    filled = 0
    for field, val in recovered.items():
        if field in missing_fields and val:
            old = row.get(field, "").strip()
            if old.lower() in ("", "n/a", "na", "none", "0", "0.0") or not old:
                row[field] = val
                filled += 1

    return filled


# ── CLI ────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="OCR-based recovery for missing field values in LIHTC extraction CSV"
    )
    p.add_argument("--in", dest="in_path", required=True, help="Input CSV")
    p.add_argument("--out", dest="out_path", required=True, help="Output CSV")
    p.add_argument(
        "--downloads",
        default="downloads_challenges",
        help="PDF directory (default: downloads_challenges)",
    )
    p.add_argument(
        "--model",
        default="gpt-4o",
        help="Vision model to use (default: gpt-4o)",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Max rows to process (0 = all, for testing)",
    )
    p.add_argument(
        "--min-missing",
        type=int,
        default=1,
        help="Min missing fields to trigger OCR (default: 1)",
    )
    args = p.parse_args()

    # Load CSV
    with open(args.in_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not fieldnames:
        print("ERROR: empty CSV", file=sys.stderr)
        sys.exit(1)

    # Identify fixable missing (excluding property_rate, error which are fundamentally unextractable)
    all_fixable = (
        CONTACT_FIELDS | SITE_FIELDS | DEMOGRAPHIC_FIELDS | TIEBREAKER_FIELDS
    )

    targets: list[tuple[int, dict[str, str], set[str]]] = []
    for i, row in enumerate(rows):
        missing: set[str] = set()
        for f in all_fixable:
            v = (row.get(f, "") or "").strip().lower()
            if v in ("", "n/a", "na", "none"):
                missing.add(f)
            elif v == "0" or v == "0.0":
                # "0" is valid for distances only if confirmed by source (needs_geocode)
                # Otherwise treat as extraction failure
                if f.startswith("distance_"):
                    missing.add(f)
        if len(missing) >= args.min_missing:
            targets.append((i, row, missing))

    print(f"Rows to process: {len(targets)} ({sum(len(t[2]) for t in targets)} missing fields)")

    processed = 0
    total_filled = 0
    for ri, row, missing in targets:
        if args.max_rows and processed >= args.max_rows:
            break

        pdf_name = (row.get("pdf") or "").strip()
        pdf_path = Path(args.downloads) / pdf_name
        if not pdf_path.exists():
            continue

        print(f"\n[{pdf_name}] {len(missing)} missing: {sorted(missing)}")
        filled = recover_row(row, pdf_path, missing, args.model)
        print(f"  → filled {filled} fields")
        total_filled += filled
        processed += 1

    # Write output
    os.makedirs(os.path.dirname(args.out_path) or ".", exist_ok=True)
    with open(args.out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'=' * 60}")
    print(f"Processed {processed} rows, filled {total_filled} fields")
    print(f"Saved: {args.out_path}")


if __name__ == "__main__":
    main()
