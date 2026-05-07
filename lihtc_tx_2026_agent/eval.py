from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .ops.supabase import log_audit_if_configured
from .strategies.registry import get_strategy, list_strategies


FIELDS = [
    "application_name",
    "contact_name",
    "contact_email",
    "contact_phone",
    "tiebreaker_park",
    "tiebreaker_school",
    "tiebreaker_grocery",
    "tiebreaker_library",
    "quartile",
    "property_rate",
    "poverty_rank",
    "census_tract",
]


def _load_label(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--labels-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--strategy", action="append", default=["llm_single_pass"])
    ap.add_argument("--list-strategies", action="store_true")
    ap.add_argument(
        "--no-supabase-log",
        action="store_true",
        help="Do not write eval results to Supabase (used when a parent tool logs the combined run)",
    )
    ap.add_argument(
        "--require-supabase-log",
        action="store_true",
        help="Exit with error if Supabase is not configured or logging is disabled",
    )
    ap.add_argument(
        "--coaching-file",
        default="",
        help="UTF-8 text appended to extraction system prompts for this process (LIHTC_COACHING_APPEND)",
    )
    args = ap.parse_args()

    if args.list_strategies:
        print("\n".join(list_strategies()))
        return 0

    if str(args.coaching_file).strip():
        cp = Path(args.coaching_file).expanduser().resolve()
        if cp.is_file() and cp.stat().st_size > 0:
            os.environ["LIHTC_COACHING_APPEND"] = cp.read_text(encoding="utf-8", errors="ignore")[:20000]
    else:
        os.environ.pop("LIHTC_COACHING_APPEND", None)

    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    labels_dir = Path(args.labels_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    diffs_path = out_dir / "diffs.jsonl"
    report_path = out_dir / "report.json"

    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])

    # Evaluate each strategy independently.
    strat_names = args.strategy or ["llm_single_pass"]
    all_reports: dict[str, Any] = {}

    diffs_out = []
    for sname in strat_names:
        strat = get_strategy(sname)
        totals = {f: {"correct": 0, "total": 0} for f in FIELDS}
        diffs = []

        for pdf in pdfs:
            label_path = labels_dir / f"{pdf.stem}.json"
            if not label_path.exists():
                continue
            label = _load_label(label_path)
            pred = strat.extract(project_id=args.project_id, model=args.model, pdf_path=pdf, max_pages=args.max_pages).row

            for fld in FIELDS:
                exp = (label.get(fld) or "").strip()
                got = getattr(pred, fld).value.strip()
                totals[fld]["total"] += 1
                if exp == got:
                    totals[fld]["correct"] += 1
                else:
                    diffs.append({"strategy": sname, "pdf": pdf.name, "field": fld, "expected": exp, "got": got})

        report = {
            "strategy": sname,
            "accuracy": {f: (totals[f]["correct"] / totals[f]["total"] if totals[f]["total"] else None) for f in FIELDS},
            "counts": totals,
        }
        all_reports[sname] = report
        diffs_out.extend(diffs)

    report_path.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")
    with diffs_path.open("w", encoding="utf-8") as f:
        for d in diffs_out:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    log_audit_if_configured(
        project_id=args.project_id,
        actor_id="lihtc-agent",
        event_type="lihtc_eval",
        pipeline="lihtc_eval",
        no_supabase_log=args.no_supabase_log,
        require_supabase_log=args.require_supabase_log,
        payload={
            "pdf_dir": str(pdf_dir),
            "labels_dir": str(labels_dir),
            "out_dir": str(out_dir),
            "model": args.model,
            "max_pages": args.max_pages,
            "strategies": strat_names,
            "report": all_reports,
            "diffs_count": len(diffs_out),
        },
    )

    print(json.dumps(all_reports, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

