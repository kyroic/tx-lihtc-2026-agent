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

# Phase 2: coordinate auto-recovery (lat/lng in decimal degrees)
COORD_PATTERNS = [
    r'(?:latitude|lat)[:\s]+(-?\d{1,2}\.\d{4,})',
    r'(?:longitude|lng|long)[:\s]+(-?\d{1,3}\.\d{4,})',
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
    
    # Use V5.8 strategy which already works
    strat = get_strategy("v5_8_fast_tiebreaker")
    
    try:
        result = strat.extract(
            project_id=project_id,
            model=model,
            pdf_path=pdf_path,
            max_pages=50,
        )
        row = result.row
        row.source_pdf_sha256 = sha256_file(pdf_path)
        row.extraction_version = "complete_pipeline"
        
        # Additional auto-recovery if LLM missed something
        if not row.census_tract.value:
            census_tract = find_census_tract(pdf_path)
            if census_tract:
                row.census_tract = field_from_obj({
                    "value": census_tract,
                    "confidence": 0.95,
                    "pages": [1],
                    "quote": f"Auto-recovered: {census_tract}",
                })
        
        if not row.poverty_rank.value:
            poverty_rate = find_poverty_rate(pdf_path)
            if poverty_rate:
                row.poverty_rank = field_from_obj({
                    "value": poverty_rate,
                    "confidence": 0.95,
                    "pages": [1],
                    "quote": f"Auto-recovered: {poverty_rate}",
                })

        # Phase 2: coordinate auto-recovery from tiebreaker pages
        tb_text = extract_text_with_pdftotext(pdf_path)
        if tb_text:
            # Try to fill missing site coords
            if not row.site_lat.value or not row.site_lng.value:
                site_lat_match = re.search(r'(?:site|project|development)\s*(?:latitude|lat)[:\s]+(-?\d{1,2}\.\d{4,})', tb_text, re.IGNORECASE)
                site_lng_match = re.search(r'(?:site|project|development)\s*(?:longitude|lng|long)[:\s]+(-?\d{1,3}\.\d{4,})', tb_text, re.IGNORECASE)
                if site_lat_match and not row.site_lat.value:
                    row.site_lat = field_from_obj({"value": site_lat_match.group(1), "confidence": 0.85, "pages": [1], "quote": f"Auto-recovered: {site_lat_match.group(1)}"})
                if site_lng_match and not row.site_lng.value:
                    row.site_lng = field_from_obj({"value": site_lng_match.group(1), "confidence": 0.85, "pages": [1], "quote": f"Auto-recovered: {site_lng_match.group(1)}"})

            # Try to recover tiebreaker_score
            if not row.tiebreaker_score.value:
                score_match = re.search(r'(?:total|aggregate|overall)\s*(?:tie[- ]?breaker\s*)?(?:score|points)[:\s]+([0-9,.]+)', tb_text, re.IGNORECASE)
                if score_match:
                    row.tiebreaker_score = field_from_obj({"value": score_match.group(1).replace(",", ""), "confidence": 0.85, "pages": [1], "quote": f"Auto-recovered: {score_match.group(1)}"})

        return row
    except Exception as e:
        # Fallback empty row
        row = ExtractedRow(
            source_pdf_path=str(pdf_path),
            source_pdf_sha256=sha256_file(pdf_path),
            extraction_version="complete_pipeline",
        )
        row.review_reasons.append(f"extraction_error:{str(e)[:50]}")
        row.needs_review = True
        return row


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Complete End-to-End Pipeline (discover → download → extract)"
    )
    ap.add_argument("--pdf-dir", default="downloads", help="PDF directory (default: downloads)")
    ap.add_argument("--out-dir", default="out", help="Output directory (default: out)")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--skip-discover", action="store_true", help="Skip discovery if PDFs exist")
    ap.add_argument("--source-folder", default="2026-9-challenges", help="Folder under /imaged/ to target (default: 2026-9-challenges)")
    ap.add_argument("--download-parallel", type=int, default=10)
    ap.add_argument("--max-download-bytes", type=int, default=500_000_000, help="Per-PDF max bytes (default 500MB)")
    args = ap.parse_args()
    
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    
    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    
    selected_urls: list[str] = []

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
        all_urls = list(disc.pdf_urls)
        selected_urls = [u for u in all_urls if f"/imaged/{args.source_folder}/" in u]
        print(f"  Found {len(all_urls)} PDF URLs total")
        print(f"  Selected {len(selected_urls)} from folder: {args.source_folder}")
        pdf_dir = out_root / "downloads"

    # Step 2: Download
    print(f"\n📥 Step 2: Downloading PDFs to {pdf_dir}...")
    pdfs = list(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []
    if selected_urls:
        dl = download_pdfs(
            pdf_urls=selected_urls,
            out_dir=pdf_dir,
            manifest_path=out_root / "download_manifest.json",
            max_new_downloads=len(selected_urls),
            parallel_downloads=int(args.download_parallel),
            max_bytes=int(args.max_download_bytes),
            timeout_s=120,
        )
        print(f"  Downloaded: {dl.downloaded}, Skipped: {dl.skipped_existing}, Failed: {dl.failed}")
    else:
        print("  Using existing PDFs in --pdf-dir (skip discovery/download)")
    
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

    # Step 5: Cleanliness pass
    print(f"\n🧹 Step 5: Running cleanliness pass...")
    from ..cleanliness import clean_csv
    raw_csv = aggregate_dir / "applications.csv"
    clean_dir = out_root / "cleaned"
    clean_dir.mkdir(parents=True, exist_ok=True)
    clean_result = clean_csv(raw_csv, clean_dir)
    print(f"  Mean quality: {clean_result['mean_quality_score']:.2f}")
    print(f"  Perfect rows: {clean_result['rows_perfect']}")
    top_issues = list(clean_result.get('issue_frequency', {}).items())[:3]
    print(f"  Top issues: {top_issues}")
    
    run_summary = {
        "mode": "complete_end_to_end_pipeline",
        "source_folder": args.source_folder,
        "selected_url_count": len(selected_urls),
        "total_pdfs": len(pdfs),
        "extracted_rows": len(extracted_rows),
        "count_needs_review": summary["count_needs_review"],
        "clean_rows": len(extracted_rows) - summary["count_needs_review"],
        "elapsed_total_s": round(elapsed, 2),
        "outputs": summary["outputs"],
        "cleanliness": {
            "mean_quality": clean_result["mean_quality_score"],
            "perfect_rows": clean_result["rows_perfect"],
            "cleaned_csv": str(clean_dir / "applications_cleaned.csv"),
        },
    }
    
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    
    print()
    print("=" * 60)
    print("COMPLETE PIPELINE FINISHED")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2, default=str))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
