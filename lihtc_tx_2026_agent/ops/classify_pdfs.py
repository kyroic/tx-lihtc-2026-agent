from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pdfplumber

from ..model_client import chat_completions, extract_json_content


def _first_page_preview(pdf_path: Path) -> str:
    with pdfplumber.open(str(pdf_path)) as pdf:
        if not pdf.pages:
            return ""
        try:
            t = pdf.pages[0].extract_text() or ""
        except Exception:
            t = ""
    return " ".join((t or "").split())[:1500]


def classify_pdf(*, project_id: str, model: str, pdf_path: Path) -> dict[str, Any]:
    preview = _first_page_preview(pdf_path)
    system = (
        "You classify Texas LIHTC 2026-related PDFs.\n"
        "Return ONLY JSON.\n"
        "Output:\n"
        "{\n"
        '  "doc_type": "full_application|pre_application|appraisal|attachment|other",\n'
        '  "year": "2026|other|unknown",\n'
        '  "confidence": 0.0,\n'
        '  "signals": ["..."]\n'
        "}\n"
    )
    user = json.dumps({"filename": pdf_path.name, "first_page_preview": preview}, ensure_ascii=False)
    resp = chat_completions(
        project_id=project_id,
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        timeout_s=120,
    )
    out = extract_json_content(resp)
    out["pdf_path"] = str(pdf_path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-id", default="lihtc-tx-2026")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pdfs = sorted([p for p in pdf_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0])
    rows = [classify_pdf(project_id=args.project_id, model=args.model, pdf_path=p) for p in pdfs]
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("ok wrote:", str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

