#!/usr/bin/env python3
"""
V5.9 Recovery Pass - Targeted scan for missing fields

Takes PDFs that failed extraction (e.g., missing census_tract) and re-processes
them with enhanced, field-specific scanning.

Key optimizations for recovery:
1. Only process PDFs that need recovery (not all 114)
2. Use multiple search patterns per missing field
3. Extended page range search (not just first 50 pages)
4. Regex patterns for specific field formats (e.g., census tract patterns)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber

from ..extract import ExtractedRow, field_from_obj, sha256_file, norm_ws, write_outputs
from ..model_client import chat_completions, extract_json_content
from ..strategies.registry import get_strategy


# Census tract patterns (Texas format)
CENSUS_TRACT_PATTERNS = [
    r'\b(\d{11,15})\b',  # Full GEOID (e.g., 48201241001)
    r'\b(\d{2}[A-Z]*\d{6,10})\b',  # With suffix
    r'Census\s*Tract[:\s]+([A-Z0-9\.]+)',
    r'Tract[:\s]+([A-Z0-9\.]+)',
    r'GEOID[:\s]+([0-9A-Z]+)',
]


def find_census_tract_by_regex(pdf_path: Path) -> list[tuple[int, str]]:
    """
    Scan ALL pages for census tract patterns using regex.
    Returns list of (page_number, matched_text).
    """
    found = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                try:
                    text = page.extract_text() or ""
                    for pattern in CENSUS_TRACT_PATTERNS:
                        matches = re.findall(pattern, text, re.IGNORECASE)
                        for match in matches:
                            if match and len(str(match).strip()) > 5:
                                found.append((idx, str(match).strip()))
                                break  # One per page is enough
                except Exception:
                    continue
    except Exception:
        pass
    
    return found


def extract_with_enhanced_context(
    pdf_path: Path,
    missing_fields: list[str],
    max_pages: int = 100,  # Extended range
) -> dict[str, Any]:
    """
    Extract with extended page range and field-specific hints.
    """
    # Scan for census tract specifically
    census_matches = find_census_tract_by_regex(pdf_path)
    
    # Extract pages with enhanced context
    standard_pages = []
    census_pages = []
    
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            # First N pages
            for idx in range(min(max_pages, len(pdf.pages))):
                page = pdf.pages[idx]
                pn = idx + 1
                try:
                    text = page.extract_text() or ""
                    # Check if this page has census tract
                    has_census = any(pn == cm[0] for cm in census_matches)
                    
                    standard_pages.append({
                        "page": pn,
                        "text": text.strip()[:4000],
                        "has_census_tract": has_census,
                        "census_matches_on_page": [cm[1] for cm in census_matches if cm[0] == pn],
                    })
                    
                    if has_census:
                        census_pages.append({
                            "page": pn,
                            "full_text": text.strip()[:8000],
                            "matches": [cm[1] for cm in census_matches if cm[0] == pn],
                        })
                except Exception:
                    continue
    except Exception:
        pass
    
    return {
        "census_tract_matches": census_matches,
        "standard_pages": standard_pages,
        "census_pages": census_pages,
    }


def recover_one_pdf(
    *,
    pdf_path: Path,
    missing_fields: list[str],
    project_id: str,
    model: str,
) -> ExtractedRow:
    """Re-extract a single PDF with enhanced recovery logic."""
    
    # Enhanced extraction
    enhanced = extract_with_enhanced_context(pdf_path, missing_fields)
    
    # Build targeted prompt
    system = (
        "You are recovering MISSING fields from Texas LIHTC 2026 Full Application PDFs.\n"
        "Focus ONLY on the missing fields listed below.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Search ALL provided pages, not just first few.\n"
        "- For census_tract: Look for 11-15 digit GEOID, or 'Census Tract' labels.\n"
        "- For property_rate: Look for dollar amounts, percentages, or rate tables.\n"
        "- Every non-empty value MUST include pages[] and a short quote.\n"
        "- If truly not found after thorough search, return value=\"\" and confidence=0.\n"
    )
    
    user_data = {
        "pdf_filename": pdf_path.name,
        "missing_fields_to_recover": missing_fields,
        "census_tract_regex_matches": enhanced["census_tract_matches"],
        "pages_with_census_tract": enhanced["census_pages"],
        "extended_page_samples": enhanced["standard_pages"][:50],  # First 50 pages
    }
    
    schema_hint = {}
    for field in missing_fields:
        schema_hint[field] = {"value": "", "confidence": 0.0, "pages": [], "quote": ""}
    
    user_data["output_schema_for_missing_fields"] = schema_hint
    
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
    
    # Build row with only recovered fields
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.9_recovery_pass",
    )
    
    # Fill in recovered fields
    for field in missing_fields:
        recovered = out.get(field, {})
        if isinstance(recovered, dict):
            val = recovered.get("value", "")
            pages = recovered.get("pages", [])
            quote = recovered.get("quote", "")
            confidence = recovered.get("confidence", 0.0)
            
            field_obj = type('FieldEvidence', (), {
                "value": val or "",
                "confidence": confidence or 0.0,
                "pages": pages or [],
                "quote": quote or "",
            })()
            setattr(row, field, field_obj)
    
    # Validation - check if we recovered anything
    for field in missing_fields:
        f = getattr(row, field, None)
        if f and f.value and (not f.pages or not f.quote.strip()):
            row.review_reasons.append(f"missing_evidence:{field}")
        elif not f or not getattr(f, 'value', ''):
            row.review_reasons.append(f"still_missing:{field}")
    
    row.needs_review = bool(row.review_reasons)
    return row


def load_review_queue(csv_path: Path) -> list[dict]:
    """Load PDFs that need recovery from review_queue.csv or applications.csv."""
    needs_review = []
    
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get('needs_review') or '').lower() == 'true':
                reasons = (row.get('review_reasons') or '').split(';')
                missing = [r.replace('missing:', '').strip() for r in reasons if r.startswith('missing:')]
                if missing:
                    needs_review.append({
                        "pdf_path": row.get('source_pdf_path', ''),
                        "missing_fields": missing,
                    })
    
    return needs_review


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.9: Recovery pass for missing fields")
    ap.add_argument("--input-csv", required=True, help="applications.csv or review_queue.csv")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--pdf-base-dir", default=".", help="Base directory for PDF paths")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--parallel", type=int, default=2)
    args = ap.parse_args()
    
    # Load PDFs needing recovery
    review_items = load_review_queue(Path(args.input_csv))
    
    if not review_items:
        print("No PDFs need recovery!")
        return 0
    
    print(f"Recovery pass: {len(review_items)} PDFs need field recovery")
    print(f"Missing fields breakdown:")
    from collections import Counter
    field_counts = Counter()
    for item in review_items:
        for f in item["missing_fields"]:
            field_counts[f] += 1
    for field, count in field_counts.most_common():
        print(f"  {field}: {count} PDFs")
    print()
    
    # Process recoveries
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    
    recovered_rows: list[ExtractedRow] = []
    t0 = time.time()
    
    import concurrent.futures
    
    def recover_one(item: dict) -> ExtractedRow | None:
        pdf_path = Path(item["pdf_path"])
        if not pdf_path.is_absolute():
            pdf_path = Path(args.pdf_base_dir) / pdf_path
        if not pdf_path.exists():
            print(f"  PDF not found: {pdf_path}")
            return None
        
        print(f"  Recovering {pdf_path.name}... missing: {item['missing_fields']}")
        try:
            row = recover_one_pdf(
                pdf_path=pdf_path,
                missing_fields=item["missing_fields"],
                project_id=args.project_id,
                model=args.model,
            )
            return row
        except Exception as e:
            print(f"  ERROR: {e}")
            return None
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(args.parallel)) as executor:
        futures = {executor.submit(recover_one, item): item for item in review_items}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result()
            if row:
                recovered_rows.append(row)
                status = "✅" if not row.needs_review else "⚠️"
                print(f"[{i}/{len(review_items)}] {status} {row.source_pdf_path.split('/')[-1]}")
    
    elapsed = time.time() - t0
    
    # Write outputs
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=recovered_rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=100,
    )
    
    run_summary = {
        "mode": "v5.9_recovery_pass",
        "input_csv": str(args.input_csv),
        "pdfs_needing_recovery": len(review_items),
        "recovered_rows": len(recovered_rows),
        "still_needs_review": sum(1 for r in recovered_rows if r.needs_review),
        "fully_recovered": sum(1 for r in recovered_rows if not r.needs_review),
        "elapsed_s": round(elapsed, 2),
        "outputs": summary.get("outputs", {}),
    }
    
    (out_root / "recovery_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    
    print()
    print("=" * 60)
    print("V5.9 RECOVERY COMPLETE")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
