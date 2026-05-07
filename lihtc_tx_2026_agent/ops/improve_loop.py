from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .supabase import enqueue_task_packet, load_supabase_config, log_audit_if_configured


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path, limit: int = 5000) -> list[dict[str, Any]]:
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _top_improvements(diffs: list[dict[str, Any]], top_n: int = 12) -> dict[str, Any]:
    by_field = Counter(d.get("field") for d in diffs if d.get("field"))
    by_strategy = Counter(d.get("strategy") for d in diffs if d.get("strategy"))
    return {
        "diffs_count": len(diffs),
        "top_fields": by_field.most_common(top_n),
        "top_strategies": by_strategy.most_common(top_n),
        "sample_diffs": diffs[: min(30, len(diffs))],
    }


def _agentic_objective_addon(summary_path: Path | None) -> str:
    if not summary_path:
        return ""
    p = summary_path.expanduser().resolve()
    if not p.exists():
        return ""
    try:
        s = _read_json(p)
    except Exception:
        return f"\n(agentic_summary.json present but unreadable: {p})\n"
    lines: list[str] = ["\nLatest agentic run gaps (post-extraction):\n"]
    sm = s.get("still_missing_field_counts") or {}
    if isinstance(sm, dict) and sm:
        top = sorted(((k, int(v)) for k, v in sm.items() if int(v or 0) > 0), key=lambda x: -x[1])[:12]
        if top:
            lines.append("Still-empty field counts across PDFs:")
            lines.extend([f"- {k}: {n}" for k, n in top])
        else:
            lines.append("No aggregate still-empty counts reported (or all zero).")
    pdf_missing = s.get("pdfs_with_any_missing")
    if pdf_missing is not None:
        lines.append(f"PDFs with any missing field: {pdf_missing}")
    lines.append(
        "Coder focus: improve retrieval for census tract / quartile / poverty rank / tie-breakers; "
        "consider OCR for scanned pages, widen label synonyms, and merge multi-document evidence.\n"
    )
    lines.append(f"(agentic_summary.json: {p})\n")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--labels-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--strategy", action="append", required=True, help="Repeat for multiple strategies")
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
    ap.add_argument("--enqueue-fix-task", action="store_true")
    ap.add_argument(
        "--agentic-summary",
        default="",
        help="Optional path to agentic_summary.json; merged into fix-task objective when enqueuing",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Run eval
    cmd = [
        "python3",
        "-m",
        "lihtc_tx_2026_agent.eval",
        "--pdf-dir",
        str(Path(args.pdf_dir).expanduser().resolve()),
        "--labels-dir",
        str(Path(args.labels_dir).expanduser().resolve()),
        "--out-dir",
        str(out_dir),
        "--project-id",
        args.project_id,
        "--model",
        args.model,
        "--max-pages",
        str(args.max_pages),
    ]
    for s in args.strategy:
        cmd.extend(["--strategy", s])
    cmd.append("--no-supabase-log")

    subprocess.check_call(cmd)

    # 2) Summarize mistakes
    report = _read_json(out_dir / "report.json")
    diffs = _read_jsonl(out_dir / "diffs.jsonl")
    improvements = _top_improvements(diffs)
    (out_dir / "improvements.json").write_text(json.dumps(improvements, indent=2), encoding="utf-8")

    # 3) Log to Supabase (automatic when env configured; eval subprocess skips to avoid duplicate rows)
    cfg = load_supabase_config()
    agentic_summary_path = Path(args.agentic_summary) if str(args.agentic_summary or "").strip() else None
    payload_eval: dict[str, Any] = {
        "model": args.model,
        "max_pages": args.max_pages,
        "strategies": args.strategy,
        "report": report,
        "improvements": {k: improvements[k] for k in ("diffs_count", "top_fields", "top_strategies")},
        "out_dir": str(out_dir),
    }
    if agentic_summary_path:
        p = agentic_summary_path.expanduser().resolve()
        if p.exists():
            try:
                payload_eval["agentic_summary"] = _read_json(p)
            except Exception as e:
                payload_eval["agentic_summary_error"] = str(e)[:500]
    log_audit_if_configured(
        project_id=args.project_id,
        actor_id="lihtc-agent-loop",
        event_type="lihtc_eval_run",
        pipeline="lihtc_improve_loop",
        no_supabase_log=args.no_supabase_log,
        require_supabase_log=args.require_supabase_log,
        payload=payload_eval,
    )

    # 4) Enqueue “improve code” task (optional)
    if args.enqueue_fix_task:
        if not cfg:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required for --enqueue-fix-task")
        objective = (
            "Improve LIHTC TX 2026 extraction agent accuracy based on latest eval diffs.\n\n"
            "Top failing fields:\n"
            + "\n".join(f"- {f}: {n}" for f, n in improvements["top_fields"][:10])
            + "\n\n"
            "Work items:\n"
            "- Update prompts/field normalization in extraction.\n"
            "- If needed, adjust strategy implementations.\n"
            "- Keep outputs stable and schema-bound with evidence.\n"
            f"\nEval artifacts are in: {out_dir}\n"
            + _agentic_objective_addon(agentic_summary_path)
        )
        try:
            packet_id = enqueue_task_packet(
                cfg=cfg,
                project_id=args.project_id,
                objective=objective,
                task_type="refactor",
                required_capabilities=["python", "file_edit"],
            )
            log_audit_if_configured(
                project_id=args.project_id,
                actor_id="lihtc-agent-loop",
                event_type="lihtc_fix_task_enqueued",
                pipeline="lihtc_improve_loop",
                no_supabase_log=args.no_supabase_log,
                require_supabase_log=False,
                payload={"packet_id": packet_id, "objective_preview": objective[:500]},
            )
            print("enqueued_task_packet_id:", packet_id)
        except Exception as e:
            # Some Supabase deployments don't expose the task_packet table via PostgREST.
            # We still log the intended objective so an external coordinator/agent can pick it up.
            log_audit_if_configured(
                project_id=args.project_id,
                actor_id="lihtc-agent-loop",
                event_type="lihtc_fix_task_enqueue_failed",
                pipeline="lihtc_improve_loop",
                no_supabase_log=args.no_supabase_log,
                require_supabase_log=False,
                payload={"error": str(e)[:800], "objective_preview": objective[:800]},
            )
            print("enqueue_failed:", str(e)[:300])

    print("ok wrote:", str(out_dir / "improvements.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

