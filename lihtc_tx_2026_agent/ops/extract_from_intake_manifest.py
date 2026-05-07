from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ..download import download_pdfs
from ..extract import sha256_file, write_outputs
from ..intake_full_applications import load_manifest
from ..strategies.registry import get_strategy
from .classify_pdfs import classify_pdf


def main() -> int:
    ap = argparse.ArgumentParser(description="Intake-first extraction: download+classify+extract from the intake manifest.")
    ap.add_argument("--out-root", required=True, help="Workspace root (expects <out-root>/intake/applications_2026_manifest.json by default).")
    ap.add_argument(
        "--manifest",
        default="",
        help="Optional manifest path override (default: <out-root>/intake/applications_2026_manifest.json).",
    )
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--strategy", default="llm_page_router_then_extract")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--download-timeout-s", type=int, default=120)
    ap.add_argument("--download-max-bytes", type=int, default=80_000_000)
    ap.add_argument("--parallel-downloads", type=int, default=20, help="Default 20.")
    ap.add_argument("--batch-downloads", type=int, default=20, help="Default 20.")
    ap.add_argument("--max-minutes", type=float, default=60.0, help="Default 60 minutes safety stop.")
    ap.add_argument("--keep-only-extracted-pdfs", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    out_root = Path(args.out_root).expanduser().resolve()
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if str(args.manifest).strip()
        else (out_root / "intake" / "applications_2026_manifest.json")
    )
    out_root.mkdir(parents=True, exist_ok=True)
    dl_dir = out_root / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    aggregate_dir = out_root / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    class_cache_path = out_root / "classification_cache.json"

    m = load_manifest(manifest_path)
    urls = list(m.application_pdf_urls)

    strat = get_strategy(args.strategy)
    class_cache: dict[str, Any] = {}
    if class_cache_path.exists():
        try:
            class_cache = json.loads(class_cache_path.read_text(encoding="utf-8"))
        except Exception:
            class_cache = {}

    extracted_rows = []
    downloaded_ok_total = 0
    downloaded_attempted_total = 0
    failed_total = 0

    cursor = 0
    t0 = time.time()
    while cursor < len(urls):
        if float(args.max_minutes) > 0 and (time.time() - t0) >= float(args.max_minutes) * 60.0:
            break
        cap = max(1, int(args.batch_downloads))
        batch_urls = urls[cursor:]

        dl = download_pdfs(
            pdf_urls=batch_urls,
            out_dir=dl_dir,
            manifest_path=out_root / "download_manifest.json",
            max_new_downloads=cap,
            timeout_s=int(args.download_timeout_s),
            max_bytes=int(args.download_max_bytes),
            parallel_downloads=int(args.parallel_downloads),
        )
        downloaded_ok_total += int(dl.downloaded)
        downloaded_attempted_total += int(dl.attempted)
        failed_total += int(dl.failed)

        pdfs = sorted([p for p in dl_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
        keepers: list[Path] = []
        for p in pdfs:
            key = str(p.resolve())
            c = class_cache.get(key)
            if c is None:
                c = classify_pdf(project_id=args.project_id, model=args.model, pdf_path=p)
                class_cache[key] = c
            if (c.get("doc_type") == "full_application") and (c.get("year") in ("2026", "unknown")):
                keepers.append(p)

        class_cache_path.write_text(json.dumps(class_cache, indent=2), encoding="utf-8")

        # Extract (sequential here; parallelism is already handled by orchestrator path. Keep it stable.)
        for p in keepers:
            row = strat.extract(project_id=args.project_id, model=args.model, pdf_path=p, max_pages=int(args.max_pages)).row
            row.source_pdf_sha256 = sha256_file(p)
            extracted_rows.append(row)

        if args.keep_only_extracted_pdfs:
            extracted_shas = {r.source_pdf_sha256 for r in extracted_rows if (r.source_pdf_sha256 or "").strip()}
            for p in list(dl_dir.glob("*.pdf")):
                if not p.is_file() or p.stat().st_size <= 0:
                    continue
                try:
                    h = sha256_file(p)
                except Exception:
                    continue
                if h not in extracted_shas:
                    try:
                        p.unlink()
                    except Exception:
                        pass

        # Advance cursor by number of URLs we attempted to touch (best effort)
        cursor = min(len(urls), cursor + max(1, int(dl.attempted)))

        # Write aggregate after each batch so you can open it while it runs.
        write_outputs(out_dir=aggregate_dir, rows=extracted_rows, project_id=args.project_id, model=args.model, max_pages=int(args.max_pages))

    summary = {
        "manifest": str(manifest_path),
        "out_root": str(out_root),
        "total_manifest_urls": len(urls),
        "downloaded_ok": downloaded_ok_total,
        "download_attempted": downloaded_attempted_total,
        "failed_downloads": failed_total,
        "extracted_rows": len(extracted_rows),
        "aggregate_xlsx": str(aggregate_dir / "applications.xlsx"),
        "elapsed_s": round(time.time() - t0, 2),
        "cursor_end": cursor,
    }
    (out_root / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

