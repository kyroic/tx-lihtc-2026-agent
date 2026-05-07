#!/usr/bin/env python3
"""
V5.9b - Fast Census Tract Recovery using pdftotext + regex

The census tract GEOIDs ARE in the PDFs - we just need to extract them directly
using regex on the full text, without relying on LLM extraction.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..extract import ExtractedRow, field_from_obj, sha256_file, write_outputs


# Texas GEOID pattern: 2-digit state + 3-digit county + 6-digit tract = 11 digits
# Sometimes with suffix = up to 15 chars
CENSUS_TRACT_PATTERNS = [
    r'\b(48\d{9,13})\b',  # Texas GEOID (48 = TX)
    r'Census\s+Tract[:\s]+(\d{6,11})',
    r'GEOID[:\s]+(\d{11,15})',
    r'Tract[:\s]+(\d{6,11})',
]


def extract_census_tract_by_regex(pdf_path: Path) -> list[dict[str, Any]]:
    """
    Extract census tract using pdftotext + regex.
    Returns list of candidates with page context.
    """
    candidates = []
    
    try:
        # Use pdftotext with page breaks
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
            tmp_path = tmp.name
        
        subprocess.run(
            ['pdftotext', '-layout', str(pdf_path), tmp_path],
            capture_output=True,
            timeout=120,
        )
        
        text = Path(tmp_path).read_text(errors='ignore')
        Path(tmp_path).unlink()
        
        # Split by page markers
        pages = text.split('\f')
        
        for page_idx, page_text in enumerate(pages, 1):
            for pattern in CENSUS_TRACT_PATTERNS:
                matches = re.findall(pattern, page_text, re.IGNORECASE)
                for match in matches:
                    tract = str(match).strip()
                    # Validate: should be mostly digits, 6-15 chars
                    if tract and len(tract) >= 6 and len(tract) <= 15:
                        # Clean up non-digit suffixes
                        clean_tract = re.sub(r'[^0-9A-Z.]', '', tract)
                        if len(clean_tract) >= 6:
                            candidates.append({
                                "page": page_idx,
                                "value": clean_tract[:15],  # Cap at 15 chars
                                "pattern": pattern[:40],
                                "context": page_text[max(0, page_text.find(tract)-50):page_text.find(tract)+50].strip()[:100],
                            })
        
        # Dedupe by value, keep first occurrence
        seen = set()
        unique = []
        for c in candidates:
            if c["value"] not in seen:
                seen.add(c["value"])
                unique.append(c)
        
        return unique[:5]  # Top 5 candidates
        
    except Exception as e:
        return [{"error": str(e)}]


def recover_census_tract_for_pdf(
    pdf_path: Path,
    project_id: str = "lihtc-tx-2026",
) -> ExtractedRow:
    """Recover census_tract field using regex extraction."""
    
    candidates = extract_census_tract_by_regex(pdf_path)
    
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.9b_census_regex_recovery",
    )
    
    # If we found candidates, use the first one
    if candidates and "error" not in candidates[0]:
        best = candidates[0]
        
        # Create field evidence object
        field_obj = type('FieldEvidence', (), {
            "value": best["value"],
            "confidence": 0.85,  # High confidence from regex
            "pages": [best["page"]],
            "quote": best["context"][:100],
        })()
        
        row.census_tract = field_obj
        row.needs_review = False
    else:
        # Still missing
        row.census_tract = type('FieldEvidence', (), {
            "value": "",
            "confidence": 0.0,
            "pages": [],
            "quote": "",
        })()
        row.review_reasons.append("still_missing:census_tract")
        row.needs_review = True
    
    return row


def load_pdfs_needing_census_tract(csv_path: Path) -> list[str]:
    """Load PDF paths that need census_tract recovery."""
    needs_recovery = []
    
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get('needs_review') or '').lower() == 'true':
                reasons = row.get('review_reasons', '').split(';')
                if any('census_tract' in r for r in reasons):
                    pdf_path = row.get('source_pdf_path', '')
                    if pdf_path:
                        needs_recovery.append(pdf_path)
    
    return needs_recovery


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.9b: Fast census tract recovery via regex")
    ap.add_argument("--input-csv", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--pdf-base-dir", default=".")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    args = ap.parse_args()
    
    # Load PDFs needing recovery
    pdf_paths = load_pdfs_needing_census_tract(Path(args.input_csv))
    
    if not pdf_paths:
        print("No PDFs need census tract recovery!")
        return 0
    
    print(f"Recovering census_tract for {len(pdf_paths)} PDFs...")
    
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    
    recovered_rows = []
    t0 = time.time()
    
    for i, pdf_path_str in enumerate(pdf_paths, 1):
        pdf_path = Path(pdf_path_str)
        if not pdf_path.is_absolute():
            pdf_path = Path(args.pdf_base_dir) / pdf_path
        
        if not pdf_path.exists():
            print(f"[{i}/{len(pdf_paths)}] ❌ Not found: {pdf_path}")
            continue
        
        print(f"[{i}/{len(pdf_paths)}] Scanning {pdf_path.name}...", end=" ", flush=True)
        
        try:
            row = recover_census_tract_for_pdf(pdf_path, args.project_id)
            recovered_rows.append(row)
            
            if row.census_tract.value:
                print(f"✅ {row.census_tract.value} (page {row.census_tract.pages[0]})")
            else:
                print(f"❌ Still missing")
        except Exception as e:
            print(f"❌ ERROR: {e}")
    
    elapsed = time.time() - t0
    
    # Write outputs
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=recovered_rows,
        project_id=args.project_id,
        model="regex_recovery",
        max_pages=100,
    )
    
    run_summary = {
        "mode": "v5.9b_census_regex_recovery",
        "input_csv": str(args.input_csv),
        "pdfs_scanned": len(pdf_paths),
        "recovered_rows": len(recovered_rows),
        "successfully_recovered": sum(1 for r in recovered_rows if r.census_tract.value),
        "still_missing": sum(1 for r in recovered_rows if not r.census_tract.value),
        "elapsed_s": round(elapsed, 2),
        "avg_time_per_pdf": round(elapsed / len(pdf_paths), 2) if pdf_paths else 0,
        "outputs": summary.get("outputs", {}),
    }
    
    (out_root / "census_recovery_summary.json").write_text(
        json.dumps(run_summary, indent=2), encoding="utf-8"
    )
    
    print()
    print("=" * 60)
    print("V5.9b CENSUS TRACT RECOVERY COMPLETE")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
