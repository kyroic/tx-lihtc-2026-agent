"""
Run N sequential evaluation passes (same PDFs + labels each time) to track
extraction quality over time — e.g. after prompt/code changes between batches.

Each pass writes to ``<out_root>/progression/run_KKK/`` (eval artifacts) plus
``run_KKK/extraction/applications.xlsx`` (one row per PDF, all required fields)
unless ``--skip-per-run-excel`` is set.

Aggregates ``progression.csv`` + ``progression_summary.json`` under ``out_root``.
Note: per-run Excel uses one extra full extract pass (see ``--excel-strategy``).

After each run (except the last), **OpenClaw** can write ``progression/coaching_for_next.txt``
from ``report.json`` + ``diffs.jsonl``; run *k+1* passes it as ``--coaching-file`` so the
extractor appends that coaching to its system prompt (unless ``--skip-openclaw-coaching``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from ..extract import write_outputs
from ..strategies.registry import get_strategy
from .coaching_openclaw import write_coaching_for_next_run
from .supabase import log_audit_if_configured

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_fixtures_pdf_dir() -> Path:
    return _REPO_ROOT / "fixtures" / "pdfs"


def _default_fixtures_labels_dir() -> Path:
    return _REPO_ROOT / "fixtures" / "labels"


def _default_out_root() -> Path:
    return Path.home() / "Desktop" / "parcell" / "agent"


def _mean_accuracy(block: dict[str, Any]) -> float | None:
    acc = block.get("accuracy") or {}
    vals = [float(v) for v in acc.values() if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 6)


def _read_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Repeat eval N times; record metrics for improvement tracking.")
    ap.add_argument(
        "--runs",
        type=int,
        default=20,
        help="Number of sequential eval passes (agent improvement on fixtures — not website harvest cycles; see progression_agentic --harvest-cycles).",
    )
    ap.add_argument(
        "--out-root",
        default=str(_default_out_root()),
        help="Desktop/parcell/agent by default; progression/ and summary files live here",
    )
    ap.add_argument(
        "--pdf-dir",
        default="",
        help="PDFs to evaluate (default: repo fixtures/pdfs if present)",
    )
    ap.add_argument(
        "--labels-dir",
        default="",
        help="Labels (default: repo fixtures/labels if present)",
    )
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument(
        "--strategy",
        action="append",
        default=[],
        help="Repeat for multiple strategies (default: llm_single_pass only, to limit API cost)",
    )
    ap.add_argument("--series-id", default="", help="Stable id across runs for dashboards (default: new UUID)")
    ap.add_argument(
        "--excel-strategy",
        default="",
        help="Strategy used for per-run applications.xlsx (default: first --strategy)",
    )
    ap.add_argument(
        "--skip-per-run-excel",
        action="store_true",
        help="Do not write extraction/applications.xlsx each run (saves API calls)",
    )
    ap.add_argument("--no-supabase-log", action="store_true")
    ap.add_argument("--require-supabase-log", action="store_true")
    ap.add_argument(
        "--openclaw-agent",
        default="",
        help="OpenClaw agent name for coaching (default: env LIHTC_OPENCLAW_COACHING_AGENT or 'default')",
    )
    ap.add_argument(
        "--skip-openclaw-coaching",
        action="store_true",
        help="Do not call OpenClaw between runs; no coaching_for_next.txt updates",
    )
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir).expanduser().resolve() if str(args.pdf_dir).strip() else _default_fixtures_pdf_dir()
    labels_dir = Path(args.labels_dir).expanduser().resolve() if str(args.labels_dir).strip() else _default_fixtures_labels_dir()
    if not pdf_dir.is_dir() or not labels_dir.is_dir():
        raise SystemExit(f"Missing --pdf-dir / --labels-dir (or defaults): pdf_dir={pdf_dir} labels_dir={labels_dir}")

    strategies = args.strategy or ["llm_single_pass"]
    out_root = Path(args.out_root).expanduser().resolve()
    prog_root = out_root / "progression"
    prog_root.mkdir(parents=True, exist_ok=True)

    series_id = (args.series_id or "").strip() or str(uuid.uuid4())
    openclaw_agent = (args.openclaw_agent or "").strip() or (
        os.environ.get("LIHTC_OPENCLAW_COACHING_AGENT") or "default"
    ).strip()
    series_meta = {
        "series_id": series_id,
        "runs": int(args.runs),
        "project_id": args.project_id,
        "pdf_dir": str(pdf_dir),
        "labels_dir": str(labels_dir),
        "strategies": strategies,
        "model": args.model,
        "max_pages": args.max_pages,
        "per_run_excel": not args.skip_per_run_excel,
        "excel_strategy_default": (args.excel_strategy or strategies[0]).strip(),
        "openclaw_coaching": not args.skip_openclaw_coaching,
        "openclaw_agent": openclaw_agent,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_root / "progression_series.json").write_text(json.dumps(series_meta, indent=2), encoding="utf-8")

    run_rows: list[dict[str, Any]] = []
    csv_fieldnames: list[str] = ["run", "elapsed_s", "iso_time", "applications_xlsx", *strategies]

    coaching_next = prog_root / "coaching_for_next.txt"

    for k in range(1, int(args.runs) + 1):
        t0 = time.time()
        os.environ.pop("LIHTC_COACHING_APPEND", None)
        run_dir = prog_root / f"run_{k:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        applications_xlsx = ""
        cmd = [
            "python3",
            "-m",
            "lihtc_tx_2026_agent.eval",
            "--pdf-dir",
            str(pdf_dir),
            "--labels-dir",
            str(labels_dir),
            "--out-dir",
            str(run_dir),
            "--project-id",
            args.project_id,
            "--model",
            args.model,
            "--max-pages",
            str(args.max_pages),
            "--no-supabase-log",
        ]
        for s in strategies:
            cmd.extend(["--strategy", s])
        if k > 1 and coaching_next.is_file() and coaching_next.stat().st_size > 0:
            cmd.extend(["--coaching-file", str(coaching_next)])
        subprocess.check_call(cmd)
        if k > 1 and coaching_next.is_file() and coaching_next.stat().st_size > 0:
            shutil.copy2(coaching_next, run_dir / "coaching_applied_this_run.txt")

        if not args.skip_per_run_excel:
            if k > 1 and coaching_next.is_file() and coaching_next.stat().st_size > 0:
                os.environ["LIHTC_COACHING_APPEND"] = coaching_next.read_text(encoding="utf-8", errors="ignore")[
                    :20000
                ]
            excel_strat = (args.excel_strategy or strategies[0]).strip()
            strat = get_strategy(excel_strat)
            pdfs_sorted = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
            extract_dir = run_dir / "extraction"
            rows = [
                strat.extract(project_id=args.project_id, model=args.model, pdf_path=p, max_pages=args.max_pages).row
                for p in pdfs_sorted
            ]
            wsum = write_outputs(
                out_dir=extract_dir,
                rows=rows,
                project_id=args.project_id,
                model=args.model,
                max_pages=args.max_pages,
            )
            applications_xlsx = str(Path(wsum["outputs"]["applications_xlsx"]).resolve())
        os.environ.pop("LIHTC_COACHING_APPEND", None)

        elapsed = round(time.time() - t0, 2)
        report = _read_report(run_dir / "report.json")
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        row: dict[str, Any] = {
            "run": k,
            "elapsed_s": elapsed,
            "iso_time": iso,
            "applications_xlsx": applications_xlsx,
        }
        payload_report: dict[str, Any] = {}
        for sname in strategies:
            block = report.get(sname) or {}
            m = _mean_accuracy(block)
            row[sname] = m
            payload_report[sname] = {"mean_accuracy": m, "accuracy": block.get("accuracy"), "counts": block.get("counts")}

        run_rows.append(row)

        log_audit_if_configured(
            project_id=args.project_id,
            actor_id="lihtc-agent-loop",
            event_type="lihtc_progression_run",
            pipeline="lihtc_progression",
            no_supabase_log=args.no_supabase_log,
            require_supabase_log=args.require_supabase_log,
            payload={
                "series_id": series_id,
                "run": k,
                "of_runs": int(args.runs),
                "elapsed_s": elapsed,
                "out_dir": str(run_dir),
                "applications_xlsx": applications_xlsx or None,
                "excel_strategy": (args.excel_strategy or strategies[0]).strip() if not args.skip_per_run_excel else None,
                "coaching_file_used": str(coaching_next)
                if (k > 1 and coaching_next.is_file() and coaching_next.stat().st_size > 0)
                else None,
                "report": payload_report,
            },
        )
        msg = f"progression run {k}/{args.runs} ok in {elapsed}s → {run_dir}"
        if applications_xlsx:
            msg += f"\n  applications.xlsx → {applications_xlsx}"
        if k > 1 and coaching_next.is_file() and coaching_next.stat().st_size > 0:
            msg += f"\n  coaching applied from → {coaching_next}"
        print(msg, flush=True)

        if not args.skip_openclaw_coaching and k < int(args.runs):
            try:
                write_coaching_for_next_run(
                    diffs_path=run_dir / "diffs.jsonl",
                    report_path=run_dir / "report.json",
                    out_path=coaching_next,
                    openclaw_agent=openclaw_agent,
                    run_completed=k,
                    runs_total=int(args.runs),
                )
                sz = coaching_next.stat().st_size if coaching_next.is_file() else 0
                print(f"  openclaw coaching_for_next → {coaching_next} ({sz} bytes)", flush=True)
            except Exception as e:
                print(f"  openclaw coaching failed: {e}", flush=True)
                try:
                    coaching_next.unlink()
                except Exception:
                    pass

    summary = {
        "series_id": series_id,
        "series": series_meta,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "by_run": run_rows,
    }
    (out_root / "progression_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    csv_path = out_root / "progression.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in run_rows:
            w.writerow(r)

    print("wrote:", csv_path)
    print("wrote:", out_root / "progression_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
