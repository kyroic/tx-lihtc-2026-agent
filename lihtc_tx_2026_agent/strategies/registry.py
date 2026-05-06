from __future__ import annotations

from .base import ExtractStrategy
from .llm_single_pass import LlmSinglePassStrategy
from .llm_two_pass import LlmTwoPassStrategy
from .regex_then_llm import RegexThenLlmStrategy
from .focused_pages_llm import FocusedPagesLlmStrategy
from .self_consistency_vote import SelfConsistencyVoteStrategy
from .llm_page_router_then_extract import LlmPageRouterThenExtractStrategy
from .openclaw_single_shot import OpenClawSingleShotStrategy
from .openclaw_two_stage import OpenClawTwoStageStrategy
from .openclaw_checklist import OpenClawChecklistStrategy
from .openclaw_self_consistency import OpenClawSelfConsistencyStrategy
from .v5_5_chunked_tiebreaker import V5_5ChunkedTieBreakerStrategy


def get_strategy(name: str) -> ExtractStrategy:
    n = (name or "").strip().lower()
    if n in ("llm_single_pass", "single", "default", ""):
        return LlmSinglePassStrategy()
    if n in ("llm_two_pass", "two_pass", "2pass"):
        return LlmTwoPassStrategy()
    if n in ("regex_then_llm", "regex_first", "hybrid"):
        return RegexThenLlmStrategy()
    if n in ("focused_pages_llm", "focused_pages", "page_focus"):
        return FocusedPagesLlmStrategy()
    if n in ("self_consistency_vote", "vote", "self_consistency"):
        return SelfConsistencyVoteStrategy()
    if n in ("llm_page_router_then_extract", "page_router", "router_then_extract"):
        return LlmPageRouterThenExtractStrategy()
    if n in ("openclaw_single_shot", "openclaw_single", "oc_single"):
        return OpenClawSingleShotStrategy()
    if n in ("openclaw_two_stage", "oc_two_stage", "oc_twostage"):
        return OpenClawTwoStageStrategy()
    if n in ("openclaw_checklist", "oc_checklist"):
        return OpenClawChecklistStrategy()
    if n in ("openclaw_self_consistency", "oc_self_consistency", "oc_vote"):
        return OpenClawSelfConsistencyStrategy()
    if n in ("v5_5_chunked_tiebreaker", "v5.5", "v5_5", "chunked_tiebreaker", "tiebreaker"):
        return V5_5ChunkedTieBreakerStrategy()
    raise ValueError(f"Unknown strategy: {name!r}")


def list_strategies() -> list[str]:
    return [
        "llm_single_pass",
        "llm_page_router_then_extract",
        "llm_two_pass",
        "regex_then_llm",
        "focused_pages_llm",
        "self_consistency_vote",
        "openclaw_single_shot",
        "openclaw_two_stage",
        "openclaw_checklist",
        "openclaw_self_consistency",
        "v5_5_chunked_tiebreaker",
    ]

