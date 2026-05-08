#!/usr/bin/env python3
"""
Smart PDF classifier using AI + folder context.

Key insight: TDHCA organizes PDFs by type in folders.
- /2026-9-challenges/ → Full Applications (114 PDFs)
- /2026preapps/ → Pre-Applications
- /2026-9-ESA/ → Environmental assessments
- etc.

This classifier:
1. Uses folder name as strong signal
2. Has AI read actual content (not just first page)
3. Specifically looks for Tie-Breaker pages
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pdfplumber

from ..model_client import chat_completions, extract_json_content


# Folder-to-type mapping based on TDHCA structure
FOLDER_HINTS = {
    "2026-9-challenges": "full_application",
    "2026-9-Appraisals": "appraisal",
    "2026-9-ESA": "appraisal",  # Environmental Site Assessment
    "2026-9-PCA": "appraisal",  # Property Condition Assessment
    "2026-9-Market": "market_study",
    "2026-9-SDFR": "other",  # Subsidized Debt Financing Request
    "2026preapps": "pre_application",
    "2026-PreAppDeficiencies": "pre_application",
    "2026-4-TEBApps": "pre_application",  # Tax-Exempt Bonds
}


def _extract_folder_name(pdf_path: Path) -> str:
    """Extract folder name from path like out_XYZ/downloads/abc_26001.pdf"""
    # Try to find folder in path
    parts = str(pdf_path).split("/")
    for i, part in enumerate(parts):
        if part == "downloads" and i + 1 < len(parts):
            # Check if next part looks like a folder that was in discover_state
            return parts[i + 1] if i + 1 < len(parts) else ""
    return ""


def _find_tiebreaker_pages(pdf_path: Path) -> list[int]:
    """Quick search for Tie-Breaker keyword across all pages."""
    keywords = ["tie-breaker", "tiebreaker", "tie breaker", "Competitive HTC Only"]
    found = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").lower()
                if any(kw.lower() in text for kw in keywords):
                    found.append(idx)
                    if len(found) >= 10:  # Cap at 10 for speed
                        break
    except Exception:
        pass
    return found


def _extract_key_content(pdf_path: Path, max_pages: int = 5) -> str:
    """Extract text from first N pages + any Tie-Breaker pages."""
    content = []
    tiebreaker_pages = _find_tiebreaker_pages(pdf_path)
    
    pages_to_read = set(range(1, min(max_pages + 1, 6)))  # First 5 pages
    pages_to_read.update(tiebreaker_pages[:5])  # Plus Tie-Breaker pages
    
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num in sorted(pages_to_read):
                if page_num < 1 or page_num > len(pdf.pages):
                    continue
                page = pdf.pages[page_num - 1]
                text = page.extract_text() or ""
                content.append(f"=== PAGE {page_num} ===\n{text[:2000]}")
    except Exception:
        pass
    
    return "\n\n".join(content)[:12000]


def classify_pdf_smart(*, project_id: str, model: str, pdf_path: Path) -> dict[str, Any]:
    """Smart classification using folder hints + AI analysis of actual content."""
    
    # Step 1: Folder hint
    folder = _extract_folder_name(pdf_path)
    folder_hint = FOLDER_HINTS.get(folder, "unknown")
    
    # Step 2: Check for Tie-Breaker pages (strong signal for full applications)
    tiebreaker_pages = _find_tiebreaker_pages(pdf_path)
    has_tiebreaker = len(tiebreaker_pages) > 0
    
    # Step 3: AI analysis if folder hint is unclear or we want confirmation
    if folder_hint in ("unknown", "other", "pre_application") or has_tiebreaker:
        content = _extract_key_content(pdf_path)
        
        system = """You classify Texas LIHTC 2026 PDFs based on actual content.
Return ONLY JSON:
{
  "doc_type": "full_application|pre_application|appraisal|attachment|market_study|other",
  "year": "2026|other|unknown",
  "confidence": 0.0-1.0,
  "signals": ["list of evidence phrases found"],
  "has_tiebreaker": true/false,
  "reasoning": "brief explanation"
}

Key signals for full_application:
- Contains "Tie-Breaker Information" or "Competitive HTC Only"
- Has distance measurements (feet, miles) to amenities
- Contains census tract, quartile, scoring sections
- Application name, developer contact, full project details"""

        user = json.dumps({
            "filename": pdf_path.name,
            "folder": folder,
            "folder_hint": folder_hint,
            "has_tiebreaker_pages": has_tiebreaker,
            "tiebreaker_page_numbers": tiebreaker_pages[:10],
            "content_sample": content[:8000]
        }, ensure_ascii=False)
        
        try:
            resp = chat_completions(
                project_id=project_id,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user}
                ],
                temperature=0.0,
                timeout_s=120
            )
            ai_result = extract_json_content(resp)
        except Exception as e:
            ai_result = {"error": str(e)}
    else:
        ai_result = {"skipped": "folder hint clear"}
    
    # Step 4: Combine signals
    final_type = folder_hint
    confidence = 0.5
    
    if has_tiebreaker:
        final_type = "full_application"
        confidence = 0.95
    elif ai_result.get("doc_type") and ai_result.get("confidence", 0) > 0.7:
        final_type = ai_result["doc_type"]
        confidence = ai_result["confidence"]
    
    return {
        "doc_type": final_type,
        "year": "2026",
        "confidence": confidence,
        "folder": folder,
        "folder_hint": folder_hint,
        "has_tiebreaker": has_tiebreaker,
        "tiebreaker_pages": tiebreaker_pages,
        "ai_analysis": ai_result,
        "pdf_path": str(pdf_path)
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--sample", type=int, default=0, help="Only classify N PDFs for testing")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    
    if args.sample > 0:
        pdfs = pdfs[:args.sample]
        print(f"Sampling {len(pdfs)} PDFs for testing")
    
    print(f"Classifying {len(pdfs)} PDFs with smart classifier...")
    
    results = []
    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name}...", end=" ", flush=True)
        result = classify_pdf_smart(
            project_id=args.project_id,
            model=args.model,
            pdf_path=pdf
        )
        results.append(result)
        print(f"→ {result['doc_type']} (tiebreaker: {result['has_tiebreaker']})")
    
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    
    # Summary
    from collections import Counter
    types = Counter(r["doc_type"] for r in results)
    tiebreaker_count = sum(1 for r in results if r["has_tiebreaker"])
    
    print(f"\n=== Classification Summary ===")
    print(f"Total: {len(results)}")
    print(f"With Tie-Breaker pages: {tiebreaker_count}")
    print("By type:")
    for k, v in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print(f"\nSaved to: {out_path}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
