#!/usr/bin/env python3
"""
V5.9c - Poverty Rate Recovery

Since poverty rate appears RIGHT NEXT TO quartile in the Census Tract section,
we can extract it with a simple targeted regex on pages where quartile was found.
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

from ..extract import ExtractedRow, field_from_obj, sha256_file, write_outputs


# Pattern: "Quartile: Xq" followed by "Poverty Rate: XX.XX" on same or next line
POVERTY_PATTERNS = [
    r'Quartile:\s*[1-4]q?\s+Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
    r'Quartile:\s*[1-4]q[^\n]{0,100}?Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
    r'Quartile[:\s]+[1-4]q?\s*.*?([0-9]{1,2}\.[0-9]{1,2})\s*(?:Poverty|%)',
]


def extract_poverty_rate_near_quartile(pdf_path: Path) -> list[dict]:
    """
    Extract poverty rate from pages where quartile appears.
    Returns list of candidates with page context.
    """
    candidates = []
    
    try:
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
            tmp_path = tmp.name
        
        # Extract first 150 pages (where Census Tract info typically is)
        subprocess.run(
            ['pdftotext', '-f', '1', '-l', '150', '-layout', str(pdf_path), tmp_path],
            capture_output=True,
            timeout=120,
        )
        
        text = Path(tmp_path).read_text(errors='ignore')
        Path(tmp_path).unlink()
        
        pages = text.split('\f')
        
        for page_idx, page_text in enumerate(pages, 1):
            for pattern in POVERTY_PATTERNS:
                matches = re.findall(pattern, page_text, re.IGNORECASE)
                for match in matches:
                    value = str(match).strip()
                    # Validate: should be a percentage-like number (0-100)
                    try:
                        num = float(value)
                        if 0 <= num <= 100:
                            candidates.append({
                                "page": page_idx,
                                "value": value,
                                "pattern": pattern[:50],
                            })
                    except ValueError:
                        continue
        
        # Dedupe, keep first occurrence
        seen = set()
        unique = []
        for c in candidates:
            if c["value"] not in seen:
                seen.add(c["value"])
                unique.append(c)
        
        return unique[:3]  # Top 3 candidates
        
    except Exception as e:
        return [{"error": str(e)}]


def recover_poverty_rate_for_pdf(
    pdf_path: Path,
    project_id: str = "lihtc-tx-2026",
) -> ExtractedRow:
    """Recover poverty_rate field using targeted regex near quartile."""
    
    candidates = extract_poverty_rate_near_quartile(pdf_path)
    
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.9c_poverty_rate_recovery",
    )
    
    # If we found candidates, use the first one
    if candidates and "error" not in candidates[0]:
        best = candidates[0]
        
        field_obj = type('FieldEvidence', (), {
            "value": best["value"],
            "confidence": 0.9,  # High confidence from structured location
            "pages": [best["page"]],
            "quote": f"Poverty Rate: {best['value']} (near Quartile)",
        })()
        
        row.poverty_rank = field_obj  # Map to poverty_rank field
        row.needs_review = False
    else:
        # Still missing
        field_obj = type('FieldEvidence', (), {
            "value": "",
            "confidence": 0.0,
            "pages": [],
            "quote": "",
        })()
        row.poverty_rank = field_obj
        row.review_reasons.append("still_missing:poverty_rank")
        row.needs_review = True
    
    return row


def load_all_pdfs(csv_path: Path) -> list[str]:
    """Load all PDF paths from applications.csv."""
    pdf_paths = []
    
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pdf_path = row.get('source_pdf_path', '')
            if pdf_path:
                pdf_paths.append(pdf_path)
    
    return pdf_paths


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.9c: Poverty rate recovery (near quartile)")
    ap.add_argument("--input-csv", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--pdf-base-dir", default=".")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    args = ap.parse_args()
    
    # Load all PDFs
    pdf_paths = load_all_pdfs(Path(args.input_csv))
    
    if not pdf_paths:
        print("No PDFs found!")
        return 0
    
    print(f"Recovering poverty_rate for {len(pdf_paths)} PDFs...")
    
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    
    recovered_rows = []
    t0 = time.time()
    
    for i, pdf_path_str in enumerate(pdf_paths, 1):
        pdf_path = Path(pdf_path_str)
        # Handle double-path issue from CSV
        if 'out_v5_6_full/downloads/out_v5_6_full' in str(pdf_path):
            pdf_path = Path(str(pdf_path).replace('out_v5_6_full/downloads/out_v5_6_full', 'out_v5_6_full/downloads'))
        if not pdf_path.is_absolute():
            pdf_path = Path(args.pdf_base_dir) / pdf_path
        
        if not pdf_path.exists():
            print(f"[{i}/{len(pdf_paths)}] ❌ Not found: {pdf_path}")
            continue
        
        print(f"[{i}/{len(pdf_paths)}] Scanning {pdf_path.name}...", end=" ", flush=True)
        
        try:
            row = recover_poverty_rate_for_pdf(pdf_path, args.project_id)
            recovered_rows.append(row)
            
            if row.poverty_rank.value:
                print(f"✅ {row.poverty_rank.value} (page {row.poverty_rank.pages[0]})")
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
        max_pages=150,
    )
    
    run_summary = {
        "mode": "v5.9c_poverty_rate_recovery",
        "input_csv": str(args.input_csv),
        "pdfs_scanned": len(pdf_paths),
        "recovered_rows": len(recovered_rows),
        "successfully_recovered": sum(1 for r in recovered_rows if r.poverty_rank.value),
        "still_missing": sum(1 for r in recovered_rows if not r.poverty_rank.value),
        "elapsed_s": round(elapsed, 2),
        "avg_time_per_pdf": round(elapsed / len(pdf_paths), 2) if pdf_paths else 0,
        "outputs": summary.get("outputs", {}),
    }
    
    (out_root / "poverty_recovery_summary.json").write_text(
        json.dumps(run_summary, indent=2), encoding="utf-8"
    )
    
    print()
    print("=" * 60)
    print("V5.9c POVERTY RATE RECOVERY COMPLETE")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
