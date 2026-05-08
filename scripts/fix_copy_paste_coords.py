#!/usr/bin/env python3
"""
Post-processing handler: fix rows where the LLM copied site coordinates
into amenity coordinate fields.

Detection (via cleanliness.py):
  - coords_copy_paste:<amenity>:  amenity coords == site coords but distance > 0
    → LLM error, fixable by re-reading the PDF's coordinate table.
  - needs_geocode:<amenity>:      amenity coords == site coords and distance == 0
    → PDF source data limitation (same parcel boundary). Not fixable here;
      requires external geocoder (Google Maps API).

For copy-paste rows, we re-parse the PDF's tiebreaker coordinate table using
pdfplumber and overwrite the bad values with the correctly-parsed amenity coords.

Usage:
  python scripts/fix_copy_paste_coords.py --in out_verify/applications.csv --out out_verify/applications_fixed.csv
"""

import argparse
import csv
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lihtc_tx_2026_agent.cleanliness import zero_distance_coords_check
from lihtc_tx_2026_agent.strategies.v5_8_fast_tiebreaker import (
    _build_coordinate_summary,
    _build_distance_summary,
    _parse_inline_coordinate_grid,
    find_tiebreaker_pages_fast,
)


def _parse_coord_value(v: str) -> str:
    """Strip pdfplumber artifacts from a coordinate string."""
    return re.sub(r'[\n|].*$', '', (v or '').strip())


def _extract_coord_map_from_pdf(pdf_path: Path) -> dict[str, tuple[str, str]]:
    """
    Parse the tiebreaker coordinate table and return a map:
      {amenity: (lat_str, lng_str)}
    e.g. {"park": ("27.747818", "-97.43366"), ...}
    Tries pdfplumber tables first, falls back to pdftotext inline grid.
    """
    coord_map: dict[str, tuple[str, str]] = {}

    tb_pages = find_tiebreaker_pages_fast(pdf_path)
    if not tb_pages:
        return coord_map

    # Approach 1: pdfplumber tables
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            all_tb_tables = []
            all_tb_text = ""
            for pn in tb_pages:
                if pn < 1 or pn > len(pdf.pages):
                    continue
                page = pdf.pages[pn - 1]
                text = page.extract_text() or ""
                tables = page.extract_tables() or []
                all_tb_tables.extend(tables)
                all_tb_text += text + "\n"

            coord_map = _parse_coord_summary_lines(
                _build_coordinate_summary(all_tb_tables, all_tb_text)
            )
            if coord_map:
                return coord_map
    except Exception:
        pass

    # Approach 2: pdftotext inline coordinate grid (for PDFs with no tables)
    try:
        import subprocess, tempfile

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), tmp_path],
            capture_output=True, timeout=60,
        )
        full_text = Path(tmp_path).read_text(errors="ignore")
        Path(tmp_path).unlink()

        # Extract pages around tiebreaker area
        pages = full_text.split("\f")
        # Expand window: all TB pages + 3 pages before/after
        tb_text = ""
        expanded_pns = set(tb_pages)
        for pn in tb_pages:
            for offset in range(-3, 4):
                expanded_pns.add(pn + offset)
        for pn in sorted(expanded_pns):
            idx = pn - 1
            if 0 <= idx < len(pages):
                tb_text += pages[idx] + "\n"

        return _parse_inline_coordinate_grid(tb_text)
    except Exception:
        pass

    return coord_map


def _parse_coord_summary_lines(coords_text: str) -> dict[str, tuple[str, str]]:
    """
    Parse the _build_coordinate_summary output lines into a coord map.
    Lines format:
      amenity_label: amenity_lat,amenity_lng = (val, val) | site_lat,site_lng = (val, val)
    """
    coord_map: dict[str, tuple[str, str]] = {}
    for line in coords_text.split("\n"):
        m = re.match(
            r"^\s*(\S[\s\S]*?):\s*amenity_lat,amenity_lng\s*=\s*\(([-\d., ]+)\)",
            line,
        )
        if m:
            name = m.group(1).strip().lower()
            vals = m.group(2).strip()
            parts = [v.strip() for v in vals.split(",")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                coord_map[name] = (parts[0], parts[1])

    # Normalize keys: "public library" -> "library", "grocery store" -> "grocery"
    normalized: dict[str, tuple[str, str]] = {}
    for k, v in coord_map.items():
        if "library" in k:
            normalized.setdefault("library", v)
        elif "grocery" in k or "store" in k:
            normalized.setdefault("grocery", v)
        elif "school" in k:
            normalized.setdefault("school", v)
        elif "park" in k:
            normalized.setdefault("park", v)
        else:
            normalized[k] = v
    return normalized


def fix_copy_paste_rows(
    rows: list[dict[str, str]],
    downloads_dir: str = "downloads_challenges",
) -> tuple[list[dict[str, str]], int]:
    """
    Fix copy-paste coordinates in rows by re-parsing PDFs.

    Returns (fixed_rows, num_fixed).
    """
    fixed_count = 0

    for ri, row in enumerate(rows):
        issues = zero_distance_coords_check(row)
        copy_amenities = [
            iss.split(":")[1]
            for iss in issues
            if iss.startswith("coords_copy_paste:")
        ]
        if not copy_amenities:
            continue

        pdf_name = (row.get("pdf") or row.get("source_pdf") or "").strip()
        pdf_path = Path(downloads_dir) / pdf_name
        if not pdf_path.exists():
            continue

        coord_map = _extract_coord_map_from_pdf(pdf_path)
        if not coord_map:
            continue

        row_fixed = False
        for amenity in copy_amenities:
            coords = coord_map.get(amenity)
            if coords:
                alat_new, alng_new = coords
                alat_old = (row.get(f"{amenity}_lat") or "").strip()
                alng_old = (row.get(f"{amenity}_lng") or "").strip()
                if alat_new != alat_old or alng_new != alng_old:
                    row[f"{amenity}_lat"] = alat_new
                    row[f"{amenity}_lng"] = alng_new
                    row_fixed = True

        if row_fixed:
            fixed_count += 1

    return rows, fixed_count


def main() -> None:
    p = argparse.ArgumentParser(
        description="Fix copy-paste amenity coordinates by re-parsing PDFs"
    )
    p.add_argument("--in", dest="in_path", required=True, help="Input CSV")
    p.add_argument("--out", dest="out_path", required=True, help="Output CSV (fixed)")
    p.add_argument(
        "--downloads",
        default="downloads_challenges",
        help="Directory containing PDFs (default: downloads_challenges)",
    )
    args = p.parse_args()

    # Load rows
    with open(args.in_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not fieldnames:
        print("ERROR: empty CSV", file=sys.stderr)
        sys.exit(1)

    # Detect copy-paste issues
    copy_paste_count = 0
    for row in rows:
        for iss in zero_distance_coords_check(row):
            if "coords_copy_paste" in iss:
                copy_paste_count += 1

    print(f"Detected {copy_paste_count} copy-paste coordinate fields")

    # Fix
    rows, fixed = fix_copy_paste_rows(rows, args.downloads)

    print(f"Fixed {fixed} row(s)")

    # Write output
    os.makedirs(os.path.dirname(args.out_path) or ".", exist_ok=True)
    with open(args.out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved: {args.out_path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
