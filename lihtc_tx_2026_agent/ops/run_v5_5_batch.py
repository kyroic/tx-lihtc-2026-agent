#!/usr/bin/env python3
"""V5.5 Batch Runner - Process PDFs in chunks to avoid timeout"""

import argparse
import json
import time
from pathlib import Path
from ..extract import sha256_file, write_outputs, ExtractedRow
from ..strategies.registry import get_strategy

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--batch-start", type=int, default=0)
    ap.add_argument("--batch-end", type=int, default=0)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="v5_5_chunked_tiebreaker")
    ap.add_argument("--max-pages", type=int, default=50)
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    start = args.batch_start
    end = args.batch_end or len(pdfs)
    batch_pdfs = pdfs[start:end]

    print(f"Processing {len(batch_pdfs)} PDFs ({start}-{end} of {len(pdfs)})")

    strat = get_strategy(args.strategy)
    extracted_rows: list[ExtractedRow] = []

    t0 = time.time()
    for i, pdf in enumerate(batch_pdfs, start):
        print(f"[{i+1}/{len(pdfs)}] {pdf.name}...", end=" ", flush=True)
        try:
            result = strat.extract(
                project_id="lihtc-tx-2026",
                model=args.model,
                pdf_path=pdf,
                max_pages=args.max_pages,
            )
            row = result.row
            row.source_pdf_sha256 = sha256_file(pdf)
            extracted_rows.append(row)
            status = "✅" if not row.needs_review else "⚠️"
            name = row.application_name.value[:35] if row.application_name.value else "(no name)"
            print(f"{status} {name}")
        except Exception as e:
            print(f"❌ {e}")

    # Always write outputs even if interrupted
    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=extracted_rows,
        project_id="lihtc-tx-2026",
        model=args.model,
        max_pages=args.max_pages,
    )

    elapsed = time.time() - t0
    run_summary = {
        "batch_start": start,
        "batch_end": end,
        "total_pdfs": len(pdfs),
        "processed": len(extracted_rows),
        "elapsed_s": round(elapsed, 2),
        "outputs": str(aggregate_dir),
    }
    (out_root / f"batch_{start}_{end}_summary.json").write_text(json.dumps(run_summary, indent=2))

    print(f"\nBatch complete: {len(extracted_rows)} extracted in {elapsed:.0f}s")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
