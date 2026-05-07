#!/usr/bin/env python3
"""
Complete End-to-End Pipeline

Single command that does everything:
1. Discovers PDFs from TDHCA website
2. Downloads all PDFs
3. Extracts data with auto-recovery (census_tract, poverty_rate, tiebreaker_*)
4. Outputs final CSV/Excel/JSONL

No parameters needed for default run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from pathlib import Path

from ..discover import discover_pdfs
from ..download import download_pdfs
from ..extract import sha256_file, write_outputs, ExtractedRow
from ..model_client import chat_completions, extract_json_content
from ..strategies.registry import get_strategy

import pdfplumber
import re
import subprocess
import tempfile


# === AUTO-RECOVERY PATTERNS ===
CENSUS_TRACT_PATTERNS = [
    r'\b(48\d{9,13})\b',
    r'Census\s+Tract[:\s]+(\d{6,11})',
]

POVERTY_PATTERNS = [
    r'Quartile:\s*[1-4]q?\s+Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
    r'Poverty\s*Rate[:\s]*\n\s*([0-9]{1,2}(?:\.[0-9]{1,2})?)',
]

TIEBREAKER_HEADING_PATTERNS = [
    r'tie[- ]?breaker\s+information',
    r'tie[- ]?breakers?',
]


def extract_text_with_pdftotext(pdf_path: Path, first_page: int = 1, last_page: int = -1) -> str:
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
    text = extract_text_with_pdftotext(pdf_path, 1, 150)
    for pattern in CENSUS_TRACT_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            tract = str(match).strip()
            if tract and len(tract) >= 6:
                return re.sub(r'[^0-9A-Z.]', '', tract)[:15]
    return ""


def find_poverty_rate(pdf_path: Path) -> str:
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


def extract_one_pdf_final(pdf_path: Path, project_id: str, model: str) -> ExtractedRow:
    """Extract one PDF with auto-recovery."""
    from ..extract import field_from_obj, norm_ws
    
    tiebreaker_pages = find_tiebreaker_pages(pdf_path)
    
    # Auto-recovery
    census_tract = find_census_tract(pdf_path)
    poverty_rate = find_poverty_rate(pdf_path)
    
    # Build prompt
    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote.\n"
        "- CRITICAL: Focus on tiebreaker_pages for tiebreaker_* fields.\n"
    )
    
    schema = {
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
        "tiebreaker_pages": tiebreaker_pages,
        "auto_recovered": {
            "census_tract": census_tract,
            "poverty_rate": poverty_rate,
        },
        "output_schema": schema,
    }
    
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
    
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="complete_pipeline",
    )
    
    for k in schema.keys():
        setattr(row, k, field_from_obj(out.get(k)))
    
    # Apply auto-recovery
    if census_tract and not row.census_tract.value:
        row.census_tract = field_from_obj({
            "value": census_tract,
            "confidence": 0.95,
            "pages": [1],
            "quote": f"Auto-recovered: {census_tract}",
        })
    
    if poverty_rate and not row.poverty_rank.value:
        row.poverty_rank = field_from_obj({
            "value": poverty_rate,
            "confidence": 0.95,
            "pages": [1],
            "quote": f"Auto-recovered: {poverty_rate}",
        })
    
    # Validation
    if not row.application_name.value:
        row.review_reasons.append("missing:application_name")
    if not row.contact_email.value:
        row.review_reasons.append("missing:contact_email")
    if not row.census_tract.value:
        row.review_reasons.append("missing:census_tract")
    
    row.needs_review = bool(row.review_reasons)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Complete End-to-End Pipeline (discover → download → extract)"
    )
    ap.add_argument("--pdf-dir", default="out_v5_6_full/downloads", help="PDF directory")
    ap.add_argument("--out-dir", default="out_complete", help="Output directory")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--skip-discover", action="store_true", help="Skip discovery if PDFs exist")
    args = ap.parse_args()
    
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    
    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    
    # Step 1: Discover (if needed)
    if not args.skip_discover or not pdf_dir.exists() or len(list(pdf_dir.glob("*.pdf"))) == 0:
        print("🔍 Step 1: Discovering PDFs from TDHCA...")
        disc = discover_pdfs(
            seed_urls=["https://www.tdhca.texas.gov/competitive-9-housing-tax-credits"],
            max_pages=550,
            state_path=out_root / "discover_state.json",
            include_pdf_regex="2026",
            fetch_timeout_s=15,
        )
        print(f"  Found {len(disc.pdf_urls)} PDF URLs")
        pdf_dir = out_root / "downloads"
    
    # Step 2: Download
    print(f"\n📥 Step 2: Downloading PDFs to {pdf_dir}...")
    pdfs = list(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []
    if len(pdfs) < 100:
        dl = download_pdfs(
            pdf_urls=[],  # Will load from discover_state
            out_dir=pdf_dir,
            manifest_path=out_root / "download_manifest.json",
            max_new_downloads=200,
            parallel_downloads=10,
        )
        print(f"  Downloaded: {dl.downloaded}, Skipped: {dl.skipped_existing}")
    
    # Step 3: Extract
    print(f"\n📊 Step 3: Extracting data with auto-recovery...")
    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.stat().st_size > 0])
    print(f"  PDFs to process: {len(pdfs)}")
    
    extracted_rows = []
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
    
    # Step 4: Write outputs
    print(f"\n💾 Step 4: Writing outputs...")
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=extracted_rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=50,
    )
    
    run_summary = {
        "mode": "complete_end_to_end_pipeline",
        "total_pdfs": len(pdfs),
        "extracted_rows": len(extracted_rows),
        "count_needs_review": summary["count_needs_review"],
        "clean_rows": len(extracted_rows) - summary["count_needs_review"],
        "elapsed_total_s": round(elapsed, 2),
        "outputs": summary["outputs"],
    }
    
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    
    print()
    print("=" * 60)
    print("COMPLETE PIPELINE FINISHED")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
