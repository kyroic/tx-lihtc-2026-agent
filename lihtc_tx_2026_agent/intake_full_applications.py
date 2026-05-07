from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .discover import DEFAULT_SEED_URLS, discover_pdfs


_TX_2026_NUM_RE = re.compile(r"/(?P<num>26\\d{3})\\.pdf(?:$|\\?)", re.I)


@dataclass(frozen=True)
class IntakeManifest:
    source_pages: list[str]
    pdf_roots: list[str]
    application_pdf_urls: list[str]
    ordering_hint: str
    completeness_check: str
    notes: str

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "IntakeManifest":
        return IntakeManifest(
            source_pages=[str(x) for x in (d.get("source_pages") or []) if str(x).strip()],
            pdf_roots=[str(x) for x in (d.get("pdf_roots") or []) if str(x).strip()],
            application_pdf_urls=[str(x) for x in (d.get("application_pdf_urls") or []) if str(x).strip()],
            ordering_hint=str(d.get("ordering_hint") or "").strip(),
            completeness_check=str(d.get("completeness_check") or "").strip(),
            notes=str(d.get("notes") or "").strip(),
        )


def _sort_key(u: str) -> tuple[int, str]:
    m = _TX_2026_NUM_RE.search(u)
    if not m:
        return (10**9, u)
    try:
        return (int(m.group("num")), u)
    except Exception:
        return (10**9, u)


def _head_ok(url: str, *, timeout_s: int = 10) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            code = getattr(r, "status", 200)
        return int(code) < 400
    except Exception:
        return False


def build_manifest_2026_from_crawl(
    *,
    out_path: Path,
    seed_urls: list[str] | None = None,
    crawl_max_pages: int = 550,
    include_pdf_regex: str = "2026",
    exclude_pdf_regex: str = "Appraisals",
    verify_head: bool = False,
) -> IntakeManifest:
    """
    Intake step: build a stable list of candidate 2026 application PDFs.

    This does NOT download PDFs. It crawls seed pages to collect candidate PDF URLs
    and persists them as a manifest for later download/classify/extract.
    """
    seeds = seed_urls or list(DEFAULT_SEED_URLS)
    t0 = time.time()
    disc = discover_pdfs(
        seed_urls=seeds,
        max_pages=int(crawl_max_pages),
        state_path=out_path.parent / "intake_discover_state.json",
        allowed_hosts=None,
        include_pdf_regex=include_pdf_regex,
        exclude_pdf_regex=exclude_pdf_regex,
        fetch_timeout_s=15,
    )
    urls = sorted(set(disc.pdf_urls), key=_sort_key)
    if verify_head:
        urls = [u for u in urls if _head_ok(u)]

    # Best-effort derive roots.
    roots: list[str] = []
    for u in urls:
        if "/multifamily/docs/imaged/" in u:
            base = u.rsplit("/", 1)[0] + "/"
            if base not in roots:
                roots.append(base)
        if len(roots) >= 12:
            break

    manifest = IntakeManifest(
        source_pages=list(seeds),
        pdf_roots=roots,
        application_pdf_urls=urls,
        ordering_hint="sorted by 5-digit application number ascending when URL matches /26xxx.pdf",
        completeness_check=f"crawled_pages={disc.crawled_pages}; pdf_urls={len(urls)}; verify_head={bool(verify_head)}",
        notes=f"Built from crawl include={include_pdf_regex!r} exclude={exclude_pdf_regex!r} in {round(time.time()-t0,2)}s.",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")
    return manifest


def load_manifest(path: Path) -> IntakeManifest:
    return IntakeManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

