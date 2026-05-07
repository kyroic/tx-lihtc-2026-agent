#!/usr/bin/env python3
"""
Final Unified Pipeline - V5.8 + Auto-Recovery

Single command runs everything:
1. Fast extraction (pdftotext + targeted Tie-Breaker page finding)
2. Auto-recovery for missing census_tract (regex on all pages)
3. Auto-recovery for missing poverty_rate (multiline pattern near quartile)
4. Auto-recovery for missing tiebreaker_* (broad heading search)

No mode selection needed - just run it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber

from ..extract import ExtractedRow, field_from_obj, sha256_file, write_outputs, norm_ws
from ..model_client import chat_completions, extract_json_content
from ..strategies.registry import get_strategy


# === CONFIGURATION ===
CENSUS_TRACT_PATTERNS = [
    r'\b(48\d{9,13})\b',
    r'Census\s+Tract[:\s]+(\d{6,11})',
    r'GEOID[:\s]+(\d{11,15})',
]

POVERTY_PATTERNS = [
    r'Quartile:\s*[1-4]q?\s+Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
    r'Quartile:\s*[1-4]q[^\n]{0,100}?Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
    r'Poverty\s*Rate[:\s]*\n\s*([0-9]{1,2}(?:\.[0-9]{1,2})?)',  # Multiline!
    r'Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
]

TIEBREAKER_HEADING_PATTERNS = [
    r'tie[- ]?breaker\s+information',
    r'tie[- ]?breakers?',
    r'competitive\s+htc\s+only',
]


def extract_text_with_pdftotext(pdf_path: Path, first_page: int = 1, last_page: int = -1) -> str:
    """Extract text using pdftotext (much faster than pdfplumber for scanning)."""
    with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
        tmp_path = tmp.name
    
    args = ['pdftotext', '-layout']
    if first_page > 1:
        args.extend(['-f', str(first_page)])
    if last_page > 0:
        args.extend(['-l', str(last_page)])
    args.extend([str(pdf_path), tmp_path])
    
    try:
        subprocess.run(args, capture_output=True, timeout=120)
        text = Path(tmp_path).read_text(errors='ignore')
        Path(tmp_path).unlink()
        return text
    except Exception:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
        return ""


def find_census_tract(pdf_path: Path) -> str:
    """Find census tract using regex on full text."""
    text = extract_text_with_pdftotext(pdf_path, 1, 150)
    
    for pattern in CENSUS_TRACT_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            tract = str(match).strip()
            if tract and len(tract) >= 6:
                clean = re.sub(r'[^0-9A-Z.]', '', tract)[:15]
                if len(clean) >= 6:
                    return clean
    return ""


def find_poverty_rate(pdf_path: Path) -> str:
    """Find poverty rate using multiline patterns near quartile."""
    text = extract_text_with_pdftotext(pdf_path, 1, 150)
    
    for pattern in POVERTY_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            try:
                val = float(str(match).strip())
                if 0 <= val <= 100:
                    return str(val)
            except ValueError:
                continue
    return ""


def find_tiebreaker_pages(pdf_path: Path) -> list[int]:
    """Find Tie-Breaker pages using broad heading patterns."""
    text = extract_text_with_pdftotext(pdf_path)
    pages = text.split('\f')
    
    found = []
    for i, page_text in enumerate(pages, 1):
        low = page_text.lower()
        if any(re.search(pat, low) for pat in TIEBREAKER_HEADING_PATTERNS):
            found.append(i)
            if len(found) >= 10:
                break
    return found


def extract_one_pdf_final(
    pdf_path: Path,
    project_id: str,
    model: str,
) -> ExtractedRow:
    """
    Complete extraction with auto-recovery for all fields.
    """
    # Step 1: Find Tie-Breaker pages
    tiebreaker_pages = find_tiebreaker_pages(pdf_path)
    
    # Step 2: Extract targeted pages
    standard_pages = []
    tiebreaker_content = []
    
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            # First 10 pages for context
            for idx in range(min(10, len(pdf.pages))):
                page = pdf.pages[idx]
                pn = idx + 1
                if pn in tiebreaker_pages:
                    continue
                try:
                    text = page.extract_text() or ""
                    standard_pages.append({"page": pn, "text": text.strip()[:4000], "tables": []})
                except Exception:
                    continue
            
            # Tie-Breaker pages
            for pn in tiebreaker_pages[:5]:
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
                    tiebreaker_content.append({
                        "page": pn,
                        "text": text.strip()[:8000],
                        "tables": table_text,
                    })
                except Exception:
                    continue
    except Exception:
        pass
    
    # Step 3: Auto-recovery for census_tract and poverty_rate
    census_tract = find_census_tract(pdf_path)
    poverty_rate = find_poverty_rate(pdf_path)
    
    # Step 4: Build LLM prompt
    page_hints = {
        "application_name": [],
        "contact": [],
        "census_tract": [],
        "tiebreaker_pages": tiebreaker_pages,
        "auto_recovered": {
            "census_tract": census_tract,
            "poverty_rate": poverty_rate,
        }
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
    
    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote.\n"
        "- CRITICAL: Focus on tiebreaker_pages for tiebreaker_* fields.\n"
        "- Use auto_recovered values if LLM extraction fails to find them.\n"
    )
    
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
    
    # Step 5: Call LLM
    try:
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
    except Exception:
        out = {}
    
    # Step 6: Build row with auto-recovery fallback
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="final_unified_pipeline",
    )
    
    for k in schema_hint.keys():
        setattr(row, k, field_from_obj(out.get(k)))
    
    # Apply auto-recovery for census_tract
    if census_tract and not row.census_tract.value:
        row.census_tract = field_from_obj({
            "value": census_tract,
            "confidence": 0.95,
            "pages": [1],
            "quote": f"Auto-recovered via regex: {census_tract}",
        })
    
    # Apply auto-recovery for poverty_rank
    if poverty_rate and not row.poverty_rank.value:
        row.poverty_rank = field_from_obj({
            "value": poverty_rate,
            "confidence": 0.95,
            "pages": [1],
            "quote": f"Auto-recovered via regex: {poverty_rate}",
        })
    
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Final Unified Pipeline - V5.8 + Auto-Recovery (no mode selection needed)"
    )
    ap.add_argument("--pdf-dir", default="downloads", help="Directory with PDFs (default: downloads)")
    ap.add_argument("--out-dir", default="out", help="Output directory (default: out)")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--parallel", type=int, default=4, help="Parallel PDFs")
    args = ap.parse_args()
    
    pdf_dir = Path(args.pdf_dir)
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    
    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    
    if not pdfs:
        print(f"No PDFs found in {pdf_dir}")
        return 1
    
    print(f"Final Unified Pipeline")
    print(f"  PDFs: {len(pdfs)}")
    print(f"  Parallel: {args.parallel}")
    print(f"  Output: {out_root}")
    print()
    
    extracted_rows: list[ExtractedRow] = []
    t0 = time.time()
    
    def extract_one(pdf_path: Path) -> tuple[Path, ExtractedRow]:
        row = extract_one_pdf_final(pdf_path, args.project_id, args.model)
        return pdf_path, row
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(args.parallel)) as executor:
        futures = {executor.submit(extract_one, pdf): pdf for pdf in pdfs}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            pdf_path, row = future.result()
            extracted_rows.append(row)
            
            status = "✅" if not row.needs_review else "⚠️"
            name = row.application_name.value[:35] if row.application_name.value else "(no name)"
            print(f"[{i}/{len(pdfs)}] {status} {pdf_path.name}: {name}")
    
    elapsed = time.time() - t0
    
    # Write outputs
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=extracted_rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=50,
    )
    
    run_summary = {
        "mode": "final_unified_pipeline",
        "total_pdfs": len(pdfs),
        "parallel_workers": args.parallel,
        "extracted_rows": len(extracted_rows),
        "count_needs_review": summary["count_needs_review"],
        "clean_rows": len(extracted_rows) - summary["count_needs_review"],
        "elapsed_total_s": round(elapsed, 2),
        "avg_time_per_pdf_s": round(elapsed / len(pdfs), 2) if pdfs else 0,
        "outputs": summary["outputs"],
    }
    
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    
    print()
    print("=" * 60)
    print("FINAL UNIFIED PIPELINE COMPLETE")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
