from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pdfplumber


FIELDS = [
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


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _page_text(pdf_path: Path, page_num: int) -> str:
    with pdfplumber.open(str(pdf_path)) as pdf:
        if page_num < 1 or page_num > len(pdf.pages):
            return ""
        try:
            return (pdf.pages[page_num - 1].extract_text() or "").strip()
        except Exception:
            return ""


def audit_record(rec: dict[str, Any]) -> dict[str, Any]:
    pdf_path = Path(rec.get("source_pdf_path") or "")
    out = {"pdf": str(pdf_path), "field_checks": {}, "issues": []}
    if not pdf_path.exists():
        out["issues"].append("missing_pdf")
        return out

    for f in FIELDS:
        obj = rec.get(f) or {}
        val = (obj.get("value") or "").strip()
        pages = obj.get("pages") or []
        quote = (obj.get("quote") or "").strip()

        check = {"has_value": bool(val), "has_pages": bool(pages), "has_quote": bool(quote), "quote_found": None}
        if val:
            if not pages:
                out["issues"].append(f"missing_pages:{f}")
            if not quote:
                out["issues"].append(f"missing_quote:{f}")
            found = False
            qn = _norm(quote)
            for p in pages[:3]:
                try:
                    pn = int(p)
                except Exception:
                    continue
                pt = _norm(_page_text(pdf_path, pn))
                if qn and qn in pt:
                    found = True
                    break
            check["quote_found"] = found
            if quote and pages and not found:
                out["issues"].append(f"quote_not_found:{f}")
        out["field_checks"][f] = check

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, help="Directory containing applications.jsonl")
    ap.add_argument("--out", default="", help="Output path for audit.json (default: <run-dir>/audit.json)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    jsonl = run_dir / "applications.jsonl"
    out_path = Path(args.out).expanduser().resolve() if args.out else (run_dir / "audit.json")

    recs = []
    with jsonl.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            recs.append(json.loads(line))

    audits = [audit_record(r) for r in recs]
    issue_counts = Counter(i for a in audits for i in a.get("issues") or [])
    summary = {
        "run_dir": str(run_dir),
        "count_records": len(audits),
        "issue_counts": dict(issue_counts.most_common()),
        "records": audits,
    }
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("ok wrote:", str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

