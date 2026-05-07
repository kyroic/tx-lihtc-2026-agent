from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


def ensure_openclaw() -> None:
    if shutil.which("openclaw") is None:
        raise RuntimeError("openclaw CLI not found in PATH (required for openclaw_* strategies)")


def _extract_json_from_text(text: str) -> dict[str, Any]:
    """
    Best-effort parse of a JSON object from an OpenClaw text response.
    Strategy: find the last {...} block and json.loads it.
    """
    s = text.rfind("{")
    e = text.rfind("}")
    if s < 0 or e < 0 or e <= s:
        raise RuntimeError("openclaw_output_missing_json")
    blob = text[s : e + 1]
    try:
        return json.loads(blob)
    except Exception as exc:
        raise RuntimeError(f"openclaw_output_invalid_json: {exc}") from exc


def run_openclaw_coaching_append(*, agent: str, message: str, timeout_s: int = 600) -> str:
    """
    Ask OpenClaw for JSON { "coaching_append": "..." } to append to extraction prompts.
    Returns the coaching string, or "" if JSON could not be parsed.
    """
    ensure_openclaw()
    proc = subprocess.run(
        ["openclaw", "agent", "--agent", agent, "--message", message],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"openclaw_coaching_failed rc={proc.returncode}: {out[-1200:]}")
    try:
        d = _extract_json_from_text(out)
        s = d.get("coaching_append")
        if s is None:
            s = d.get("coaching")
        return str(s).strip()[:12000] if s is not None else ""
    except Exception:
        return ""


def run_openclaw_agent(*, agent: str, message: str, timeout_s: int = 900) -> dict[str, Any]:
    ensure_openclaw()
    proc = subprocess.run(
        ["openclaw", "agent", "--agent", agent, "--message", message],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"openclaw_failed rc={proc.returncode}: {out[-800:]}")
    return _extract_json_from_text(out)

