from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Bumped when audit payload shape or logging behavior changes materially.
AUDIT_AGENT_VERSION = "lihtc-tx-2026-agent-audit-v1"


@dataclass
class SupabaseConfig:
    url: str
    service_key: str


def load_supabase_config() -> SupabaseConfig | None:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return None
    return SupabaseConfig(url=url, service_key=key)


def _headers(cfg: SupabaseConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "apikey": cfg.service_key,
        "Authorization": f"Bearer {cfg.service_key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:26]}"


def skip_supabase_log_env() -> bool:
    v = (os.environ.get("LIHTC_SKIP_SUPABASE_LOG") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def merge_audit_envelope(
    payload: dict[str, Any],
    *,
    project_id: str,
    pipeline: str,
    run_id: str,
) -> dict[str, Any]:
    """
    Normal JSON shape for audit_log.payload so Supabase consumers can filter
    by project_id (column) and by run_id / pipeline inside the JSON.
    """
    merged: dict[str, Any] = {
        "run_id": run_id,
        "pipeline": pipeline,
        "audit_agent_version": AUDIT_AGENT_VERSION,
        "project_context": {"project_id": project_id},
    }
    merged.update(payload)
    return merged


def log_audit_if_configured(
    *,
    project_id: str,
    actor_id: str,
    event_type: str,
    payload: dict[str, Any],
    pipeline: str,
    no_supabase_log: bool = False,
    run_id: str | None = None,
    require_supabase_log: bool = False,
) -> str | None:
    """
    Writes one audit_log row when SUPABASE_URL + SUPABASE_SERVICE_KEY are set,
    unless disabled via --no-supabase-log or LIHTC_SKIP_SUPABASE_LOG.

    Returns run_id when a row was written, else None.
    """
    if no_supabase_log or skip_supabase_log_env():
        if require_supabase_log:
            raise SystemExit(
                "Supabase logging was required but is disabled (--no-supabase-log or LIHTC_SKIP_SUPABASE_LOG)."
            )
        return None
    cfg = load_supabase_config()
    if not cfg:
        if require_supabase_log:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required (--require-supabase-log).")
        return None
    rid = run_id or new_run_id()
    merged = merge_audit_envelope(payload, project_id=project_id, pipeline=pipeline, run_id=rid)
    insert_audit_log(cfg=cfg, project_id=project_id, actor_id=actor_id, event_type=event_type, payload=merged)
    return rid


def insert_audit_log(*, cfg: SupabaseConfig, project_id: str, actor_id: str, event_type: str, payload: dict[str, Any]) -> None:
    row = {
        "event_type": event_type,
        "actor_id": actor_id,
        "project_id": project_id,
        "payload": {**payload, "ts": datetime.now(timezone.utc).isoformat()},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    req = urllib.request.Request(
        f"{cfg.url}/rest/v1/audit_log",
        data=json.dumps(row).encode("utf-8"),
        headers=_headers(cfg, {"Prefer": "return=minimal"}),
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20).read()


def enqueue_task_packet(
    *,
    cfg: SupabaseConfig,
    project_id: str,
    objective: str,
    task_type: str = "refactor",
    requested_by: str = "lihtc-agent-loop",
    risk_level: str = "low",
    required_capabilities: list[str] | None = None,
) -> str:
    packet_id = f"task-{int(time.time() * 1000)}"
    row: dict[str, Any] = {
        "packet_id": packet_id,
        "project_id": project_id,
        "task_type": task_type,
        "objective": objective,
        "requested_by": requested_by,
        "risk_level": risk_level,
        "project_revision": 1,
        "idempotency_key": packet_id,
        "status": "queued",
    }
    if required_capabilities:
        # Keep in objective text since schema varies across deployments.
        row["objective"] = objective + "\n\n(required_capabilities=" + ",".join(required_capabilities) + ")"

    req = urllib.request.Request(
        f"{cfg.url}/rest/v1/task_packet",
        data=json.dumps(row).encode("utf-8"),
        headers=_headers(cfg, {"Prefer": "return=minimal"}),
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            body = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"task_packet_insert_failed HTTP {e.code}: {body}") from e
        raise
    return packet_id

