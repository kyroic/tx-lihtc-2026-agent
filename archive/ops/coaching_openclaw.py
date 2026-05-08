from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..strategies.openclaw_client import run_openclaw_coaching_append


def _read_diffs_sample(diffs_path: Path, limit: int = 80) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not diffs_path.is_file():
        return out
    with diffs_path.open(encoding="utf-8", errors="ignore") as f:
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


def build_openclaw_coaching_message(
    *,
    run_completed: int,
    runs_total: int,
    report: dict[str, Any],
    diffs_sample: list[dict[str, Any]],
) -> str:
    payload = {
        "task": (
            "You refine instructions for the NEXT Texas LIHTC 2026 PDF extraction run. "
            "The extractor appends your string to its system prompt. "
            "Respond with ONLY valid JSON (no markdown): "
            '{"coaching_append": "<string>"}. '
            "The string must be <= 5500 characters, plain text inside JSON.\n"
            "Emphasize:\n"
            "- Field intent vs common distractors (e.g. tiebreaker_school expects school name / site name when labels use names; "
            "walking distance alone may be wrong if the schema expects an identifiable school).\n"
            "- When label expects X but PDF only shows Y, instruct whether to leave empty, capture both, or prefer the closest match.\n"
            "- Short bullets per affected field; never invent PDF facts.\n"
        ),
        "completed_run_index": run_completed,
        "runs_total": runs_total,
        "eval_report": report,
        "sample_label_mismatches": diffs_sample,
    }
    return json.dumps(payload, ensure_ascii=False)


def write_coaching_for_next_run(
    *,
    diffs_path: Path,
    report_path: Path,
    out_path: Path,
    openclaw_agent: str,
    run_completed: int,
    runs_total: int,
) -> str:
    report: dict[str, Any] = {}
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    diffs_sample = _read_diffs_sample(diffs_path)
    msg = build_openclaw_coaching_message(
        run_completed=run_completed,
        runs_total=runs_total,
        report=report,
        diffs_sample=diffs_sample,
    )
    coaching = run_openclaw_coaching_append(agent=openclaw_agent, message=msg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if coaching.strip():
        out_path.write_text(coaching.strip(), encoding="utf-8")
    elif out_path.exists():
        out_path.unlink()
    return coaching
