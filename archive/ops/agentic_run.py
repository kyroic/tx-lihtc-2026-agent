from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ..discover import discover_pdfs, DEFAULT_SEED_URLS
from ..download import download_pdfs
from ..extract import (
    ExtractedRow,
    build_page_hints,
    coaching_append_from_env,
    field_from_obj,
    norm_ws,
    read_pdf_pages,
    sha256_file,
    write_outputs,
)
from ..model_client import chat_completions, extract_json_content
from ..ops.supabase import log_audit_if_configured
from ..strategies.registry import get_strategy
from .classify_pdfs import classify_pdf

TARGET_FIELDS = [
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


def _missing_fields(row: ExtractedRow) -> list[str]:
    missing: list[str] = []
    for f in TARGET_FIELDS:
        v = getattr(row, f).value.strip()
        if not v:
            missing.append(f)
    return missing


def _hint_pages_for_missing(missing: list[str], hints: dict[str, list[int]]) -> list[int]:
    keys: list[str] = []
    for f in missing:
        keys.append(f)
        if f in ("census_tract", "quartile", "property_rate", "poverty_rank"):
            keys.extend(["site", "scoring"])
        if f.startswith("tiebreaker_"):
            keys.extend(["scoring", "site"])
    pages: list[int] = []
    seen: set[int] = set()
    for k in keys:
        for p in hints.get(k, []) or []:
            if p not in seen:
                seen.add(p)
                pages.append(int(p))
    return pages


def _select_recovery_pages(
    *,
    all_pages: list[dict[str, Any]],
    hinted: list[int],
    budget: int,
    widen: bool,
) -> list[dict[str, Any]]:
    by_page = {int(p["page"]): p for p in all_pages if p.get("page") is not None}
    chosen: list[int] = []
    for p in hinted:
        if p in by_page and p not in chosen:
            chosen.append(p)
        if len(chosen) >= budget:
            return [by_page[x] for x in chosen]

    if widen:
        step = max(1, len(all_pages) // max(1, budget))
        for p in range(1, len(all_pages) + 1, step):
            if p in by_page and p not in chosen:
                chosen.append(p)
            if len(chosen) >= budget:
                break

    for p in sorted(by_page.keys()):
        if p not in chosen:
            chosen.append(p)
        if len(chosen) >= budget:
            break

    return [by_page[p] for p in chosen if p in by_page]


def _pages_payload(pages: list[dict[str, Any]], text_limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in pages:
        t = str(p.get("text") or "")
        if len(t) > text_limit:
            t = t[:text_limit]
        out.append({"page": int(p.get("page") or 0), "text": norm_ws(t)})
    return out


def _recover_missing_fields(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    row: ExtractedRow,
    max_pages_scan: int,
    page_budget: int,
    widen: bool,
) -> tuple[ExtractedRow, dict[str, Any]]:
    missing0 = _missing_fields(row)
    if not missing0:
        return row, {"missing_before": [], "missing_after": [], "attempted": False}

    raw_pages = read_pdf_pages(pdf_path, max_pages=max_pages_scan)
    for p in raw_pages:
        t = str(p.get("text") or "")
        if len(t) > 8000:
            p["text"] = t[:8000]

    hints = build_page_hints(raw_pages)
    hinted = _hint_pages_for_missing(missing0, hints)
    selected = _select_recovery_pages(all_pages=raw_pages, hinted=hinted, budget=page_budget, widen=widen)
    pages_payload = _pages_payload(selected, text_limit=9000)

    system = (
        "You are filling ONLY missing fields for a Texas LIHTC application PDF.\n"
        "Return ONLY JSON: an object whose keys are field names and values are objects "
        '{ "value": "", "confidence": 0.0, "pages": [], "quote": "" }.\n'
        "Rules:\n"
        "- Only include keys for fields listed in missing_fields.\n"
        "- Never invent values; if still not present, use value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a verbatim quote from the provided page texts.\n"
    ) + coaching_append_from_env()
    user = json.dumps(
        {
            "pdf_filename": pdf_path.name,
            "missing_fields": missing0,
            "page_hints": {k: hints.get(k, []) for k in sorted(hints.keys())},
            "pages": pages_payload,
        },
        ensure_ascii=False,
    )

    try:
        resp = chat_completions(
            project_id=project_id,
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            timeout_s=240,
        )
        patch = extract_json_content(resp)
    except Exception as e:
        return row, {
            "missing_before": missing0,
            "missing_after": missing0,
            "attempted": True,
            "error": str(e)[:500],
            "recovery_pages_sent": len(pages_payload),
        }

    if not isinstance(patch, dict):
        return row, {
            "missing_before": missing0,
            "missing_after": missing0,
            "attempted": True,
            "error": "bad_patch_shape",
            "recovery_pages_sent": len(pages_payload),
        }

    for k, v in patch.items():
        if k not in TARGET_FIELDS:
            continue
        fe = field_from_obj(v if isinstance(v, dict) else {})
        if fe.value.strip():
            setattr(row, k, fe)

    missing1 = _missing_fields(row)
    return row, {
        "missing_before": missing0,
        "missing_after": missing1,
        "attempted": True,
        "recovery_pages_sent": len(pages_payload),
        "widened_scan": widen,
    }


def main() -> int:
    """
    Agentic runner:
    - Discover many PDFs
    - Download (optionally unlimited)
    - Classify PDFs to keep "2026 full_application"
    - Run extraction on those
    - Optional bounded recovery pass for still-empty fields
    - Log to Supabase
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--download-dir", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="llm_page_router_then_extract")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--seed-url", action="append", default=[])
    ap.add_argument("--crawl-max-pages", type=int, default=200)
    ap.add_argument("--include-pdf-regex", default="2026")
    ap.add_argument("--exclude-pdf-regex", default="")
    ap.add_argument("--download-limit", type=int, default=0, help="0 = no limit")
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
    ap.add_argument("--recovery-iters", type=int, default=2, help="Recovery LLM passes per PDF (0 disables)")
    ap.add_argument("--recovery-max-pages", type=int, default=120, help="Max PDF pages scanned during recovery")
    ap.add_argument("--recovery-page-budget", type=int, default=34, help="Max pages of text sent to recovery LLM")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dl_dir = Path(args.download_dir).expanduser().resolve()
    dl_dir.mkdir(parents=True, exist_ok=True)

    seeds = args.seed_url or list(DEFAULT_SEED_URLS)
    disc = discover_pdfs(
        seed_urls=seeds,
        max_pages=args.crawl_max_pages,
        state_path=out_dir / "discover_state.json",
        allowed_hosts=None,
        include_pdf_regex=args.include_pdf_regex,
        exclude_pdf_regex=args.exclude_pdf_regex,
    )

    limit = None if int(args.download_limit) == 0 else int(args.download_limit)
    dl = download_pdfs(pdf_urls=disc.pdf_urls, out_dir=dl_dir, manifest_path=out_dir / "download_manifest.json", limit=limit)

    # Classify downloaded PDFs and keep likely 2026 full applications
    pdfs = sorted([p for p in dl_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    classifications = []
    keep = []
    for p in pdfs:
        c = classify_pdf(project_id=args.project_id, model=args.model, pdf_path=p)
        classifications.append(c)
        if (c.get("doc_type") == "full_application") and (c.get("year") in ("2026", "unknown")):
            keep.append(p)

    (out_dir / "classifications.json").write_text(json.dumps(classifications, indent=2), encoding="utf-8")

    strat = get_strategy(args.strategy)
    t0 = time.time()
    results: list[ExtractedRow] = []
    recovery_stats: list[dict[str, Any]] = []
    for p in keep:
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

    summary = write_outputs(
        out_dir=out_dir / "extraction",
        rows=results,
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
        "discovered_pdf_urls": len(disc.pdf_urls),
        "downloaded": dl.downloaded,
        "failed": dl.failed,
        "classified": len(classifications),
        "kept_full_applications": len(keep),
        "strategy": args.strategy,
        "elapsed_s_extraction": elapsed,
        "outputs": summary.get("outputs"),
        "out_dir": str(out_dir),
        "recovery_iters": int(args.recovery_iters),
        "recovery_max_pages": int(args.recovery_max_pages),
        "recovery_page_budget": int(args.recovery_page_budget),
        "still_missing_field_counts": still_missing,
        "pdfs_with_any_missing": sum(1 for r in results if bool(_missing_fields(r))),
        "recovery_stats": recovery_stats[:500],
    }

    (out_dir / "agentic_summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    log_audit_if_configured(
        project_id=args.project_id,
        actor_id="lihtc-agent",
        event_type="lihtc_agentic_run",
        pipeline="lihtc_agentic",
        no_supabase_log=args.no_supabase_log,
        require_supabase_log=args.require_supabase_log,
        payload=payload,
    )

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
