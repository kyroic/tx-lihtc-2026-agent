from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


DEFAULT_SEED_URLS = [
    "https://www.tdhca.texas.gov/competitive-9-housing-tax-credits",
    "https://www.tdhca.texas.gov/apply-funds",
]


PDF_RE = re.compile(r"(?i)\.pdf(?:$|\?)")


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.hrefs.append(v)


def _fetch(url: str, *, timeout_s: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        # best-effort decode
        raw = r.read()
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return raw.decode(errors="ignore")


def _abs_url(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def _same_host(u: str, host: str) -> bool:
    try:
        return urllib.parse.urlparse(u).netloc.lower() == host.lower()
    except Exception:
        return False


def extract_links(html: str, base_url: str) -> list[str]:
    p = _LinkParser()
    p.feed(html)
    out: list[str] = []
    for href in p.hrefs:
        u = _abs_url(base_url, href)
        # strip fragments
        u = u.split("#", 1)[0]
        out.append(u)
    # stable unique
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


@dataclass
class DiscoverResult:
    seed_urls: list[str]
    crawled_pages: int
    pdf_urls: list[str]
    non_pdf_urls: list[str]
    wrote_state_path: str


def discover_pdfs(
    *,
    seed_urls: list[str] | None = None,
    max_pages: int = 50,
    state_path: Path | None = None,
    allowed_hosts: Iterable[str] | None = ("www.tdhca.texas.gov", "tdhca.texas.gov"),
    include_pdf_regex: str = "",
    exclude_pdf_regex: str = "",
    fetch_timeout_s: int = 15,
    sleep_s: float = 0.2,
) -> DiscoverResult:
    seeds = seed_urls or list(DEFAULT_SEED_URLS)
    state_path = state_path or Path("./out/discover_state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)

    allowed_hosts_set = {h.lower() for h in (allowed_hosts or [])}
    inc_re = re.compile(include_pdf_regex, re.I) if include_pdf_regex else None
    exc_re = re.compile(exclude_pdf_regex, re.I) if exclude_pdf_regex else None

    queue: list[str] = list(seeds)
    seen_pages: set[str] = set()
    pdfs: set[str] = set()
    non_pdfs: set[str] = set()

    while queue and len(seen_pages) < max_pages:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        host = urllib.parse.urlparse(url).netloc.lower()
        if allowed_hosts is not None and host and host not in allowed_hosts_set:
            continue

        seen_pages.add(url)
        try:
            html = _fetch(url, timeout_s=int(fetch_timeout_s))
        except Exception:
            continue

        links = extract_links(html, url)
        for u in links:
            if PDF_RE.search(u):
                if inc_re and not inc_re.search(u):
                    continue
                if exc_re and exc_re.search(u):
                    continue
                pdfs.add(u)
            else:
                non_pdfs.add(u)
                if u not in seen_pages and len(seen_pages) + len(queue) < max_pages * 4:
                    queue.append(u)

        time.sleep(sleep_s)

    payload = {
        "seed_urls": seeds,
        "crawled_pages": len(seen_pages),
        "pdf_urls": sorted(pdfs),
        "non_pdf_urls": sorted(non_pdfs),
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return DiscoverResult(
        seed_urls=seeds,
        crawled_pages=len(seen_pages),
        pdf_urls=sorted(pdfs),
        non_pdf_urls=sorted(non_pdfs),
        wrote_state_path=str(state_path),
    )

