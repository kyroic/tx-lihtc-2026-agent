#!/usr/bin/env python3
"""
V5.5 Runner - Chunked Tie-Breaker Extraction

Usage:
    python3 -m lihtc_tx_2026_agent.ops.run_v5_5 --pdf-dir <path> --out-dir <path>
    python3 -m lihtc_tx_2026_agent.ops.run_v5_5 --manifest <path> --out-dir <path>

This version:
1. Searches ALL pages for Tie-Breaker keywords (memory-safe)
2. Extracts Tie-Breaker pages thoroughly with tables
3. Includes full Tie-Breaker content in LLM prompt
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

from ..extract import (
    ExtractedRow,
    extracted_row_from_jsondict,
    field_from_obj,
    write_outputs,
    coaching_append_from_env,
    norm_ws,
)
from ..model_client import chat_completions, extract_json_content


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_tiebreaker_pages(pdf_path: Path, verbose: bool = False) -> list[int]:
    """
    First pass: Lightweight search for Tie-Breaker pages across ALL pages.
    Returns list of page numbers (1-indexed) that contain Tie-Breaker keywords.
    """
    tiebreaker_keywords = [
        "tie-breaker",
        "tiebreaker",
        "tie breaker",
        "Tie-Breaker Information",
        "Competitive HTC Only",
    ]
    
    found_pages = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            for idx, page in enumerate(pdf.pages, start=1):
                try:
                    text = (page.extract_text() or "").lower()
                    if any(kw.lower() in text for kw in tiebreaker_keywords):
                        found_pages.append(idx)
                        if verbose:
                            print(f"  Found Tie-Breaker on page {idx}/{total_pages}")
                except Exception as e:
                    if verbose:
                        print(f"  Error on page {idx}: {e}")
                    continue
    except Exception as e:
        if verbose:
            print(f"  Error opening PDF: {e}")
    
    return found_pages


def extract_chunked_pages(
    pdf_path: Path,
    page_numbers: list[int],
    chunk_size: int = 50,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """
    Second pass: Extract specific pages in chunks to avoid memory issues.
    Returns list of page dicts with full text and tables.
    """
    extracted = []
    
    # Sort and dedupe pages
    pages_to_extract = sorted(set(page_numbers))
    
    if verbose:
        print(f"  Extracting {len(pages_to_extract)} Tie-Breaker pages in chunks of {chunk_size}")
    
    # Process in chunks
    for i in range(0, len(pages_to_extract), chunk_size):
        chunk = pages_to_extract[i : i + chunk_size]
        
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page_num in chunk:
                    if page_num < 1 or page_num > len(pdf.pages):
                        continue
                        
                    page = pdf.pages[page_num - 1]
                    try:
                        text = page.extract_text() or ""
                        tables = page.extract_tables() or []
                        
                        # Format tables as text
                        table_text = []
                        for table_idx, table in enumerate(tables):
                            if table:
                                rows = []
                                for row in table:
                                    if row:
                                        cells = [str(cell or "").strip() for cell in row if cell is not None]
                                        if cells:
                                            rows.append(" | ".join(cells))
                                if rows:
                                    table_text.append(f"[Table {table_idx + 1}]\n" + "\n".join(rows))
                        
                        extracted.append({
                            "page": page_num,
                            "text": text.strip()[:8000],
                            "tables": table_text,
                            "full_text": text.strip(),
                        })
                    except Exception as e:
                        if verbose:
                            print(f"    Error extracting page {page_num}: {e}")
                        continue
        except Exception as e:
            if verbose:
                print(f"  Error processing chunk: {e}")
            continue
    
    return extracted


def extract_all_pages_lightweight(pdf_path: Path, max_pages: int = 50) -> list[dict[str, Any]]:
    """
    Extract first N pages with basic text (for non-Tie-Breaker content).
    """
    pages = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages[:max_pages], start=1):
                try:
                    text = page.extract_text() or ""
                    pages.append({"page": idx, "text": text.strip()[:4000], "tables": []})
                except Exception:
                    continue
    except Exception:
        pass
    return pages


def extract_one_pdf_v5_5(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    max_pages: int = 50,
    verbose: bool = False,
) -> ExtractedRow:
    """
    V5.5: Chunked Tie-Breaker extraction.
    """
    if verbose:
        print(f"Processing {pdf_path.name}...")
    
    # Step 1: Find all Tie-Breaker pages across entire PDF
    tiebreaker_pages = find_tiebreaker_pages(pdf_path, verbose=verbose)
    if verbose:
        print(f"  Found {len(tiebreaker_pages)} Tie-Breaker pages: {tiebreaker_pages[:10]}{'...' if len(tiebreaker_pages) > 10 else ''}")
    
    # Step 2: Extract first N pages (standard content)
    standard_pages = extract_all_pages_lightweight(pdf_path, max_pages=max_pages)
    
    # Step 3: Extract Tie-Breaker pages thoroughly (chunked)
    tiebreaker_content = extract_chunked_pages(pdf_path, tiebreaker_pages, chunk_size=50, verbose=verbose)
    
    # Build page hints for standard fields
    def has_any(t: str, needles: list[str]) -> bool:
        return any(n in t for n in needles)
    
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
        
        if has_any(txt, ["development name", "project name", "property name", "application name"]):
            page_hints["application_name"].append(pn)
        if has_any(txt, ["contact", "prepared by", "authorized representative"]):
            page_hints["contact"].append(pn)
        if has_any(txt, ["census tract", "tract", "geoid"]):
            page_hints["census_tract"].append(pn)
    
    # Build system prompt
    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote copied from the PDF text.\n"
        "- CRITICAL: The 'tiebreaker_pages' field tells you which pages contain Tie-Breaker Information.\n"
        "- For tiebreaker_* fields (park, school, grocery, library), focus on those pages.\n"
        "- Distance values are often in format like '300 feet', '500 feet', '0.3 miles', etc.\n"
        "- Coordinates may appear as decimal numbers (latitude/longitude).\n"
        "- Look for tables on Tie-Breaker pages - they contain distance data.\n"
    ) + coaching_append_from_env()
    
    # Build user prompt with both standard and Tie-Breaker content
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
    
    # Call LLM
    try:
        resp = chat_completions(
            project_id=project_id,
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)}],
            temperature=0.0,
        )
        
        out = extract_json_content(resp)
    except Exception as e:
        if verbose:
            print(f"  LLM error: {e}")
        out = {}
    
    # Build row
    row = ExtractedRow(
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=sha256_file(pdf_path),
        extraction_version="v5.5_chunked_tiebreaker",
    )
    
    for k in schema_hint.keys():
        setattr(row, k, field_from_obj(out.get(k)))
    
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
    
    if verbose:
        status = "✅" if not row.needs_review else "⚠️"
        print(f"  {status} Extracted: {row.application_name.value[:50] if row.application_name.value else '(no name)'}")
        if row.review_reasons:
            print(f"     Review: {', '.join(row.review_reasons)}")
    
    return row


def load_manifest(manifest_path: Path) -> list[Path]:
    """Load PDF paths from manifest JSON."""
    with manifest_path.open(encoding="utf-8") as f:
        data = json.load(f)
    
    pdfs = []
    # Handle different manifest formats
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                pdfs.append(Path(item))
            elif isinstance(item, dict) and "path" in item:
                pdfs.append(Path(item["path"]))
    elif isinstance(data, dict):
        if "pdfs" in data and isinstance(data["pdfs"], list):
            for item in data["pdfs"]:
                if isinstance(item, str):
                    pdfs.append(Path(item))
                elif isinstance(item, dict) and "path" in item:
                    pdfs.append(Path(item["path"]))
        elif "files" in data and isinstance(data["files"], list):
            for item in data["files"]:
                if isinstance(item, str):
                    pdfs.append(Path(item))
    
    return pdfs


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.5: Chunked Tie-Breaker Extraction")
    ap.add_argument("--pdf-dir", default="", help="Directory containing PDFs")
    ap.add_argument("--manifest", default="", help="Manifest JSON file with PDF paths")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=50, help="Max pages for standard extraction")
    ap.add_argument("--parallel", type=int, default=1, help="Parallel PDF processing (max 20)")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    
    args = ap.parse_args()
    
    # Collect PDFs
    pdfs: list[Path] = []
    
    if args.pdf_dir:
        pdf_dir = Path(args.pdf_dir)
        if pdf_dir.is_dir():
            pdfs = sorted(pdf_dir.glob("*.pdf"))
            print(f"Found {len(pdfs)} PDFs in {pdf_dir}")
        else:
            print(f"Error: {pdf_dir} is not a directory")
            return 1
    
    if args.manifest:
        manifest_path = Path(args.manifest)
        if manifest_path.is_file():
            manifest_pdfs = load_manifest(manifest_path)
            pdfs.extend(manifest_pdfs)
            print(f"Loaded {len(manifest_pdfs)} PDFs from manifest")
        else:
            print(f"Error: {manifest_path} is not a file")
            return 1
    
    if not pdfs:
        print("Error: No PDFs found. Use --pdf-dir or --manifest")
        return 1
    
    print(f"\nV5.5 Extraction Starting")
    print(f"  PDFs: {len(pdfs)}")
    print(f"  Model: {args.model}")
    print(f"  Parallel: {args.parallel}")
    print(f"  Output: {args.out_dir}")
    print()
    
    # Process PDFs
    results: list[ExtractedRow] = []
    start_time = time.time()
    
    if args.parallel > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.parallel, 20)) as executor:
            futures = {
                executor.submit(
                    extract_one_pdf_v5_5,
                    project_id=args.project_id,
                    model=args.model,
                    pdf_path=pdf,
                    max_pages=args.max_pages,
                    verbose=args.verbose,
                ): pdf
                for pdf in pdfs
            }
            
            for future in concurrent.futures.as_completed(futures):
                pdf = futures[future]
                try:
                    row = future.result()
                    results.append(row)
                except Exception as e:
                    print(f"Error processing {pdf}: {e}")
    else:
        for pdf in pdfs:
            try:
                row = extract_one_pdf_v5_5(
                    project_id=args.project_id,
                    model=args.model,
                    pdf_path=pdf,
                    max_pages=args.max_pages,
                    verbose=args.verbose,
                )
                results.append(row)
            except Exception as e:
                print(f"Error processing {pdf}: {e}")
    
    elapsed = time.time() - start_time
    
    # Write outputs
    out_dir = Path(args.out_dir)
    summary = write_outputs(
        out_dir=out_dir,
        rows=results,
        project_id=args.project_id,
        model=args.model,
        max_pages=args.max_pages,
    )
    
    print()
    print("=" * 60)
    print("V5.5 EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"  PDFs processed: {len(results)}")
    print(f"  Needs review: {summary['count_needs_review']}")
    print(f"  Time elapsed: {elapsed:.1f}s ({elapsed/len(results):.1f}s/PDF)")
    print(f"  Output: {out_dir}")
    print()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
