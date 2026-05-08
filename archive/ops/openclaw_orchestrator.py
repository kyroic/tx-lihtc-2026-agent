"""
OpenClaw-driven **planner loop** for TDHCA 2026 PDF harvesting.

Each iteration:
1. Build a **monitor** payload (metrics from the last cycle + cumulative state).
2. Ask OpenClaw (JSON-only) for the **next plan** (seeds, crawl budget, regex filters, download batch).
3. **Clamp** the plan to safe bounds, run one discover → download → classify → extract cycle (same
   workspace layout as ``progression_agentic``).

This is not a replacement for the deterministic pipeline: Python still executes tools. OpenClaw
chooses parameters so later iterations can react to a noisy URL list (e.g. raise batch size when
no ``full_application`` PDFs appear).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
import threading
import urllib.request
from typing import Any

from ..discover import DEFAULT_SEED_URLS, discover_pdfs
from ..download import download_pdfs
from ..extract import ExtractedRow, extracted_row_from_jsondict, sha256_file, write_outputs
from ..strategies.openclaw_client import ensure_openclaw, run_openclaw_agent
from ..strategies.registry import get_strategy
from .agentic_run import TARGET_FIELDS, _missing_fields, _recover_missing_fields
from .classify_pdfs import classify_pdf
from .openclaw_workspace_cleanup import disk_free_mb, run_openclaw_download_hygiene
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


def _write_cumulative(
    *,
    jsonl_path: Path,
    aggregate_dir: Path,
    rows: list[ExtractedRow],
    project_id: str,
    model: str,
    max_pages: int,
) -> None:
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


def _classify_histogram(classifications: list[dict[str, Any]]) -> dict[str, int]:
    h: dict[str, int] = {}
    for c in classifications:
        dt = str(c.get("doc_type") or "unknown")
        h[dt] = h.get(dt, 0) + 1
    return dict(sorted(h.items(), key=lambda kv: (-kv[1], kv[0])))


def _merge_seeds(
    *,
    base_seeds: list[str],
    plan_urls: list[str],
    replace_seeds: bool,
) -> list[str]:
    cleaned = [u.strip() for u in plan_urls if isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))]
    cleaned = cleaned[:24]
    if not cleaned:
        return list(base_seeds)
    if replace_seeds:
        return cleaned
    seen: set[str] = set()
    out: list[str] = []
    for u in cleaned + base_seeds:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _filter_working_seeds(urls: list[str]) -> list[str]:
    """
    Best-effort: drop broken seeds (e.g. 404) before crawling.
    Keeps the cycle from bricking when the planner suggests dead pages.
    """
    ok: list[str] = []
    for u in urls:
        u = str(u).strip()
        if not u:
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"}, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as r:
                code = getattr(r, "status", 200)
            if int(code) >= 400:
                continue
            ok.append(u)
            continue
        except Exception:
            pass
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                code = getattr(r, "status", 200)
                r.read(1)
            if int(code) >= 400:
                continue
            ok.append(u)
        except Exception:
            continue
    return ok


_TX_2026_NUM_RE = re.compile(r"/(?P<num>26\\d{3})\\.pdf(?:$|\\?)", re.I)


def _build_intake_manifest_2026(*, seed_urls: list[str], out_path: Path, crawl_max_pages: int) -> dict[str, Any]:
    """
    Intake step: ensure there is a stable, persisted list of candidate 2026 application PDFs.

    Today this is implemented as:
    - crawl seed_urls for PDF links (URL-level include/exclude)
    - infer numeric PDF patterns (26xxx.pdf) and directories from discovered URLs
    - persist the candidate list to out_path

    This makes the *application list* explicit and stable across runs, instead of being implicit in a crawl batch.
    """
    disc = discover_pdfs(
        seed_urls=seed_urls,
        max_pages=int(crawl_max_pages),
        state_path=out_path.parent / "intake_discover_state.json",
        allowed_hosts=None,
        include_pdf_regex="2026",
        exclude_pdf_regex="Appraisals",
        fetch_timeout_s=15,
    )
    urls = list(disc.pdf_urls)

    # Try to sort numerically when URLs look like /26xxx.pdf
    def sort_key(u: str) -> tuple[int, str]:
        m = _TX_2026_NUM_RE.search(u)
        if not m:
            return (10**9, u)
        return (int(m.group("num")), u)

    urls_sorted = sorted(set(urls), key=sort_key)
    payload = {
        "project_id": "lihtc-tx-2026",
        "kind": "intake_manifest",
        "year": "2026",
        "seed_urls": list(seed_urls),
        "crawled_pages": int(disc.crawled_pages),
        "candidate_pdf_urls": urls_sorted,
        "candidate_count": len(urls_sorted),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _clamp_plan(
    raw: dict[str, Any],
    *,
    default_crawl: int,
    default_include: str,
    default_exclude: str,
    default_downloads: int,
    cap_crawl: int,
    cap_downloads: int,
    min_crawl: int,
    no_download_cap: bool,
) -> dict[str, Any]:
    crawl = int(raw.get("crawl_max_pages") or default_crawl)
    crawl = max(min_crawl, min(cap_crawl, crawl))

    dl = int(raw.get("max_new_downloads") or default_downloads)
    if no_download_cap:
        dl = max(1, dl)
    else:
        dl = max(1, min(cap_downloads, dl))

    inc = str(raw.get("include_pdf_regex") or default_include).strip() or default_include
    exc = str(raw.get("exclude_pdf_regex") or default_exclude).strip() or default_exclude
    if len(inc) > 240:
        inc = inc[:240]
    if len(exc) > 240:
        exc = exc[:240]

    seeds_in = raw.get("seed_urls")
    if not isinstance(seeds_in, list):
        seeds_in = []
    replace = bool(raw.get("replace_seeds"))

    rationale = str(raw.get("rationale") or raw.get("notes") or "")[:2000]

    out: dict[str, Any] = {
        "crawl_max_pages": crawl,
        "max_new_downloads": dl,
        "include_pdf_regex": inc,
        "exclude_pdf_regex": exc,
        "seed_urls": [str(x).strip() for x in seeds_in if isinstance(x, str) and x.strip()],
        "replace_seeds": replace,
        "rationale": rationale,
    }
    cdel = raw.get("cleanup_delete_extracted_pdfs")
    if cdel is not None:
        out["cleanup_delete_extracted_pdfs"] = bool(cdel)
    cm = raw.get("cleanup_min_free_mb")
    if cm is not None:
        try:
            out["cleanup_min_free_mb"] = max(256.0, min(16000.0, float(cm)))
        except Exception:
            pass
    cjunk = raw.get("cleanup_delete_classified_junk")
    if cjunk is not None:
        out["cleanup_delete_classified_junk"] = bool(cjunk)
    return out


def _effective_hygiene(plan: dict[str, Any] | None, args: argparse.Namespace) -> dict[str, Any]:
    """Merge CLI defaults with optional OpenClaw plan overrides from the previous iteration."""
    eff = {
        "delete_extracted": bool(args.cleanup_delete_extracted_pdfs),
        "delete_junk": bool(args.cleanup_delete_classified_junk),
        "min_free_mb": float(args.min_free_mb),
    }
    if not plan:
        return eff
    if plan.get("cleanup_delete_extracted_pdfs") is not None:
        eff["delete_extracted"] = bool(plan["cleanup_delete_extracted_pdfs"])
    if plan.get("cleanup_delete_classified_junk") is not None:
        eff["delete_junk"] = bool(plan["cleanup_delete_classified_junk"])
    if plan.get("cleanup_min_free_mb") is not None:
        try:
            eff["min_free_mb"] = max(64.0, min(16000.0, float(plan["cleanup_min_free_mb"])))
        except Exception:
            pass
    return eff


def _heuristic_plan(
    *,
    last_summary: dict[str, Any] | None,
    baseline_downloads: int,
    cap_downloads: int,
    default_crawl: int,
    cap_crawl: int,
    mult_zero_keepers: float,
    mult_keepers_no_extract: float,
) -> dict[str, Any]:
    """Local fallback when OpenClaw is skipped or fails (same spirit as adaptive downloads)."""
    if not last_summary:
        return {
            "crawl_max_pages": default_crawl,
            "max_new_downloads": baseline_downloads,
            "include_pdf_regex": "2026",
            "exclude_pdf_regex": "Appraisals",
            "seed_urls": [],
            "replace_seeds": False,
            "rationale": "heuristic_bootstrap",
        }
    prev_dl = int(last_summary.get("download_cap_used") or baseline_downloads)
    n_extracted = int(last_summary.get("new_full_applications_extracted") or 0)
    n_keep = int(last_summary.get("kept_full_applications_total") or 0)
    if n_extracted > 0:
        next_dl = baseline_downloads
        reason = "heuristic_reset_after_extract"
    elif n_keep == 0:
        next_dl = min(cap_downloads, max(prev_dl + 1, int(round(prev_dl * mult_zero_keepers))))
        reason = "heuristic_zero_full_application"
    else:
        next_dl = min(cap_downloads, max(prev_dl + 1, int(round(prev_dl * mult_keepers_no_extract))))
        reason = "heuristic_keepers_no_new_extract"
    crawl = int(last_summary.get("crawl_max_pages_used") or default_crawl)
    crawl = min(cap_crawl, max(crawl, default_crawl))
    return {
        "crawl_max_pages": crawl,
        "max_new_downloads": next_dl,
        "include_pdf_regex": str(last_summary.get("include_pdf_regex_used") or "2026"),
        "exclude_pdf_regex": str(last_summary.get("exclude_pdf_regex_used") or "Appraisals"),
        "seed_urls": [],
        "replace_seeds": False,
        "rationale": reason,
    }


def _build_planner_message(
    *,
    iteration: int,
    iterations_total: int,
    monitor: dict[str, Any],
    caps: dict[str, Any],
) -> str:
    task = (
        "You steer the NEXT cycle of a Texas TDHCA LIHTC website harvest (2026 PDFs).\n"
        "We crawl seed pages for PDF links, download a batch, classify each PDF, and extract rows "
        "for doc_type full_application (year 2026 or unknown).\n"
        "There is no official JSON API for a master list: discovery is link-following from seeds.\n"
        "Return ONLY valid JSON (no markdown fences, no commentary) with exactly these keys:\n"
        "{\n"
        '  "seed_urls": [],\n'
        '  "replace_seeds": false,\n'
        '  "crawl_max_pages": 500,\n'
        '  "include_pdf_regex": "2026",\n'
        '  "exclude_pdf_regex": "Appraisals",\n'
        '  "max_new_downloads": 20,\n'
        '  "rationale": "one sentence"\n'
        "}\n"
        "Rules:\n"
        "- Prefer raising max_new_downloads when the last cycle had many downloads but zero full_application "
        "classifications (noisy list); lower it after successful new extractions.\n"
        "- crawl_max_pages within the given caps; widen slightly if discovery seems stuck and URLs are few.\n"
        "- seed_urls: optional extra TDHCA pages that may list 2026 applications; use replace_seeds=true only "
        "if you are confident these seeds alone are sufficient.\n"
        "- include_pdf_regex / exclude_pdf_regex: keep tight enough to avoid junk but not so tight you drop real apps.\n"
        "Optional workspace hygiene (downloads folder under out-root):\n"
        '- cleanup_delete_extracted_pdfs: remove PDFs already in extracted_hashes (aggregate unchanged).\n'
        '- cleanup_delete_classified_junk: remove PDFs classified as non-keeper (not full_application 2026/unknown).\n'
        '- cleanup_min_free_mb: target free disk (MB). Monitor includes disk_free_mb and previous hygiene summary.\n'
    )
    payload = {
        "task": task,
        "iteration": iteration,
        "iterations_total": iterations_total,
        "caps": caps,
        "monitor": monitor,
    }
    return json.dumps(payload, ensure_ascii=False)


def _run_one_cycle(
    *,
    run_dir: Path,
    seeds: list[str],
    crawl_max_pages: int,
    include_pdf_regex: str,
    exclude_pdf_regex: str,
    max_new_downloads: int,
    discover_state: Path,
    manifest_path: Path,
    dl_dir: Path,
    class_cache_path: Path,
    class_cache: dict[str, Any],
    extracted_hashes: set[str],
    extracted_hashes_path: Path,
    cumulative_jsonl: Path,
    aggregate_dir: Path,
    project_id: str,
    model: str,
    strat: Any,
    max_pages: int,
    recovery_iters: int,
    recovery_max_pages: int,
    recovery_page_budget: int,
    download_timeout_s: int,
    download_max_bytes: int,
    parallel_downloads: int,
    parallel_pdfs: int,
    no_cap_max_downloads: int,
    no_cap_max_minutes: float,
    keep_only_extracted_pdfs: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    t0 = time.time()
    disc = discover_pdfs(
        seed_urls=seeds,
        max_pages=crawl_max_pages,
        state_path=discover_state,
        allowed_hosts=None,
        include_pdf_regex=include_pdf_regex,
        exclude_pdf_regex=exclude_pdf_regex,
        fetch_timeout_s=15,
    )
    classifications_run: list[dict[str, Any]] = []
    keep: list[Path] = []

    parallel_n = max(1, min(20, int(parallel_pdfs)))
    cache_lock = threading.Lock()

    def classify_one(p: Path) -> tuple[Path, dict[str, Any]]:
        key = str(p.resolve())
        with cache_lock:
            cached = class_cache.get(key)
        if cached is not None:
            return p, cached
        c = classify_pdf(project_id=project_id, model=model, pdf_path=p)
        with cache_lock:
            class_cache[key] = c
        return p, c

    # If max_new_downloads is negative, treat as "no cap" but do it in batches so
    # classification + cleanup can keep disk under control.
    no_cap = int(max_new_downloads) < 0
    batch_size = 20 if no_cap else int(max_new_downloads)
    total_dl = total_failed = total_skipped = 0
    no_cap_deadline_s = float(no_cap_max_minutes) * 60.0 if no_cap else 0.0
    no_cap_dl_limit = max(1, int(no_cap_max_downloads)) if no_cap else 0

    while True:
        if no_cap:
            if no_cap_deadline_s and (time.time() - t0) >= no_cap_deadline_s:
                break
            if no_cap_dl_limit and total_dl >= no_cap_dl_limit:
                break
        dl = download_pdfs(
            pdf_urls=disc.pdf_urls,
            out_dir=dl_dir,
            manifest_path=manifest_path,
            # In no-cap mode we still download in bounded batches so we can classify/cleanup between them.
            max_new_downloads=int(batch_size),
            timeout_s=int(download_timeout_s),
            max_bytes=int(download_max_bytes),
            parallel_downloads=int(parallel_downloads),
        )
        total_dl += int(dl.downloaded)
        total_failed += int(dl.failed)
        total_skipped += int(dl.skipped_existing)

        pdfs = sorted([p for p in dl_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
        if pdfs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_n) as ex:
                for p, c in ex.map(classify_one, pdfs):
                    classifications_run.append(c)
                    if (c.get("doc_type") == "full_application") and (c.get("year") in ("2026", "unknown")):
                        keep.append(p)
            _save_json(class_cache_path, class_cache)

        # Opportunistic junk cleanup inside the cycle (only in no-cap mode).
        if no_cap:
            for p in list(pdfs):
                key = str(p.resolve())
                c = class_cache.get(key) or {}
                is_keeper = (c.get("doc_type") == "full_application") and (c.get("year") in ("2026", "unknown"))
                if not is_keeper:
                    try:
                        p.unlink()
                    except Exception:
                        pass

        # Stop conditions:
        # - capped mode: one batch is the whole cycle
        # - no-cap mode: stop when there are no new downloads this batch, or we hit time/download limits
        if not no_cap:
            break
        if int(dl.downloaded) <= 0:
            break

    (run_dir / "classifications_snapshot.json").write_text(json.dumps(classifications_run, indent=2), encoding="utf-8")

    new_keep: list[Path] = []
    for p in keep:
        h = sha256_file(p)
        if h not in extracted_hashes:
            new_keep.append(p)

    results: list[ExtractedRow] = []
    recovery_stats: list[dict[str, Any]] = []

    def extract_one(p: Path) -> tuple[ExtractedRow, list[dict[str, Any]]]:
        r = strat.extract(project_id=project_id, model=model, pdf_path=p, max_pages=max_pages).row
        r.source_pdf_sha256 = sha256_file(p)
        local_stats: list[dict[str, Any]] = []
        for i in range(max(0, int(recovery_iters))):
            if not _missing_fields(r):
                break
            r, st = _recover_missing_fields(
                project_id=project_id,
                model=model,
                pdf_path=p,
                row=r,
                max_pages_scan=int(recovery_max_pages),
                page_budget=int(recovery_page_budget),
                widen=(i > 0),
            )
            local_stats.append({"pdf": p.name, "iter": i, **st})
        return r, local_stats

    if new_keep:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_n) as ex:
            for r, st in ex.map(extract_one, new_keep):
                recovery_stats.extend(st)
                results.append(r)
                extracted_hashes.add(r.source_pdf_sha256)

    if results:
        write_outputs(
            out_dir=run_dir / "extraction",
            rows=results,
            project_id=project_id,
            model=model,
            max_pages=max_pages,
        )

    _save_json(extracted_hashes_path, sorted(extracted_hashes))

    # Optional: after a cycle, keep only PDFs that actually produced extracted rows.
    # This is the strictest disk hygiene: it deletes even keepers that failed extraction.
    if bool(keep_only_extracted_pdfs):
        extracted_now = {r.source_pdf_sha256 for r in results if (r.source_pdf_sha256 or "").strip()}
        if extracted_now:
            for p in list(dl_dir.glob("*.pdf")):
                if not p.is_file() or p.stat().st_size <= 0:
                    continue
                try:
                    h = sha256_file(p)
                except Exception:
                    continue
                if h not in extracted_now:
                    try:
                        p.unlink()
                    except Exception:
                        pass

    cumulative_rows = _load_cumulative_rows(cumulative_jsonl)
    cumulative_rows.extend(results)
    _write_cumulative(
        jsonl_path=cumulative_jsonl,
        aggregate_dir=aggregate_dir,
        rows=cumulative_rows,
        project_id=project_id,
        model=model,
        max_pages=max_pages,
    )

    elapsed = round(time.time() - t0, 2)
    still_missing: dict[str, int] = {f: 0 for f in TARGET_FIELDS}
    for r in results:
        for f in _missing_fields(r):
            still_missing[f] = still_missing.get(f, 0) + 1

    hist = _classify_histogram(classifications_run)
    summary = {
        "discovered_pdf_urls": len(disc.pdf_urls),
        "downloaded_new": int(total_dl),
        "skipped_existing": int(total_skipped),
        "failed_downloads": int(total_failed),
        "pdfs_on_disk": len(pdfs),
        "kept_full_applications_total": len(keep),
        "new_full_applications_extracted": len(results),
        "cumulative_unique_extracted": len(_load_cumulative_rows(cumulative_jsonl)),
        "download_cap_used": max_new_downloads,
        "no_cap_limits": (
            {"max_downloads": int(no_cap_dl_limit), "max_minutes": float(no_cap_max_minutes)} if no_cap else None
        ),
        "crawl_max_pages_used": crawl_max_pages,
        "include_pdf_regex_used": include_pdf_regex,
        "exclude_pdf_regex_used": exclude_pdf_regex,
        "classify_histogram": hist,
        "elapsed_s": elapsed,
        "still_missing_field_counts_this_batch": still_missing,
        "recovery_stats": recovery_stats[:200],
        "seeds_used": seeds[:40],
    }
    return summary, classifications_run


def main() -> int:
    ap = argparse.ArgumentParser(
        description="OpenClaw planner loop: each iteration proposes crawl/download parameters; Python executes one cycle.",
    )
    ap.add_argument("--iterations", type=int, default=10, help="Planner cycles (each = discover+download+classify+extract)")
    ap.add_argument("--out-root", default=str(_default_out_root()), help="Workspace root (shared with progression_agentic)")
    ap.add_argument("--download-dir", default="", help="PDF dir (default: <out-root>/downloads)")
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="llm_page_router_then_extract")
    ap.add_argument("--seed-url", action="append", default=[], help="Extra seeds (merged with defaults unless plan replaces)")
    ap.add_argument("--default-crawl-max-pages", type=int, default=500)
    ap.add_argument("--default-max-new-downloads", type=int, default=15)
    ap.add_argument("--default-include-pdf-regex", default="2026")
    ap.add_argument("--default-exclude-pdf-regex", default="Appraisals")
    ap.add_argument("--cap-crawl-max-pages", type=int, default=1200, help="Hard max crawl pages per cycle")
    ap.add_argument("--cap-max-new-downloads", type=int, default=80, help="Hard max new downloads per cycle")
    ap.add_argument(
        "--no-download-cap",
        action="store_true",
        help="Disable max_new_downloads clamping; download all discovered PDFs each cycle (can be large).",
    )
    ap.add_argument("--min-crawl-max-pages", type=int, default=80)
    ap.add_argument("--openclaw-agent", default="", help="Default: env LIHTC_OPENCLAW_ORCHESTRATOR_AGENT or LIHTC_OPENCLAW_COACHING_AGENT or 'default'")
    ap.add_argument("--openclaw-timeout-s", type=int, default=900)
    ap.add_argument("--fallback-heuristic", action="store_true", help="Do not call OpenClaw; use local heuristic plans")
    ap.add_argument("--recovery-iters", type=int, default=2)
    ap.add_argument("--recovery-max-pages", type=int, default=120)
    ap.add_argument("--recovery-page-budget", type=int, default=34)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--download-timeout-s", type=int, default=30, help="Per-request timeout for PDF downloads.")
    ap.add_argument("--download-max-bytes", type=int, default=80_000_000, help="Skip PDFs larger than this many bytes.")
    ap.add_argument("--parallel-downloads", type=int, default=1, help="Parallelism for PDF downloads (max 32).")
    ap.add_argument("--parallel-pdfs", type=int, default=1, help="Parallelism for classify+extract (max 20).")
    ap.add_argument(
        "--no-cap-max-downloads",
        type=int,
        default=200,
        help="When --no-download-cap is set, stop after this many successful downloads (safety).",
    )
    ap.add_argument(
        "--no-cap-max-minutes",
        type=float,
        default=45.0,
        help="When --no-download-cap is set, stop after this many minutes (safety).",
    )
    ap.add_argument("--heuristic-mult-zero-keepers", type=float, default=1.5)
    ap.add_argument("--heuristic-mult-keepers-no-extract", type=float, default=1.25)
    ap.add_argument("--no-supabase-log", action="store_true")
    ap.add_argument("--require-supabase-log", action="store_true")
    ap.add_argument(
        "--self-cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Each iteration: prune redundant/junk downloads + disk trim (default: on). OpenClaw plan can tune.",
    )
    ap.add_argument(
        "--min-free-mb",
        type=float,
        default=float(os.environ.get("LIHTC_MIN_FREE_MB") or "2048"),
        help="Minimum free disk MB on out-root volume (after hygiene passes).",
    )
    ap.add_argument(
        "--cleanup-log-name",
        default="progression_agentic_console.log",
        help="Under out-root; empty disables log trim during hygiene.",
    )
    ap.add_argument("--trim-console-log-mb", type=float, default=80.0)
    ap.add_argument("--trim-console-log-tail", type=int, default=8000)
    ap.add_argument(
        "--cleanup-delete-extracted-pdfs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delete downloads whose SHA is already in extracted_hashes.json.",
    )
    ap.add_argument(
        "--cleanup-delete-classified-junk",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delete downloads classified as non-keeper (see classification_cache.json).",
    )
    ap.add_argument(
        "--keep-only-extracted-pdfs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After each cycle, delete all downloaded PDFs except those that produced extracted rows.",
    )
    args = ap.parse_args()

    out_root = Path(args.out_root).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    dl_dir = Path(args.download_dir).expanduser().resolve() if str(args.download_dir).strip() else (out_root / "downloads")
    dl_dir.mkdir(parents=True, exist_ok=True)

    orch_dir = out_root / "openclaw_orchestrator"
    orch_dir.mkdir(parents=True, exist_ok=True)
    discover_state = out_root / "discover_state.json"
    manifest_path = out_root / "download_manifest.json"
    intake_dir = out_root / "intake"
    intake_manifest_path = intake_dir / "applications_2026_manifest.json"
    class_cache_path = out_root / "classification_cache.json"
    extracted_hashes_path = out_root / "extracted_hashes.json"
    cumulative_jsonl = out_root / "all_applications.jsonl"
    aggregate_dir = out_root / "aggregate"
    state_path = out_root / "openclaw_orchestrator_state.json"

    base_seeds = list(args.seed_url) or list(DEFAULT_SEED_URLS)
    strat = get_strategy(args.strategy)

    class_cache: dict[str, Any] = _load_json(class_cache_path, {})
    extracted_hashes: set[str] = set(_load_json(extracted_hashes_path, []))

    # Intake-first (default): persist the canonical 2026 candidate list before downloading/extracting.
    # If this file exists, later runs reuse it (stable application list).
    if not intake_manifest_path.exists():
        _build_intake_manifest_2026(
            seed_urls=list(DEFAULT_SEED_URLS),
            out_path=intake_manifest_path,
            crawl_max_pages=int(args.cap_crawl_max_pages),
        )

    agent = (args.openclaw_agent or "").strip() or (
        os.environ.get("LIHTC_OPENCLAW_ORCHESTRATOR_AGENT")
        or os.environ.get("LIHTC_OPENCLAW_COACHING_AGENT")
        or "default"
    )

    caps = {
        "crawl_max_pages_min": int(args.min_crawl_max_pages),
        "crawl_max_pages_max": int(args.cap_crawl_max_pages),
        "max_new_downloads_max": int(args.cap_max_new_downloads),
        "note": "Planner JSON must respect these caps after clamping.",
    }

    series_rows: list[dict[str, Any]] = []
    last_summary: dict[str, Any] | None = None
    last_plan: dict[str, Any] | None = None
    last_hygiene_report: dict[str, Any] | None = None

    if not args.fallback_heuristic:
        try:
            ensure_openclaw()
        except RuntimeError as e:
            print(f"openclaw_orchestrator: OpenClaw unavailable ({e}); use --fallback-heuristic for local plans.", flush=True)
            return 2

    for k in range(1, int(args.iterations) + 1):
        t_iter = time.time()
        run_dir = orch_dir / f"run_{k:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)

        hygiene_report: dict[str, Any] | None = None
        if args.self_cleanup:
            eff = _effective_hygiene(last_plan, args)
            log_p = (out_root / str(args.cleanup_log_name).strip()) if str(args.cleanup_log_name).strip() else None
            hygiene_report = run_openclaw_download_hygiene(
                out_root=out_root,
                download_dir=dl_dir,
                manifest_path=manifest_path,
                extracted_hashes_path=extracted_hashes_path,
                class_cache_path=class_cache_path,
                class_cache=class_cache,
                delete_extracted_pdfs=eff["delete_extracted"],
                delete_classified_junk=eff["delete_junk"],
                min_free_mb=float(eff["min_free_mb"]),
                trim_log_path=log_p,
                trim_log_max_mb=float(args.trim_console_log_mb),
                trim_log_tail_lines=int(args.trim_console_log_tail),
            )
            extracted_hashes = set(_load_json(extracted_hashes_path, []))
            _save_json(class_cache_path, class_cache)
            er = int((hygiene_report.get("extracted_redundant") or {}).get("freed_bytes") or 0)
            jr = int((hygiene_report.get("classified_junk") or {}).get("freed_bytes") or 0)
            print(f"openclaw_orchestrator: hygiene iter={k} freed_bytes_extracted={er} freed_bytes_junk={jr}", flush=True)

        cumulative_n = len(_load_cumulative_rows(cumulative_jsonl))
        monitor: dict[str, Any] = {
            "iteration": k,
            "iterations_total": int(args.iterations),
            "cumulative_unique_extracted_before": cumulative_n,
            "disk_free_mb": disk_free_mb(out_root),
            "last_workspace_hygiene": last_hygiene_report,
            "hygiene_policy_resolved": _effective_hygiene(last_plan, args) if args.self_cleanup else None,
            "base_seed_urls": base_seeds,
            "last_cycle_summary": last_summary,
            "last_plan_applied": last_plan,
            "defaults": {
                "crawl_max_pages": int(args.default_crawl_max_pages),
                "max_new_downloads": int(args.default_max_new_downloads),
                "include_pdf_regex": str(args.default_include_pdf_regex),
                "exclude_pdf_regex": str(args.default_exclude_pdf_regex),
            },
        }

        if args.fallback_heuristic:
            raw_plan = _heuristic_plan(
                last_summary=last_summary,
                baseline_downloads=int(args.default_max_new_downloads),
                cap_downloads=int(args.cap_max_new_downloads),
                default_crawl=int(args.default_crawl_max_pages),
                cap_crawl=int(args.cap_crawl_max_pages),
                mult_zero_keepers=float(args.heuristic_mult_zero_keepers),
                mult_keepers_no_extract=float(args.heuristic_mult_keepers_no_extract),
            )
            plan_source = "heuristic"
        else:
            msg = _build_planner_message(iteration=k, iterations_total=int(args.iterations), monitor=monitor, caps=caps)
            (run_dir / "openclaw_planner_input.json").write_text(msg, encoding="utf-8")
            try:
                raw_plan = run_openclaw_agent(agent=agent, message=msg, timeout_s=int(args.openclaw_timeout_s))
                plan_source = "openclaw"
            except Exception as e:
                print(f"openclaw_orchestrator: planner failed ({e}); falling back to heuristic this iteration.", flush=True)
                raw_plan = _heuristic_plan(
                    last_summary=last_summary,
                    baseline_downloads=int(args.default_max_new_downloads),
                    cap_downloads=int(args.cap_max_new_downloads),
                    default_crawl=int(args.default_crawl_max_pages),
                    cap_crawl=int(args.cap_crawl_max_pages),
                    mult_zero_keepers=float(args.heuristic_mult_zero_keepers),
                    mult_keepers_no_extract=float(args.heuristic_mult_keepers_no_extract),
                )
                plan_source = "heuristic_after_openclaw_error"

        raw_dict = raw_plan if isinstance(raw_plan, dict) else {}
        plan = _clamp_plan(
            raw_dict,
            default_crawl=int(args.default_crawl_max_pages),
            default_include=str(args.default_include_pdf_regex),
            default_exclude=str(args.default_exclude_pdf_regex),
            default_downloads=int(args.default_max_new_downloads),
            cap_crawl=int(args.cap_crawl_max_pages),
            cap_downloads=int(args.cap_max_new_downloads),
            min_crawl=int(args.min_crawl_max_pages),
            no_download_cap=bool(args.no_download_cap),
        )
        seeds = _merge_seeds(
            base_seeds=base_seeds,
            plan_urls=plan.get("seed_urls") or [],
            replace_seeds=bool(plan.get("replace_seeds")),
        )
        if plan_source == "openclaw":
            seeds_ok = _filter_working_seeds(seeds)
            if seeds_ok:
                seeds = seeds_ok

        (run_dir / "plan_applied.json").write_text(
            json.dumps({**plan, "plan_source": plan_source, "raw_planner_keys": list(raw_dict.keys())}, indent=2),
            encoding="utf-8",
        )

        print(
            f"openclaw_orchestrator: run {k}/{int(args.iterations)} plan_source={plan_source} "
            f"crawl={plan['crawl_max_pages']} dl={plan['max_new_downloads']} seeds={len(seeds)} …",
            flush=True,
        )

        summary, _classifs = _run_one_cycle(
            run_dir=run_dir,
            seeds=seeds,
            crawl_max_pages=int(plan["crawl_max_pages"]),
            include_pdf_regex=str(plan["include_pdf_regex"]),
            exclude_pdf_regex=str(plan["exclude_pdf_regex"]),
            max_new_downloads=(-1 if bool(args.no_download_cap) else int(plan["max_new_downloads"])),
            discover_state=discover_state,
            manifest_path=manifest_path,
            dl_dir=dl_dir,
            class_cache_path=class_cache_path,
            class_cache=class_cache,
            extracted_hashes=extracted_hashes,
            extracted_hashes_path=extracted_hashes_path,
            cumulative_jsonl=cumulative_jsonl,
            aggregate_dir=aggregate_dir,
            project_id=args.project_id,
            model=args.model,
            strat=strat,
            max_pages=int(args.max_pages),
            recovery_iters=int(args.recovery_iters),
            recovery_max_pages=int(args.recovery_max_pages),
            recovery_page_budget=int(args.recovery_page_budget),
            download_timeout_s=int(args.download_timeout_s),
            download_max_bytes=int(args.download_max_bytes),
            parallel_downloads=int(args.parallel_downloads),
            parallel_pdfs=int(args.parallel_pdfs),
            no_cap_max_downloads=int(args.no_cap_max_downloads),
            no_cap_max_minutes=float(args.no_cap_max_minutes),
            keep_only_extracted_pdfs=bool(args.keep_only_extracted_pdfs),
        )

        payload = {
            "run": k,
            "of_iterations": int(args.iterations),
            "plan_source": plan_source,
            "plan": plan,
            "strategy": args.strategy,
            "workspace_hygiene": hygiene_report,
            "out_dir": str(run_dir),
            "aggregate_dir": str(aggregate_dir),
            "monitor_before": monitor,
            **summary,
            "iteration_elapsed_s": round(time.time() - t_iter, 2),
        }
        (run_dir / "run_summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        log_audit_if_configured(
            project_id=args.project_id,
            actor_id="lihtc-agent",
            event_type="lihtc_openclaw_orchestrator",
            pipeline="lihtc_openclaw_orchestrator",
            no_supabase_log=args.no_supabase_log,
            require_supabase_log=args.require_supabase_log,
            payload={k: v for k, v in payload.items() if k != "monitor_before"},
        )

        series_rows.append(payload)
        print(json.dumps({k: v for k, v in payload.items() if k != "monitor_before"}, indent=2), flush=True)

        last_summary = summary
        last_plan = plan
        last_hygiene_report = hygiene_report

        hist = _load_json(state_path, {"history": []})
        if not isinstance(hist, dict):
            hist = {"history": []}
        hist_history = hist.get("history")
        if not isinstance(hist_history, list):
            hist_history = []
        hist_history.append(
            {
                "run": k,
                "plan": plan,
                "plan_source": plan_source,
                "summary": {x: summary[x] for x in summary if x != "recovery_stats"},
            }
        )
        hist["history"] = hist_history[-25:]
        hist["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_json(state_path, hist)

    _save_json(orch_dir / "orchestrator_series.json", {"runs": series_rows, "out_root": str(out_root)})
    print("ok cumulative →", aggregate_dir / "applications.xlsx", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
