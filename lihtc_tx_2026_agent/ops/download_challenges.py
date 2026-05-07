#!/usr/bin/env python3
"""Download the 114 challenge PDFs for V5.5 extraction"""

import json
import hashlib
import os
import urllib.request
import urllib.error
from pathlib import Path

def _safe_name(url: str) -> str:
    base = os.path.basename(urllib.parse.urlparse(url).path) or "file.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    hid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{hid}_{base}"

def download_pdfs(urls: list[str], out_dir: Path, max_parallel: int = 10):
    import concurrent.futures
    import threading
    
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    downloaded = 0
    failed = 0
    
    def download_one(url: str) -> tuple[str, bool, str]:
        nonlocal downloaded, failed
        name = _safe_name(url)
        dest = out_dir / name
        
        if dest.exists() and dest.stat().st_size > 0:
            with lock:
                return url, True, "skipped_existing"
        
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lihtc-tx-2026-agent/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                if not data.startswith(b"%PDF"):
                    with lock:
                        failed += 1
                    return url, False, "not_pdf"
                dest.write_bytes(data)
                with lock:
                    downloaded += 1
                return url, True, f"bytes={len(data)}"
        except Exception as e:
            with lock:
                failed += 1
            return url, False, str(e)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = {ex.submit(download_one, url): url for url in urls}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            url, ok, detail = future.result()
            status = "✅" if ok else "❌"
            print(f"[{i}/{len(urls)}] {status} {url.split('/')[-1]}: {detail}")
    
    print(f"\nDone: {downloaded} downloaded, {failed} failed")

if __name__ == "__main__":
    import urllib.parse
    manifest = json.load(open("out_v5_5_challenges_manifest.json"))
    urls = manifest["application_pdf_urls"]
    print(f"Downloading {len(urls)} challenge PDFs...")
    download_pdfs(urls, Path("downloads_challenges"), max_parallel=15)
