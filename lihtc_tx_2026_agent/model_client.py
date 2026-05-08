from __future__ import annotations

import json
import os
import random
import time
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _dotenv_get(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def openai_base_url() -> str:
    """
    Returns an OpenAI-compatible base URL from OPENAI_BASE_URL env var only.
    Does NOT fall back to dotenv — use env vars explicitly for the base URL.
    """
    base = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    return base.rstrip("/")

def openai_api_key() -> str:
    # Standalone mode uses OPENAI_API_KEY directly (normal OpenAI API).
    k = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if k:
        return k
    # Optional convenience: read from ~/.openclaw/.env if present.
    k = _dotenv_get(Path.home() / ".openclaw" / ".env", "OPENAI_API_KEY")
    return (k or "").strip()


def supabase_service_key() -> str:
    """
    Optional: when using a Supabase Edge Function as an OpenAI-compatible gateway,
    requests may require Supabase auth headers (apikey + bearer).
    """
    k = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if k:
        return k
    k = _dotenv_get(Path.home() / ".openclaw" / ".env", "SUPABASE_SERVICE_KEY")
    return (k or "").strip()

def resolve_base_url() -> str:
    """
    Resolve base URL for OpenAI-compatible calls.

    Priority:
    - OPENAI_BASE_URL (any OpenAI-compatible gateway)
    - ~/.openclaw/.env OPENAI_BASE_URL (optional convenience)
    - default OpenAI API base if OPENAI_API_KEY is set
    """
    base = openai_base_url()
    if base:
        return base
    if openai_api_key():
        return "https://api.openai.com"
    return ""

def _chat_completions_max_retries() -> int:
    raw = (os.environ.get("LIHTC_CHAT_COMPLETION_RETRIES") or "5").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 5
    return max(1, min(12, n))


def chat_completions(
    *,
    project_id: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    timeout_s: int = 180,
) -> dict[str, Any]:
    base = resolve_base_url()
    if not base:
        raise RuntimeError("No model endpoint configured. Set OPENAI_BASE_URL (proxy/sidecar) or OPENAI_API_KEY (direct OpenAI).")

    body = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}

    # Standalone direct OpenAI requires Authorization. Proxies may ignore it.
    api_key = openai_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        # If we're pointing at a Supabase Edge Function gateway, prefer the service key.
        if "supabase.co/functions" in base:
            sb = supabase_service_key()
            if sb:
                headers["apikey"] = sb
                headers["Authorization"] = f"Bearer {sb}"

    # Optional metadata headers; some gateways use these for routing/logging.
    if project_id:
        headers["x-project-id"] = project_id
        headers["x-agent-id"] = "lihtc-tx-2026-agent"

    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    max_tries = _chat_completions_max_retries()
    last_exc: BaseException | None = None
    for attempt in range(max_tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (408, 425, 429, 500, 502, 503, 504) and attempt < max_tries - 1:
                delay = min(90.0, (2**attempt) + random.random())
                time.sleep(delay)
                continue
            raise
        except urllib.error.URLError as e:
            last_exc = e
            if attempt < max_tries - 1:
                delay = min(90.0, (2**attempt) + random.random())
                time.sleep(delay)
                continue
            raise
        except (TimeoutError, socket.timeout) as e:
            last_exc = e
            if attempt < max_tries - 1:
                delay = min(90.0, (2**attempt) + random.random())
                time.sleep(delay)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def extract_json_content(resp: dict[str, Any]) -> dict[str, Any]:
    try:
        content = resp["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        raise RuntimeError(f"model_returned_invalid_json: {e}") from e

