from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..extract import write_outputs
from ..strategies.registry import get_strategy, list_strategies
from .supabase import log_audit_if_configured


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out-dir", required=True, help="Directory where per-strategy outputs go")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--strategy", action="append", default=[], help="Repeat; default = all")
    ap.add_argument("--list-strategies", action="store_true")
    ap.add_argument(
        "--no-supabase-log",
        action="store_true",
        help="Do not write to Supabase even if SUPABASE_URL and SUPABASE_SERVICE_KEY are set",
    )
    ap.add_argument(
        "--require-supabase-log",
        action="store_true",
        help="Exit with error if Supabase is not configured or logging is disabled",
    )
    ap.add_argument(
        "--log-supabase",
        action="store_true",
        help="Deprecated: logging is automatic when Supabase env vars are set",
    )
    args = ap.parse_args()

    if args.list_strategies:
        print("\n".join(list_strategies()))
        return 0

    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    strategies = args.strategy or list_strategies()
    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])

    bench = {
        "project_id": args.project_id,
        "pdf_dir": str(pdf_dir),
        "count_pdfs": len(pdfs),
        "model": args.model,
        "max_pages": args.max_pages,
        "strategies": [],
    }

    for sname in strategies:
        strat = get_strategy(sname)
        t0 = time.time()
        results = [strat.extract(project_id=args.project_id, model=args.model, pdf_path=p, max_pages=args.max_pages) for p in pdfs]
        rows = [r.row for r in results]
        elapsed = round(time.time() - t0, 2)
        per_pdf = [r.wall_time_s for r in results]
        needs_review = sum(1 for r in rows if r.needs_review)

        out_sub = out_dir / sname
        summary = write_outputs(out_dir=out_sub, rows=rows, project_id=args.project_id, model=args.model, max_pages=args.max_pages)
        bench["strategies"].append(
            {
                "strategy": sname,
                "elapsed_s": elapsed,
                "avg_pdf_s": (sum(per_pdf) / len(per_pdf)) if per_pdf else 0.0,
                "needs_review": needs_review,
                "outputs": summary.get("outputs"),
                "out_dir": str(out_sub),
            }
        )

    bench_path = out_dir / "benchmark.json"
    bench_path.write_text(json.dumps(bench, indent=2), encoding="utf-8")
    print("ok wrote:", str(bench_path))

    log_audit_if_configured(
        project_id=args.project_id,
        actor_id="lihtc-agent",
        event_type="lihtc_benchmark",
        pipeline="lihtc_benchmark",
        no_supabase_log=args.no_supabase_log,
        require_supabase_log=args.require_supabase_log,
        payload=bench,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

