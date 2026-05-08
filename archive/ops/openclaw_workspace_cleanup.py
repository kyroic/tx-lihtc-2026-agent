"""
Disk space management for the shared ``--out-root`` workspace (OpenClaw + harvest tools).

- Deletes **only** PDFs in ``downloads/`` whose sha256 is already in ``extracted_hashes.json``
  (rows remain in ``aggregate/`` and ``all_applications.jsonl``).
- Trims a large console log (e.g. ``progression_agentic_console.log``) to the last N lines.

``run_openclaw_download_hygiene`` runs each orchestrator iteration: (1) PDFs already in
``extracted_hashes``, (2) PDFs classified as **non-keeper** in ``classification_cache.json``,
(3) ``ensure_workspace_room`` for low disk. OpenClaw may set ``cleanup_delete_extracted_pdfs``,
``cleanup_delete_classified_junk``, and ``cleanup_min_free_mb`` in the plan JSON (merged with CLI defaults).
Python performs all deletes; ``progression_agentic`` uses the same hygiene with CLI-only policy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..extract import sha256_file


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_manifest(path: Path) -> dict[str, Any]:
    data = _load_json(path, {})
    return data if isinstance(data, dict) else {}


def disk_free_mb(path: Path) -> float:
    try:
        return round(shutil.disk_usage(path).free / (1024 * 1024), 2)
    except Exception:
        return 0.0


def cleanup_extracted_pdfs(
    *,
    download_dir: Path,
    manifest_path: Path,
    extracted_hashes: set[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    removed: list[dict[str, Any]] = []
    freed = 0
    manifest = _load_manifest(manifest_path)
    for pdf in sorted(download_dir.glob("*.pdf")):
        if not pdf.is_file():
            continue
        try:
            h = sha256_file(pdf)
        except OSError:
            continue
        if h not in extracted_hashes:
            continue
        sz = pdf.stat().st_size
        pstr = str(pdf.resolve())
        if dry_run:
            removed.append({"path": pdf.name, "bytes": sz, "dry_run": True})
            continue
        try:
            pdf.unlink()
        except OSError as e:
            removed.append({"path": pdf.name, "error": str(e)[:200]})
            continue
        freed += sz
        removed.append({"path": pdf.name, "bytes": sz})
        for meta in manifest.values():
            if isinstance(meta, dict) and str(meta.get("path") or "") == pstr:
                meta["status"] = "removed_after_extract_cleanup"
                meta["detail"] = "deleted after extract; aggregate retains rows"
    if not dry_run and freed > 0:
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except OSError:
            pass
    return {"removed": removed, "freed_bytes": freed, "dry_run": dry_run}


def cleanup_classified_non_keepers(
    *,
    download_dir: Path,
    manifest_path: Path,
    class_cache: dict[str, Any],
    class_cache_path: Path,
    extracted_hashes: set[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Remove PDFs that are **already classified** as something other than a keeper
    (``full_application`` + year ``2026`` or ``unknown``). Unclassified PDFs are kept.

    Does not delete keeper PDFs (even if not yet extracted). Extracted SHA dedupe is handled by
    ``cleanup_extracted_pdfs`` first.
    """
    removed: list[dict[str, Any]] = []
    freed = 0
    manifest = _load_manifest(manifest_path)
    keys_drop: list[str] = []

    for pdf in sorted(download_dir.glob("*.pdf")):
        if not pdf.is_file():
            continue
        key = str(pdf.resolve())
        c = class_cache.get(key)
        if not isinstance(c, dict):
            continue
        dt = str(c.get("doc_type") or "")
        yr = str(c.get("year") or "")
        is_keeper = (dt == "full_application") and (yr in ("2026", "unknown"))
        if is_keeper:
            continue
        try:
            h = sha256_file(pdf)
        except OSError:
            continue
        if h in extracted_hashes:
            continue
        sz = pdf.stat().st_size
        pstr = key
        if dry_run:
            removed.append({"path": pdf.name, "bytes": sz, "doc_type": dt, "year": yr, "dry_run": True})
            freed += sz
            continue
        try:
            pdf.unlink()
        except OSError as e:
            removed.append({"path": pdf.name, "error": str(e)[:200]})
            continue
        freed += sz
        keys_drop.append(key)
        removed.append({"path": pdf.name, "bytes": sz, "doc_type": dt, "year": yr})
        for meta in manifest.values():
            if isinstance(meta, dict) and str(meta.get("path") or "") == pstr:
                meta["status"] = "removed_classified_junk"
                meta["detail"] = f"doc_type={dt} year={yr}"
    for kk in keys_drop:
        class_cache.pop(kk, None)
    if not dry_run and (freed > 0 or keys_drop):
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except OSError:
            pass
        try:
            class_cache_path.write_text(json.dumps(class_cache, indent=2), encoding="utf-8")
        except OSError:
            pass
    return {"removed": removed, "freed_bytes": freed, "dropped_cache_keys": len(keys_drop), "dry_run": dry_run}


def trim_console_log(
    log_path: Path,
    *,
    max_mb: float,
    keep_tail_lines: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not log_path.is_file() or max_mb <= 0:
        return {"trimmed": False}
    try:
        sz = log_path.stat().st_size
    except OSError:
        return {"trimmed": False, "reason": "stat_failed"}
    max_bytes = int(max_mb * 1024 * 1024)
    if sz <= max_bytes:
        return {"trimmed": False, "size_bytes": sz}
    if dry_run:
        return {"trimmed": True, "dry_run": True, "size_bytes": sz}
    tail_cap = min(sz, 32 * 1024 * 1024)
    with log_path.open("rb") as f:
        f.seek(max(0, sz - tail_cap))
        chunk = f.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    tail = "\n".join(lines[-keep_tail_lines:]) + ("\n" if lines else "")
    hdr = "[lihtc: log trimmed to last %d lines]\n" % keep_tail_lines
    try:
        log_path.write_text(hdr + tail, encoding="utf-8")
    except OSError as e:
        return {"trimmed": False, "error": str(e)[:200]}
    return {"trimmed": True, "size_bytes_before": sz, "size_bytes_after": log_path.stat().st_size}


def ensure_workspace_room(
    *,
    out_root: Path,
    download_dir: Path,
    manifest_path: Path,
    extracted_hashes_path: Path,
    min_free_mb: float,
    delete_extracted_pdfs: bool,
    trim_log_path: Path | None,
    trim_log_max_mb: float,
    trim_log_tail_lines: int,
    dry_run: bool = False,
    max_rounds: int = 6,
) -> dict[str, Any]:
    before = disk_free_mb(out_root)
    report: dict[str, Any] = {
        "disk_free_mb_before": before,
        "min_free_mb": min_free_mb,
        "rounds": [],
    }
    if before >= min_free_mb:
        report["disk_free_mb_after"] = before
        report["needed_cleanup"] = False
        return report

    raw_h = _load_json(extracted_hashes_path, [])
    extracted_hashes: set[str] = set(raw_h) if isinstance(raw_h, list) else set()

    for _ in range(max(1, int(max_rounds))):
        free = disk_free_mb(out_root)
        if free >= min_free_mb:
            break
        rnd: dict[str, Any] = {"disk_free_mb": free}
        if delete_extracted_pdfs and extracted_hashes:
            rnd["extracted_pdfs"] = cleanup_extracted_pdfs(
                download_dir=download_dir,
                manifest_path=manifest_path,
                extracted_hashes=extracted_hashes,
                dry_run=dry_run,
            )
        if trim_log_path and trim_log_max_mb > 0:
            rnd["console_log"] = trim_console_log(
                trim_log_path,
                max_mb=trim_log_max_mb,
                keep_tail_lines=trim_log_tail_lines,
                dry_run=dry_run,
            )
        report["rounds"].append(rnd)
        freed = int((rnd.get("extracted_pdfs") or {}).get("freed_bytes") or 0)
        trimmed = (rnd.get("console_log") or {}).get("trimmed")
        if freed == 0 and not trimmed:
            break

    report["disk_free_mb_after"] = disk_free_mb(out_root)
    report["needed_cleanup"] = True
    return report


def run_openclaw_download_hygiene(
    *,
    out_root: Path,
    download_dir: Path,
    manifest_path: Path,
    extracted_hashes_path: Path,
    class_cache_path: Path,
    class_cache: dict[str, Any],
    delete_extracted_pdfs: bool,
    delete_classified_junk: bool,
    min_free_mb: float,
    trim_log_path: Path | None,
    trim_log_max_mb: float,
    trim_log_tail_lines: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    One pass before discover/download: remove redundant extracted PDFs, remove classified junk,
    then bring free space up if still below ``min_free_mb``.
    """
    out: dict[str, Any] = {"disk_free_mb_start": disk_free_mb(out_root)}
    raw_h = _load_json(extracted_hashes_path, [])
    extracted_hashes: set[str] = set(raw_h) if isinstance(raw_h, list) else set()

    if delete_extracted_pdfs and extracted_hashes:
        out["extracted_redundant"] = cleanup_extracted_pdfs(
            download_dir=download_dir,
            manifest_path=manifest_path,
            extracted_hashes=extracted_hashes,
            dry_run=dry_run,
        )
        raw_h2 = _load_json(extracted_hashes_path, [])
        extracted_hashes = set(raw_h2) if isinstance(raw_h2, list) else set()

    if delete_classified_junk:
        out["classified_junk"] = cleanup_classified_non_keepers(
            download_dir=download_dir,
            manifest_path=manifest_path,
            class_cache=class_cache,
            class_cache_path=class_cache_path,
            extracted_hashes=extracted_hashes,
            dry_run=dry_run,
        )

    out["disk_pressure"] = ensure_workspace_room(
        out_root=out_root,
        download_dir=download_dir,
        manifest_path=manifest_path,
        extracted_hashes_path=extracted_hashes_path,
        min_free_mb=min_free_mb,
        delete_extracted_pdfs=bool(delete_extracted_pdfs and extracted_hashes),
        trim_log_path=trim_log_path,
        trim_log_max_mb=trim_log_max_mb,
        trim_log_tail_lines=trim_log_tail_lines,
        dry_run=dry_run,
    )
    out["disk_free_mb_end"] = disk_free_mb(out_root)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="TDHCA workspace cleanup (extracted PDFs + log trim).")
    ap.add_argument("--out-root", default=str(Path.home() / "Desktop" / "parcell" / "agent"))
    ap.add_argument("--download-dir", default="")
    ap.add_argument("--min-free-mb", type=float, default=float(os.environ.get("LIHTC_MIN_FREE_MB") or "2048"))
    ap.add_argument("--trim-log", default="progression_agentic_console.log")
    ap.add_argument("--trim-log-max-mb", type=float, default=80.0)
    ap.add_argument("--trim-log-tail-lines", type=int, default=8000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-delete-extracted", action="store_true")
    args = ap.parse_args()

    out_root = Path(args.out_root).expanduser().resolve()
    dl = Path(args.download_dir).expanduser().resolve() if str(args.download_dir).strip() else (out_root / "downloads")
    rep = ensure_workspace_room(
        out_root=out_root,
        download_dir=dl,
        manifest_path=out_root / "download_manifest.json",
        extracted_hashes_path=out_root / "extracted_hashes.json",
        min_free_mb=float(args.min_free_mb),
        delete_extracted_pdfs=not args.no_delete_extracted,
        trim_log_path=(out_root / args.trim_log) if str(args.trim_log).strip() else None,
        trim_log_max_mb=float(args.trim_log_max_mb),
        trim_log_tail_lines=int(args.trim_log_tail_lines),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(rep, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
