"""
**Website harvest** (not eval “runs”): N **harvest cycles** of
**discover → download → classify → extract** so you can resume TDHCA 2026 PDF
work without re-downloading (shared manifest).

For **agent improvement iterations** on fixtures + coaching, use
``python3 -m lihtc_tx_2026_agent.ops.progression_run`` (its ``--runs`` means eval passes).

Cumulative outputs: ``<out_root>/aggregate/`` (applications.xlsx + jsonl/csv)
and per-cycle folders ``<out_root>/agentic_progression/run_KKK/`` (legacy path name).

**Defaults** assume you want **broad coverage** (many 2026 apps), not tiny batches: a
higher per-cycle download baseline plus **adaptive** batch sizing (on by default) when
the crawl list is noisy. Use ``--no-adaptive-downloads`` for a fixed cap only.
For an **OpenClaw planner loop** that chooses seeds,
crawl budget, regex filters, and download batch each iteration (Python still runs
the tools), use ``python3 -m lihtc_tx_2026_agent.ops.openclaw_orchestrator``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..discover import DEFAULT_SEED_URLS, discover_pdfs
from ..download import download_pdfs
from ..extract import (
    ExtractedRow,
    extracted_row_from_jsondict,
    sha256_file,
    write_outputs,
)
from ..strategies.registry import get_strategy
from .agentic_run import TARGET_FIELDS, _missing_fields, _recover_missing_fields
from .classify_pdfs import classify_pdf
from .openclaw_workspace_cleanup import run_openclaw_download_hygiene
from .supabase import log_audit_if_configured


def _default_out_root() -> Path:
    return Path.home() / "Desktop" / "parcell" / "agent"


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _load_cumulative_rows(jsonl_path: Path) -> list[ExtractedRow]:
    rows: list[ExtractedRow] = []
    if not jsonl_path.is_file():
        return rows
    with jsonl_path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(extracted_row_from_jsondict(json.loads(line)))
            except Exception:
                continue
    return rows


def _write_cumulative(*, jsonl_path: Path, aggregate_dir: Path, rows: list[ExtractedRow], project_id: str, model: str, max_pages: int) -> None:
    by_sha: dict[str, ExtractedRow] = {}
    for r in rows:
        h = (r.source_pdf_sha256 or "").strip()
        if h:
            by_sha[h] = r
    merged = list(by_sha.values())
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    write_outputs(out_dir=aggregate_dir, rows=merged, project_id=project_id, model=model, max_pages=max_pages)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "TDHCA website harvest: repeated discover→download→classify→extract cycles. "
            "Use progression_run.py --runs for eval/agent improvement passes."
        ),
    )
    ap.add_argument(
        "--harvest-cycles",
        type=int,
        default=None,
        metavar="N",
        help="How many website harvest passes to execute (default: 20). Not the same as eval improvement runs.",
    )
    ap.add_argument("--runs", type=int, default=None, metavar="N", help=argparse.SUPPRESS)
    ap.add_argument("--out-root", default=str(_default_out_root()), help="Workspace root (Desktop/parcell/agent)")
    ap.add_argument("--download-dir", default="", help="PDF download dir (default: <out-root>/downloads)")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="llm_page_router_then_extract")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--seed-url", action="append", default=[])
    ap.add_argument("--crawl-max-pages", type=int, default=500)
    ap.add_argument("--include-pdf-regex", default="2026")
    ap.add_argument("--exclude-pdf-regex", default="Appraisals")
    ap.add_argument(
        "--max-new-downloads-per-cycle",
        type=int,
        default=None,
        metavar="N",
        help="Baseline max new PDFs per harvest cycle before adaptive adjustment (default: 50).",
    )
    ap.add_argument("--max-new-downloads-per-run", type=int, default=None, metavar="N", help=argparse.SUPPRESS)
    ap.add_argument(
        "--adaptive-downloads",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Tune download batch between cycles when the list is noisy (default: on). Use --no-adaptive-downloads to fix the cap.",
    )
    ap.add_argument(
        "--adaptive-download-multiplier",
        type=float,
        default=1.5,
        help="Multiply previous cap when zero full_application hits (capped by --adaptive-download-max).",
    )
    ap.add_argument(
        "--adaptive-download-max",
        type=int,
        default=150,
        help="Upper bound for adaptive max_new_downloads per harvest cycle (default: 150).",
    )
    ap.add_argument("--recovery-iters", type=int, default=2)
    ap.add_argument("--recovery-max-pages", type=int, default=120)
    ap.add_argument("--recovery-page-budget", type=int, default=34)
    ap.add_argument(
        "--self-cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before each cycle: remove redundant/junk PDFs in downloads/ (see OpenClaw hygiene).",
    )
    ap.add_argument(
        "--min-free-mb",
        type=float,
        default=float(os.environ.get("LIHTC_MIN_FREE_MB") or "2048"),
        help="Target free disk MB after hygiene (env LIHTC_MIN_FREE_MB).",
    )
    ap.add_argument("--cleanup-log-name", default="progression_agentic_console.log")
    ap.add_argument("--trim-console-log-mb", type=float, default=80.0)
    ap.add_argument("--trim-console-log-tail", type=int, default=8000)
    ap.add_argument(
        "--cleanup-delete-extracted-pdfs",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    ap.add_argument(
        "--cleanup-delete-classified-junk",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    ap.add_argument(
        "--no-download-cap",
        action="store_true",
        help="Disable max_new_downloads cap; attempt to download every discovered PDF each cycle (can be large).",
    )
    ap.add_argument("--no-supabase-log", action="store_true")
    ap.add_argument("--require-supabase-log", action="store_true")
    ap.add_argument(
        "--dry-config",
        action="store_true",
        help="Resolve flags, print settings as JSON to stdout, and exit (no crawl or downloads).",
    )
    args = ap.parse_args()

    if args.runs is not None and args.harvest_cycles is not None:
        print(
            "progression_agentic: error: pass only one of --harvest-cycles or --runs (deprecated).",
            file=sys.stderr,
            flush=True,
        )
        return 2
    if args.runs is not None:
        print(
            "progression_agentic: warning: --runs is deprecated; use --harvest-cycles for website harvest passes.",
            file=sys.stderr,
            flush=True,
        )
        harvest_cycles = max(1, int(args.runs))
    elif args.harvest_cycles is not None:
        harvest_cycles = max(1, int(args.harvest_cycles))
    else:
        harvest_cycles = 20

    if args.max_new_downloads_per_run is not None and args.max_new_downloads_per_cycle is not None:
        print(
            "progression_agentic: error: pass only one of --max-new-downloads-per-cycle "
            "or --max-new-downloads-per-run (deprecated).",
            file=sys.stderr,
            flush=True,
        )
        return 2
    if args.max_new_downloads_per_run is not None:
        print(
            "progression_agentic: warning: --max-new-downloads-per-run is deprecated; use --max-new-downloads-per-cycle.",
            file=sys.stderr,
            flush=True,
        )
        baseline_cap = max(1, int(args.max_new_downloads_per_run))
    elif args.max_new_downloads_per_cycle is not None:
        baseline_cap = max(1, int(args.max_new_downloads_per_cycle))
    else:
        baseline_cap = 50

    if args.dry_config:
        out = Path(args.out_root).expanduser().resolve()
        print(
            json.dumps(
                {
                    "harvest_cycles": harvest_cycles,
                    "max_new_downloads_baseline": baseline_cap,
                    "deprecated_runs_cli_used": args.runs is not None,
                    "deprecated_max_new_downloads_per_run_used": args.max_new_downloads_per_run is not None,
                    "out_root": str(out),
                },
                indent=2,
            ),
            flush=True,
        )
        return 0

    out_root = Path(args.out_root).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    dl_dir = Path(args.download_dir).expanduser().resolve() if str(args.download_dir).strip() else (out_root / "downloads")
    dl_dir.mkdir(parents=True, exist_ok=True)

    prog_dir = out_root / "agentic_progression"
    prog_dir.mkdir(parents=True, exist_ok=True)
    discover_state = out_root / "discover_state.json"
    manifest_path = out_root / "download_manifest.json"
    class_cache_path = out_root / "classification_cache.json"
    extracted_hashes_path = out_root / "extracted_hashes.json"
    cumulative_jsonl = out_root / "all_applications.jsonl"
    aggregate_dir = out_root / "aggregate"
    adaptive_path = out_root / "adaptive_download_state.json"

    seeds = args.seed_url or list(DEFAULT_SEED_URLS)
    strat = get_strategy(args.strategy)

    class_cache: dict[str, Any] = _load_json(class_cache_path, {})
    extracted_hashes: set[str] = set(_load_json(extracted_hashes_path, []))

    series_rows: list[dict[str, Any]] = []

    for k in range(1, harvest_cycles + 1):
        t0 = time.time()
        run_dir = prog_dir / f"run_{k:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        download_cap = (-1 if args.no_download_cap else baseline_cap)
        if args.adaptive_downloads:
            st0 = _load_json(adaptive_path, {})
            if int(st0.get("baseline_cap") or 0) != baseline_cap:
                download_cap = baseline_cap
            else:
                download_cap = max(baseline_cap, int(st0.get("next_cap") or baseline_cap))
            download_cap = min(int(args.adaptive_download_max), max(baseline_cap, download_cap))

        if args.self_cleanup:
            log_p = (out_root / str(args.cleanup_log_name).strip()) if str(args.cleanup_log_name).strip() else None
            run_openclaw_download_hygiene(
                out_root=out_root,
                download_dir=dl_dir,
                manifest_path=manifest_path,
                extracted_hashes_path=extracted_hashes_path,
                class_cache_path=class_cache_path,
                class_cache=class_cache,
                delete_extracted_pdfs=bool(args.cleanup_delete_extracted_pdfs),
                delete_classified_junk=bool(args.cleanup_delete_classified_junk),
                min_free_mb=float(args.min_free_mb),
                trim_log_path=log_p,
                trim_log_max_mb=float(args.trim_console_log_mb),
                trim_log_tail_lines=int(args.trim_console_log_tail),
            )
            extracted_hashes = set(_load_json(extracted_hashes_path, []))
            _save_json(class_cache_path, class_cache)

        print(
            f"progression_agentic: harvest_cycle {k}/{harvest_cycles} — discover (up to {int(args.crawl_max_pages)} pages); "
            f"download_cap={download_cap}{' (adaptive)' if args.adaptive_downloads else ''}…",
            flush=True,
        )

        disc = discover_pdfs(
            seed_urls=seeds,
            max_pages=int(args.crawl_max_pages),
            state_path=discover_state,
            allowed_hosts=None,
            include_pdf_regex=args.include_pdf_regex,
            exclude_pdf_regex=args.exclude_pdf_regex,
        )

        dl = download_pdfs(
            pdf_urls=disc.pdf_urls,
            out_dir=dl_dir,
            manifest_path=manifest_path,
            max_new_downloads=(None if int(download_cap) < 0 else int(download_cap)),
        )

        pdfs = sorted([p for p in dl_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
        classifications_run: list[dict[str, Any]] = []
        keep: list[Path] = []

        for p in pdfs:
            key = str(p.resolve())
            if key in class_cache:
                c = class_cache[key]
            else:
                c = classify_pdf(project_id=args.project_id, model=args.model, pdf_path=p)
                class_cache[key] = c
                _save_json(class_cache_path, class_cache)
            classifications_run.append(c)
            if (c.get("doc_type") == "full_application") and (c.get("year") in ("2026", "unknown")):
                keep.append(p)

        (run_dir / "classifications_snapshot.json").write_text(
            json.dumps(classifications_run, indent=2),
            encoding="utf-8",
        )

        new_keep: list[Path] = []
        for p in keep:
            h = sha256_file(p)
            if h not in extracted_hashes:
                new_keep.append(p)

        results: list[ExtractedRow] = []
        recovery_stats: list[dict[str, Any]] = []
        for p in new_keep:
            r = strat.extract(project_id=args.project_id, model=args.model, pdf_path=p, max_pages=args.max_pages).row
            r.source_pdf_sha256 = sha256_file(p)
            for i in range(max(0, int(args.recovery_iters))):
                if not _missing_fields(r):
                    break
                r, st = _recover_missing_fields(
                    project_id=args.project_id,
                    model=args.model,
                    pdf_path=p,
                    row=r,
                    max_pages_scan=int(args.recovery_max_pages),
                    page_budget=int(args.recovery_page_budget),
                    widen=(i > 0),
                )
                recovery_stats.append({"pdf": p.name, "iter": i, **st})
            results.append(r)
            extracted_hashes.add(r.source_pdf_sha256)

        if results:
            summary = write_outputs(
                out_dir=run_dir / "extraction",
                rows=results,
                project_id=args.project_id,
                model=args.model,
                max_pages=args.max_pages,
            )
        else:
            summary = {"outputs": {}, "count_pdfs": 0, "count_needs_review": 0}

        _save_json(extracted_hashes_path, sorted(extracted_hashes))

        cumulative_rows = _load_cumulative_rows(cumulative_jsonl)
        cumulative_rows.extend(results)
        _write_cumulative(
            jsonl_path=cumulative_jsonl,
            aggregate_dir=aggregate_dir,
            rows=cumulative_rows,
            project_id=args.project_id,
            model=args.model,
            max_pages=args.max_pages,
        )

        elapsed = round(time.time() - t0, 2)
        still_missing: dict[str, int] = {f: 0 for f in TARGET_FIELDS}
        for r in results:
            for f in _missing_fields(r):
                still_missing[f] = still_missing.get(f, 0) + 1

        payload = {
            "harvest_cycle": k,
            "of_harvest_cycles": harvest_cycles,
            "run": k,
            "of_runs": harvest_cycles,
            "discovered_pdf_urls": len(disc.pdf_urls),
            "downloaded_new": dl.downloaded,
            "skipped_existing": dl.skipped_existing,
            "failed_downloads": dl.failed,
            "pdfs_on_disk": len(pdfs),
            "kept_full_applications_total": len(keep),
            "new_full_applications_extracted": len(results),
            "cumulative_unique_extracted": len(_load_cumulative_rows(cumulative_jsonl)),
            "download_cap_used": download_cap,
            "adaptive_downloads": bool(args.adaptive_downloads),
            "strategy": args.strategy,
            "elapsed_s": elapsed,
            "out_dir": str(run_dir),
            "aggregate_dir": str(aggregate_dir),
            "still_missing_field_counts_this_batch": still_missing,
            "recovery_stats": recovery_stats[:200],
        }

        if args.adaptive_downloads:
            mult = float(args.adaptive_download_multiplier)
            mx = int(args.adaptive_download_max)
            n_extracted = int(payload["new_full_applications_extracted"])
            n_keep_total = int(payload["kept_full_applications_total"])
            if n_extracted > 0:
                next_cap = baseline_cap
                reason = "extracted_reset_baseline"
            elif n_keep_total == 0:
                next_cap = min(mx, max(download_cap + 1, int(round(download_cap * mult))))
                reason = "zero_full_application_classifications"
            else:
                next_cap = min(mx, max(download_cap + 1, int(round(download_cap * 1.25))))
                reason = "keepers_exist_but_none_new_to_extract"
            _save_json(
                adaptive_path,
                {
                    "baseline_cap": baseline_cap,
                    "previous_cap": download_cap,
                    "next_cap": next_cap,
                    "harvest_cycle": k,
                    "run": k,
                    "reason": reason,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            payload["adaptive_next_cap"] = next_cap
            payload["adaptive_reason"] = reason

        (run_dir / "run_summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        log_audit_if_configured(
            project_id=args.project_id,
            actor_id="lihtc-agent",
            event_type="lihtc_progression_agentic",
            pipeline="lihtc_progression_agentic",
            no_supabase_log=args.no_supabase_log,
            require_supabase_log=args.require_supabase_log,
            payload=payload,
        )

        series_rows.append(payload)
        print(json.dumps(payload, indent=2), flush=True)

    _save_json(
        out_root / "agentic_progression_series.json",
        {
            "out_root": str(out_root),
            "harvest_cycles": series_rows,
            "runs": series_rows,
        },
    )
    print("ok cumulative →", aggregate_dir / "applications.xlsx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
