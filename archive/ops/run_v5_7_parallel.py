#!/usr/bin/env python3
"""
V5.7 Parallel Runner - Targeted Tie-Breaker + Parallel execution

Key optimizations:
1. Find Tie-Breaker pages by exact title (fast scan)
2. Extract only those pages (not all pages)
3. Run multiple PDFs in parallel
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from pathlib import Path
from typing import Any

from ..extract import sha256_file, write_outputs, ExtractedRow
from ..strategies.registry import get_strategy


def extract_one_pdf(
    pdf_path: Path,
    project_id: str,
    model: str,
    max_pages: int,
    strategy_name: str,
) -> tuple[Path, ExtractedRow | None, float]:
    """Extract one PDF, return (path, row, time)."""
    strat = get_strategy(strategy_name)
    start = time.time()
    try:
        result = strat.extract(
            project_id=project_id,
            model=model,
            pdf_path=pdf_path,
            max_pages=max_pages,
        )
        row = result.row
        row.source_pdf_sha256 = sha256_file(pdf_path)
        return pdf_path, row, time.time() - start
    except Exception as e:
        print(f"  ERROR {pdf_path.name}: {e}")
        return pdf_path, None, time.time() - start


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.7: Targeted + Parallel extraction")
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="v5_7_targeted_tiebreaker")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--parallel", type=int, default=4, help="Parallel PDFs (default 4)")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    print(f"Processing {len(pdfs)} PDFs with {args.parallel} parallel workers...")
    print(f"Strategy: {args.strategy}")
    print()

    extracted_rows: list[ExtractedRow] = []
    total_time = 0
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(args.parallel)) as executor:
        futures = {
            executor.submit(
                extract_one_pdf,
                pdf,
                args.project_id,
                args.model,
                args.max_pages,
                args.strategy,
            ): pdf
            for pdf in pdfs
        }

        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            pdf, row, elapsed = future.result()
            total_time += elapsed
            if row:
                extracted_rows.append(row)
            
            status = "✅" if row and not row.needs_review else "⚠️" if row else "❌"
            name = row.application_name.value[:35] if row and row.application_name.value else "(no name)"
            print(f"[{i}/{len(pdfs)}] {status} {pdf.name}: {name} ({elapsed:.1f}s)")

    elapsed_total = time.time() - t0
    avg_per_pdf = total_time / len(pdfs) if pdfs else 0

    # Write outputs
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=extracted_rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=args.max_pages,
    )

    run_summary = {
        "strategy": args.strategy,
        "total_pdfs": len(pdfs),
        "parallel_workers": args.parallel,
        "extracted_rows": len(extracted_rows),
        "count_needs_review": summary["count_needs_review"],
        "elapsed_total_s": round(elapsed_total, 2),
        "avg_time_per_pdf_s": round(avg_per_pdf, 2),
        "outputs": summary["outputs"],
    }
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    print()
    print("=" * 60)
    print("V5.7 COMPLETE")
    print("=" * 60)
    print(json.dumps(run_summary, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
