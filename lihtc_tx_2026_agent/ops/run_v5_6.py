#!/usr/bin/env python3
"""
V5.6 = AI target-set selection + unchanged V5.5 extraction.

What changes from V5.5:
- AI decides which discovered PDF folder(s) correspond to FULL APPLICATIONS.

What stays the same:
- Extraction strategy is unchanged: v5_5_chunked_tiebreaker.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from ..discover import DEFAULT_SEED_URLS, discover_pdfs
from ..download import download_pdfs
from ..extract import ExtractedRow, sha256_file, write_outputs
from ..model_client import chat_completions, extract_json_content
from ..strategies.registry import get_strategy


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _fetch_text(url: str, timeout_s: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="ignore")


def _extract_imaged_folder(url: str) -> str:
    # .../imaged/<folder>/<file.pdf>
    m = re.search(r"/imaged/([^/]+)/[^/]+\.pdf", url, re.I)
    return m.group(1) if m else "(other)"


def _bucket_urls(pdf_urls: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for u in pdf_urls:
        f = _extract_imaged_folder(u)
        buckets.setdefault(f, []).append(u)
    for k in buckets:
        buckets[k] = sorted(set(buckets[k]))
    return dict(sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def _ai_select_full_app_folders(
    *,
    project_id: str,
    model: str,
    page_url: str,
    page_text: str,
    buckets: dict[str, list[str]],
) -> dict[str, Any]:
    bucket_summary = [
        {
            "folder": folder,
            "count": len(urls),
            "sample_files": [u.split("/")[-1] for u in urls[:8]],
        }
        for folder, urls in buckets.items()
    ]

    system = (
        "You are selecting the best PDF folder(s) for Texas TDHCA 2026 9% Competitive HTC FULL APPLICATION packets.\n"
        "Use webpage context + discovered bucket names/counts.\n"
        "Return ONLY JSON with this schema:\n"
        "{\n"
        '  "selected_folders": ["folder-name"],\n'
        '  "confidence": 0.0,\n'
        '  "reasoning": "short explanation",\n'
        '  "rejected": [{"folder":"...","why":"..."}]\n'
        "}\n"
        "Rules:\n"
        "- You MUST select at least one folder unless there are truly zero 2026 9% candidate folders.\n"
        "- Do NOT return empty just because labels are indirect.\n"
        "- preapps/pre-application/deficiencies are NOT full applications.\n"
        "- ESA/PCA/Appraisals/Market/SDFR are supporting-doc categories, not the canonical full-application set.\n"
        "- In TDHCA 2026 9% data, the full application packet set is often grouped under a '...challenges' bucket; treat that as strong evidence when present and sized like a complete cycle cohort.\n"
        "- Prefer one primary folder if it clearly represents the complete full-application cohort.\n"
    )

    user = {
        "page_url": page_url,
        "page_excerpt": page_text[:12000],
        "discovered_buckets": bucket_summary,
        "task": "Pick the folder(s) that are the 2026 9% Competitive Housing Tax Credit Full Application set.",
    }

    resp = chat_completions(
        project_id=project_id,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        temperature=0.0,
        timeout_s=120,
    )
    return extract_json_content(resp)


def _extract_all_with_v5_5(
    *,
    project_id: str,
    model: str,
    pdf_dir: Path,
    max_pages: int,
) -> list[ExtractedRow]:
    strat = get_strategy("v5_5_chunked_tiebreaker")
    rows: list[ExtractedRow] = []
    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    for i, p in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {p.name}...", flush=True)
        try:
            result = strat.extract(project_id=project_id, model=model, pdf_path=p, max_pages=max_pages)
            row = result.row
            row.source_pdf_sha256 = sha256_file(p)
            rows.append(row)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.6: AI target-set selection + V5.5 extraction")
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--seed-url", action="append", default=[])
    ap.add_argument("--page-url", default="https://www.tdhca.texas.gov/competitive-9-housing-tax-credits")
    ap.add_argument("--discover-state", default="")
    ap.add_argument("--crawl-max-pages", type=int, default=550)
    ap.add_argument("--parallel-downloads", type=int, default=10)
    ap.add_argument("--max-download-bytes", type=int, default=500_000_000)
    ap.add_argument("--max-urls", type=int, default=0, help="Optional cap for testing")
    ap.add_argument("--resolve-only", action="store_true")
    args = ap.parse_args()

    out_root = Path(args.out_root).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    dl_dir = out_root / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    state_path = Path(args.discover_state).expanduser().resolve() if args.discover_state else (out_root / "discover_state.json")

    # Discover/load PDFs
    if state_path.exists():
        state = _load_json(state_path, {})
        pdf_urls = list(state.get("pdf_urls") or [])
    else:
        seeds = args.seed_url or list(DEFAULT_SEED_URLS)
        disc = discover_pdfs(
            seed_urls=seeds,
            max_pages=int(args.crawl_max_pages),
            state_path=state_path,
            allowed_hosts=None,
            include_pdf_regex="2026",
            exclude_pdf_regex="",
            fetch_timeout_s=15,
            sleep_s=0.1,
        )
        pdf_urls = list(disc.pdf_urls)

    buckets = _bucket_urls(pdf_urls)

    # AI folder selection
    page_text = _fetch_text(args.page_url, timeout_s=30)
    ai_pick = _ai_select_full_app_folders(
        project_id=args.project_id,
        model=args.model,
        page_url=args.page_url,
        page_text=page_text,
        buckets=buckets,
    )

    selected_folders = [str(x) for x in (ai_pick.get("selected_folders") or []) if str(x).strip()]
    selected_urls: list[str] = []
    for f in selected_folders:
        selected_urls.extend(buckets.get(f, []))
    selected_urls = sorted(set(selected_urls))

    if args.max_urls and args.max_urls > 0:
        selected_urls = selected_urls[: int(args.max_urls)]

    decision = {
        "page_url": args.page_url,
        "selected_folders": selected_folders,
        "selected_url_count": len(selected_urls),
        "ai_pick": ai_pick,
        "bucket_counts": {k: len(v) for k, v in buckets.items()},
    }
    (out_root / "v5_6_selection.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    print(json.dumps(decision, indent=2), flush=True)

    if args.resolve_only:
        return 0

    # Download selected set
    dl = download_pdfs(
        pdf_urls=selected_urls,
        out_dir=dl_dir,
        manifest_path=out_root / "download_manifest.json",
        max_new_downloads=len(selected_urls),
        timeout_s=120,
        max_bytes=int(args.max_download_bytes),
        parallel_downloads=int(args.parallel_downloads),
    )
    print(f"downloaded={dl.downloaded} skipped={dl.skipped_existing} failed={dl.failed}", flush=True)

    # Unchanged extraction (V5.5)
    rows = _extract_all_with_v5_5(
        project_id=args.project_id,
        model=args.model,
        pdf_dir=dl_dir,
        max_pages=int(args.max_pages),
    )

    summary = write_outputs(
        out_dir=aggregate_dir,
        rows=rows,
        project_id=args.project_id,
        model=args.model,
        max_pages=int(args.max_pages),
    )

    run_summary = {
        "mode": "v5_6_ai_targeting_plus_v5_5_extraction",
        "selected_folders": selected_folders,
        "selected_url_count": len(selected_urls),
        "downloaded": dl.downloaded,
        "skipped_existing": dl.skipped_existing,
        "failed_downloads": dl.failed,
        "extracted_rows": len(rows),
        "count_needs_review": summary.get("count_needs_review"),
        "outputs": summary.get("outputs"),
    }
    (out_root / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    print(json.dumps(run_summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
