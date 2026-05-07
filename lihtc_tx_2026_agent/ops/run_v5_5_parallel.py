#!/usr/bin/env python3
"""V5.5 Parallel Batch Runner - Process multiple PDFs concurrently"""

import argparse
import concurrent.futures
import json
import time
from pathlib import Path
from ..extract import sha256_file, write_outputs, ExtractedRow
from ..strategies.registry import get_strategy

def extract_one(pdf_path, project_id, model, max_pages):
    strat = get_strategy("v5_5_chunked_tiebreaker")
    try:
        result = strat.extract(project_id=project_id, model=model, pdf_path=pdf_path, max_pages=max_pages)
        row = result.row
        row.source_pdf_sha256 = sha256_file(pdf_path)
        return (pdf_path, row, None)
    except Exception as e:
        return (pdf_path, None, str(e))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--parallel", type=int, default=4, help="Parallel PDFs")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    print(f"Processing {len(pdfs)} PDFs with {args.parallel} parallel workers...")

    extracted_rows = []
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {
            ex.submit(extract_one, p, args.project_id, args.model, args.max_pages): p
            for p in pdfs
        }
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            pdf_path, row, error = future.result()
            if error:
                print(f"[{i}/{len(pdfs)}] ❌ {pdf_path.name}: {error}")
            else:
                extracted_rows.append(row)
                status = "✅" if not row.needs_review else "⚠️"
                name = row.application_name.value[:35] if row.application_name.value else "(no name)"
                print(f"[{i}/{len(pdfs)}] {status} {name}")

    elapsed = time.time() - t0
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=extracted_rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=args.max_pages,
    )

    run_summary = {
        "total_pdfs": len(pdfs),
        "extracted": len(extracted_rows),
        "parallel_workers": args.parallel,
        "elapsed_s": round(elapsed, 2),
        "avg_s_per_pdf": round(elapsed / len(pdfs), 2) if pdfs else 0,
        "outputs": str(aggregate_dir),
    }
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    print(f"\n=== Complete ===")
    print(f"Extracted: {len(extracted_rows)}/{len(pdfs)}")
    print(f"Time: {elapsed/60:.1f}m ({elapsed/len(pdfs):.1f}s/PDF)")
    print(json.dumps(run_summary, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
