from __future__ import annotations

import argparse
import concurrent.futures
import time
from pathlib import Path

from .extract import write_outputs
from .discover import discover_pdfs, DEFAULT_SEED_URLS
from .download import download_pdfs
from .ops.supabase import log_audit_if_configured
from .strategies.registry import get_strategy, list_strategies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", default="", help="Directory containing PDFs (if omitted, use --seed-url + --download-dir)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--strategy", default="llm_single_pass")
    ap.add_argument("--parallel-pdfs", type=int, default=1, help="Parallelism for extraction (max 20).")
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
        help="Deprecated: logging is automatic when Supabase env vars are set (use --no-supabase-log to disable)",
    )
    ap.add_argument("--seed-url", action="append", default=[], help="Seed page URL to discover PDFs (repeatable)")
    ap.add_argument("--download-dir", default="", help="Directory to download discovered PDFs into")
    ap.add_argument(
        "--download-limit",
        type=int,
        default=10,
        help="Max PDFs to download in website mode. Use 0 for no limit (can be very large).",
    )
    ap.add_argument("--parallel-downloads", type=int, default=1, help="Parallelism for website downloads (max 32).")
    ap.add_argument("--crawl-max-pages", type=int, default=50)
    ap.add_argument("--discover-all-hosts", action="store_true", help="Do not restrict crawl hostnames")
    ap.add_argument("--include-pdf-regex", default="", help="Only keep PDF URLs matching this regex")
    ap.add_argument("--exclude-pdf-regex", default="", help="Drop PDF URLs matching this regex")
    args = ap.parse_args()

    if args.list_strategies:
        print("\n".join(list_strategies()))
        return 0

    started = time.time()

    pdf_dir_val = (args.pdf_dir or "").strip()
    if not pdf_dir_val:
        seed_urls = args.seed_url or list(DEFAULT_SEED_URLS)
        dl_dir = Path(args.download_dir or "./downloads").expanduser().resolve()
        out_dir = Path(args.out_dir).expanduser().resolve()

        disc = discover_pdfs(
            seed_urls=seed_urls,
            max_pages=args.crawl_max_pages,
            state_path=out_dir / "discover_state.json",
            allowed_hosts=None if args.discover_all_hosts else ("www.tdhca.texas.gov", "tdhca.texas.gov"),
            include_pdf_regex=args.include_pdf_regex,
            exclude_pdf_regex=args.exclude_pdf_regex,
        )
        limit = None if int(args.download_limit) == 0 else int(args.download_limit)
        dl = download_pdfs(
            pdf_urls=disc.pdf_urls,
            out_dir=dl_dir,
            manifest_path=out_dir / "download_manifest.json",
            limit=limit,
            parallel_downloads=int(args.parallel_downloads),
        )
        print(f"discovered {len(disc.pdf_urls)} pdf url(s); downloaded={dl.downloaded} skipped={dl.skipped_existing} failed={dl.failed}")
        pdf_dir = dl_dir
    else:
        pdf_dir = Path(pdf_dir_val).expanduser().resolve()

    out_dir = Path(args.out_dir).expanduser().resolve()

    strat = get_strategy(args.strategy)
    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    parallel_n = max(1, min(20, int(args.parallel_pdfs)))
    if parallel_n == 1 or len(pdfs) <= 1:
        rows = [strat.extract(project_id=args.project_id, model=args.model, pdf_path=p, max_pages=args.max_pages).row for p in pdfs]
    else:
        def extract_one(p: Path):
            return strat.extract(project_id=args.project_id, model=args.model, pdf_path=p, max_pages=args.max_pages).row
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_n) as ex:
            rows = list(ex.map(extract_one, pdfs))
    summary = write_outputs(out_dir=out_dir, rows=rows, project_id=args.project_id, model=args.model, max_pages=args.max_pages)

    elapsed = round(time.time() - started, 2)
    print(f"ok: {summary['count_pdfs']} pdf(s) in {elapsed}s → {out_dir}")

    log_audit_if_configured(
        project_id=args.project_id,
        actor_id="lihtc-agent",
        event_type="lihtc_run",
        pipeline="lihtc_extract",
        no_supabase_log=args.no_supabase_log,
        require_supabase_log=args.require_supabase_log,
        payload={
            "strategy": args.strategy,
            "model": args.model,
            "max_pages": args.max_pages,
            "elapsed_s": elapsed,
            "pdf_dir": str(pdf_dir),
            "out_dir": str(out_dir),
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

