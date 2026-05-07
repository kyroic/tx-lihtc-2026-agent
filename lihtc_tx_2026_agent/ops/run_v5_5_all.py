#!/usr/bin/env python3
"""
V5.5 Runner - Extract ALL PDFs (no classifier filter)

Use when you know the PDFs contain Tie-Breaker info regardless of classification.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ..extract import sha256_file, write_outputs, ExtractedRow
from ..strategies.registry import get_strategy


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.5: Extract ALL PDFs without classifier filter")
    ap.add_argument("--pdf-dir", required=True, help="Directory with PDFs")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="v5_5_chunked_tiebreaker")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--manifest", default="", help="Optional manifest to filter which PDFs to process")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    # Load PDFs
    if args.manifest:
        with open(args.manifest) as f:
            manifest = json.load(f)
        urls = manifest.get("application_pdf_urls", [])
        # Match by URL or filename
        pdfs = []
        for p in sorted(pdf_dir.glob("*.pdf")):
            if any(p.name in url or p.stem in url for url in urls):
                pdfs.append(p)
        print(f"Loaded {len(pdfs)} PDFs from manifest")
    else:
        pdfs = sorted(pdf_dir.glob("*.pdf"))
        print(f"Found {len(pdfs)} PDFs in {pdf_dir}")

    strat = get_strategy(args.strategy)
    extracted_rows: list[ExtractedRow] = []

    t0 = time.time()
    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name}...", end=" ", flush=True)
        try:
            result = strat.extract(
                project_id=args.project_id,
                model=args.model,
                pdf_path=pdf,
                max_pages=args.max_pages,
            )
            row = result.row
            row.source_pdf_sha256 = sha256_file(pdf)
            extracted_rows.append(row)
            status = "✅" if not row.needs_review else "⚠️"
            print(f"{status} {row.application_name.value[:40] if row.application_name.value else '(no name)'}")
        except Exception as e:
            print(f"❌ Error: {e}")

    elapsed = time.time() - t0

    # Write outputs
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=extracted_rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=args.max_pages,
    )

    # Write run summary
    run_summary = {
        "pdf_dir": str(pdf_dir),
        "out_root": str(out_root),
        "total_pdfs": len(pdfs),
        "extracted_rows": len(extracted_rows),
        "count_needs_review": summary["count_needs_review"],
        "aggregate_xlsx": str(aggregate_dir / "applications.xlsx"),
        "elapsed_s": round(elapsed, 2),
        "avg_s_per_pdf": round(elapsed / len(pdfs), 2) if pdfs else 0,
    }
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    print()
    print("=" * 60)
    print("V5.5 EXTRACTION COMPLETE")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
