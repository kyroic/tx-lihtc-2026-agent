from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..intake_full_applications import build_manifest_2026_from_crawl, load_manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Intake-first: build/validate the 2026 full-application manifest (no downloads).")
    ap.add_argument("--out-root", required=True, help="Workspace root; writes under <out-root>/intake/")
    ap.add_argument("--manifest-name", default="applications_2026_manifest.json")
    ap.add_argument("--crawl-max-pages", type=int, default=550)
    ap.add_argument("--include-pdf-regex", default="2026")
    ap.add_argument("--exclude-pdf-regex", default="Appraisals")
    ap.add_argument("--verify-head", action="store_true", help="HEAD-check each URL (slower, but drops dead links).")
    ap.add_argument("--print-only", action="store_true", help="Do not crawl; just load + print existing manifest JSON.")
    args = ap.parse_args()

    out_root = Path(args.out_root).expanduser().resolve()
    intake_dir = out_root / "intake"
    manifest_path = intake_dir / str(args.manifest_name).strip()

    if args.print_only:
        m = load_manifest(manifest_path)
        print(json.dumps(m.__dict__, indent=2), flush=True)
        return 0

    m = build_manifest_2026_from_crawl(
        out_path=manifest_path,
        crawl_max_pages=int(args.crawl_max_pages),
        include_pdf_regex=str(args.include_pdf_regex),
        exclude_pdf_regex=str(args.exclude_pdf_regex),
        verify_head=bool(args.verify_head),
    )
    print(json.dumps(m.__dict__, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

