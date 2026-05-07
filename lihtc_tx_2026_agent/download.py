from __future__ import annotations

import hashlib
import json
import os
import time
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Iterable


def _safe_name(url: str) -> str:
    base = os.path.basename(urllib.parse.urlparse(url).path) or "file.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    hid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{hid}_{base}"


def _download_one(
    url: str,
    out_path: Path,
    *,
    timeout_s: int = 60,
    max_bytes: int = 80_000_000,
) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"})
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            try:
                clen = r.headers.get("Content-Length")
                if clen is not None:
                    n = int(str(clen).strip() or "0")
                    if n and n > max_bytes:
                        return False, f"too_large_header:{n}>{max_bytes}"
            except Exception:
                pass
            # Read a small prefix first so we can reject non-PDFs without buffering huge bodies.
            prefix = r.read(2048) or b""
            if not (prefix.startswith(b"%PDF") or b"%PDF" in prefix):
                return False, "not_pdf"
            wrote = 0
            with out_path.open("wb") as f:
                f.write(prefix)
                wrote += len(prefix)
                while True:
                    if wrote >= max_bytes:
                        return False, f"too_large>={max_bytes}"
                    if (time.time() - started) > max(5, int(timeout_s) * 3):
                        # Soft overall deadline: avoid hanging on slow large PDFs.
                        return False, f"timeout_overall>{max(5, int(timeout_s) * 3)}s"
                    chunk = r.read(1024 * 128)
                    if not chunk:
                        break
                    f.write(chunk)
                    wrote += len(chunk)
        return True, f"bytes={wrote}"
    except urllib.error.HTTPError as e:
        return False, f"http_error:{e.code}"
    except urllib.error.URLError as e:
        return False, f"url_error:{e.reason}"
    except Exception as e:
        return False, f"error:{type(e).__name__}:{e}"


@dataclass
class DownloadSummary:
    downloaded: int
    skipped_existing: int
    failed: int
    attempted: int
    out_dir: str
    manifest_path: str


def _load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def download_pdfs(
    *,
    pdf_urls: Iterable[str],
    out_dir: Path,
    manifest_path: Path | None = None,
    limit: int | None = None,
    max_new_downloads: int | None = None,
    timeout_s: int = 30,
    max_bytes: int = 80_000_000,
    parallel_downloads: int = 1,
) -> DownloadSummary:
    """
    Download PDFs into ``out_dir`` and update ``manifest_path`` (merged with prior runs).

    - ``limit``: only consider the first N URLs in the iterable (legacy crawl batches).
    - ``max_new_downloads``: walk URLs (entire list unless ``limit`` is set) until this many
      **new** successful downloads occur; skips existing files without counting toward the cap.
      Use this for multi-run pipelines so later runs advance past already-downloaded URLs.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_path or (out_dir / "download_manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest(manifest_path)
    downloaded = skipped = failed = attempted = 0
    urls_list = list(pdf_urls)
    lock = threading.Lock()

    def process_url(url: str) -> tuple[str, str]:
        nonlocal downloaded, skipped, failed, attempted
        with lock:
            attempted += 1
        name = _safe_name(url)
        dest = out_dir / name
        if dest.exists() and dest.stat().st_size > 0:
            with lock:
                skipped += 1
                manifest[url] = {"status": "skipped_existing", "path": str(dest)}
            return url, "skipped_existing"
        ok, detail = _download_one(url, dest, timeout_s=int(timeout_s), max_bytes=int(max_bytes))
        if ok:
            with lock:
                downloaded += 1
                manifest[url] = {"status": "downloaded", "path": str(dest), "detail": detail}
            return url, "downloaded"
        else:
            with lock:
                failed += 1
            try:
                if dest.exists():
                    dest.unlink()
            except Exception:
                pass
            with lock:
                manifest[url] = {"status": "failed", "detail": detail}
            return url, "failed"

    def iter_urls() -> list[str]:
        if max_new_downloads is not None:
            return urls_list
        if limit is not None:
            return urls_list[: int(limit)]
        return urls_list

    if max_new_downloads is not None:
        cap = max(0, int(max_new_downloads))
        # Parallel: rolling window, but keep a HARD success cap.
        # We bound concurrency by the remaining downloads so we never overshoot.
        n = max(1, int(parallel_downloads))
        n = min(32, n, max(1, cap) if cap else 1)
        if cap == 0:
            pass
        elif n == 1:
            for url in urls_list:
                if downloaded >= cap:
                    break
                process_url(url)
        else:
            it = iter(urls_list)
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
                inflight: set[concurrent.futures.Future] = set()
                # prime the pump
                while len(inflight) < n:
                    try:
                        u = next(it)
                    except StopIteration:
                        break
                    inflight.add(ex.submit(process_url, u))
                while inflight:
                    done, inflight = concurrent.futures.wait(inflight, return_when=concurrent.futures.FIRST_COMPLETED)
                    # If cap reached, cancel anything not yet started.
                    if downloaded >= cap:
                        for f in inflight:
                            f.cancel()
                        break
                    # Submit at most one new URL per completion, and never submit
                    # more than the remaining needed successes.
                    for _ in done:
                        if downloaded >= cap:
                            break
                        try:
                            u = next(it)
                        except StopIteration:
                            break
                        inflight.add(ex.submit(process_url, u))
    else:
        n = max(1, int(parallel_downloads))
        n = min(32, n)
        urls = iter_urls()
        if n == 1:
            for url in urls:
                process_url(url)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(process_url, urls))

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return DownloadSummary(
        downloaded=downloaded,
        skipped_existing=skipped,
        failed=failed,
        attempted=attempted,
        out_dir=str(out_dir),
        manifest_path=str(manifest_path),
    )

