from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pdfplumber

from ..extract import ExtractedRow, coaching_append_from_env, field_from_obj, norm_ws
from ..model_client import chat_completions, extract_json_content
from .base import StrategyResult


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


def _read_pages(pdf_path: Path, max_pages: int) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages[:max_pages], start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            text = (text or "").strip()
            pages.append({"page": idx, "text": text[:4000]})
    return pages


def _page_summaries(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in pages:
        t = norm_ws(str(p.get("text") or ""))
        out.append({"page": int(p.get("page") or 0), "preview": t[:600]})
    return out


def _route_pages(*, project_id: str, model: str, pdf_name: str, page_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    system = (
        "You are a document router for a Texas LIHTC application PDF.\n"
        "Given page previews, choose which pages likely contain each field.\n"
        "Return ONLY JSON.\n"
        "Output format:\n"
        "{\n"
        '  "field_pages": { "<field>": [<page_numbers>] },\n'
        '  "notes": "short"\n'
        "}\n"
        "Rules:\n"
        "- Only include page numbers that exist.\n"
        "- Prefer <= 6 pages per field.\n"
    ) + coaching_append_from_env()
    user = json.dumps(
        {
            "pdf_filename": pdf_name,
            "fields": FIELDS,
            "page_summaries": page_summaries,
        },
        ensure_ascii=False,
    )
    resp = chat_completions(
        project_id=project_id,
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        timeout_s=180,
    )
    return extract_json_content(resp)


def _extract_with_hints(
    *,
    project_id: str,
    model: str,
    pdf_path: Path,
    pages: list[dict[str, Any]],
    field_pages: dict[str, list[int]],
) -> dict[str, Any]:
    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON (no markdown).\n"
        "Rules:\n"
        "- Never invent values. If not present, return value=\"\" and confidence=0.\n"
        "- Every non-empty value MUST include pages[] and a short quote copied from the PDF text.\n"
        "- Use field_pages hints to focus search.\n"
    ) + coaching_append_from_env()
    schema = {k: {"value": "", "confidence": 0.0, "pages": [], "quote": ""} for k in FIELDS}
    user = json.dumps(
        {
            "pdf_filename": pdf_path.name,
            "field_pages": field_pages,
            "pages": pages,
            "output_schema_example": schema,
        },
        ensure_ascii=False,
    )
    resp = chat_completions(
        project_id=project_id,
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        timeout_s=240,
    )
    return extract_json_content(resp)


def _quote_exists_on_pages(quote: str, pages: list[int], all_pages: list[dict[str, Any]]) -> bool:
    q = norm_ws(quote).lower()
    if not q:
        return False
    page_map = {int(p.get("page") or 0): norm_ws(str(p.get("text") or "")).lower() for p in all_pages}
    for pn in pages[:3]:
        if q and q in (page_map.get(int(pn), "")):
            return True
    return False


class LlmPageRouterThenExtractStrategy:
    """
    AI-driven first stage:
    - Router selects pages per field from page previews
    - Extractor uses full page text but focuses via field_pages hints
    - Auto-retry any field where quote doesn't appear on cited pages
    """

    name = "llm_page_router_then_extract"

    def extract(self, *, project_id: str, model: str, pdf_path: Path, max_pages: int) -> StrategyResult:
        t0 = time.time()
        pages = _read_pages(pdf_path, max_pages=max_pages)
        summaries = _page_summaries(pages)

        route = _route_pages(project_id=project_id, model=model, pdf_name=pdf_path.name, page_summaries=summaries)
        field_pages = (route.get("field_pages") or {}) if isinstance(route, dict) else {}

        out = _extract_with_hints(project_id=project_id, model=model, pdf_path=pdf_path, pages=pages, field_pages=field_pages)

        # Auto-retry fields with evidence mismatch by asking for a new quote from the cited page text.
        # (Keep it bounded: at most 3 fields.)
        retry_fields = []
        for k in FIELDS:
            obj = out.get(k) or {}
            val = str(obj.get("value") or "").strip()
            q = str(obj.get("quote") or "").strip()
            pns = obj.get("pages") or []
            if val and q and pns and not _quote_exists_on_pages(q, pns, pages):
                retry_fields.append(k)
        retry_fields = retry_fields[:3]

        if retry_fields:
            sys2 = (
                "You are fixing evidence quotes for extracted fields.\n"
                "Return ONLY JSON mapping each field to {pages, quote}.\n"
                "Do not change the field value.\n"
                "The quote MUST be a verbatim substring of the provided page text.\n"
            ) + coaching_append_from_env()
            # Provide the cited page texts for those fields.
            ctx = {}
            page_map = {int(p.get('page') or 0): p.get('text') or "" for p in pages}
            for f in retry_fields:
                obj = out.get(f) or {}
                ctx[f] = {
                    "value": obj.get("value"),
                    "pages": obj.get("pages") or [],
                    "page_texts": {str(pn): page_map.get(int(pn), "")[:2000] for pn in (obj.get("pages") or [])[:3]},
                }
            user2 = json.dumps({"fix_fields": retry_fields, "context": ctx}, ensure_ascii=False)
            resp2 = chat_completions(
                project_id=project_id,
                model=model,
                messages=[{"role": "system", "content": sys2}, {"role": "user", "content": user2}],
                temperature=0.0,
                timeout_s=180,
            )
            fix = extract_json_content(resp2)
            if isinstance(fix, dict):
                for f in retry_fields:
                    if f in fix and isinstance(fix[f], dict):
                        out.setdefault(f, {})
                        out[f]["quote"] = fix[f].get("quote") or out[f].get("quote")
                        out[f]["pages"] = fix[f].get("pages") or out[f].get("pages")

        row = ExtractedRow(source_pdf_path=str(pdf_path), source_pdf_sha256="")
        for k in FIELDS:
            setattr(row, k, field_from_obj(out.get(k)))
        # keep review flagging to downstream audit/eval
        row.needs_review = True
        return StrategyResult(
            row=row,
            wall_time_s=round(time.time() - t0, 3),
            meta={"router_notes": route.get("notes") if isinstance(route, dict) else "", "retried_fields": retry_fields},
        )

