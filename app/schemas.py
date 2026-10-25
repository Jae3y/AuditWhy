"""Pydantic schemas for AuditWhy API contracts, agent state, and JSONL telemetry."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from app.config import safe_identifier, settings

MemoryMode = Literal["auto", "cold", "warm"]


TraceStatus = Literal[
    "success",
    "needs_revision",
    "retrying",
    "partial_success",
    "fallback",
    "blocked",
]

SponsorTech = Literal[
    "zetaris",
    "meterless_hmem",
    "meterless_markovian",
    "meterless_world_model",
    "meterless_scout",
    "nvidia_nemotron",
]


class JudgeInputPayload(BaseModel):
    query: str = Field(default="")
    follow_up_conflict_test: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = None


class JudgeOptionsPayload(BaseModel):
    sample_mode: Optional[bool] = None
    warm_memory_seed: Optional[bool] = None
    max_retries: int = 2


class RunRequest(BaseModel):
    """Unified request schema supporting both Judge payload and Vibe Coding payload."""

    # Standard Judge payload fields
    run_id: Optional[str] = None
    input: Optional[JudgeInputPayload | Dict[str, Any] | str] = None
    options: Optional[JudgeOptionsPayload | Dict[str, Any]] = None

    # Vibe Coding payload fields
    request_id: Optional[str] = None
    user_query: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

    # Direct convenience fields
    query: Optional[str] = None
    sample_mode: Optional[bool] = None

    def effective_run_id(self) -> str:
        raw = self.run_id or self.request_id
        return safe_identifier(raw) if raw else f"run-{uuid.uuid4().hex[:10]}"

    def effective_memory_mode(self) -> MemoryMode:
        """Map `warm_memory_seed` onto an H-MEM policy.

        true  -> "warm": seed the reconciliation rule if absent, then apply recalled rules.
        false -> "cold": recall for telemetry only; force the full discovery path (demo baseline).
        unset -> "auto": apply any recalled high-trust rule, which is how compounding happens organically.
        """
        flag = self.effective_context().get("warm_memory_seed")
        if flag is True:
            return "warm"
        if flag is False:
            return "cold"
        return "auto"

    def effective_max_retries(self) -> int:
        try:
            return max(0, min(5, int(self.effective_context().get("max_retries", 2))))
        except (TypeError, ValueError):
            return 2

    def effective_query(self) -> str:
        if self.user_query and self.user_query.strip():
            return self.user_query.strip()
        if self.query and self.query.strip():
            return self.query.strip()
        if isinstance(self.input, JudgeInputPayload):
            return self.input.query.strip()
        if isinstance(self.input, dict):
            q = self.input.get("query") or self.input.get("user_query") or ""
            return str(q).strip()
        if isinstance(self.input, str):
            return self.input.strip()
        return ""

    def effective_sample_mode(self) -> bool:
        if self.sample_mode is not None:
            return bool(self.sample_mode)
        if isinstance(self.options, JudgeOptionsPayload) and self.options.sample_mode is not None:
            return bool(self.options.sample_mode)
        if isinstance(self.options, dict) and "sample_mode" in self.options:
            return bool(self.options["sample_mode"])
        if isinstance(self.context, dict) and "sample_mode" in self.context:
            return bool(self.context["sample_mode"])
        return settings.sample_mode

    def effective_context(self) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        if isinstance(self.context, dict):
            ctx.update(self.context)
        if isinstance(self.options, JudgeOptionsPayload):
            ctx.update(self.options.model_dump(exclude_none=True))
        elif isinstance(self.options, dict):
            ctx.update(self.options)
        if isinstance(self.input, JudgeInputPayload) and self.input.follow_up_conflict_test:
            ctx["follow_up_conflict_test"] = self.input.follow_up_conflict_test
        elif isinstance(self.input, dict) and "follow_up_conflict_test" in self.input:
            ctx["follow_up_conflict_test"] = self.input["follow_up_conflict_test"]
        return ctx


class TraceEvent(BaseModel):
    """Mandatory JSONL trace event schema."""

    timestamp: str
    run_id: str
    trace_id: str
    agent_name: str
    action: str
    input_summary: str
    output_summary: str
    target_agent: str
    confidence: float = Field(ge=0.0, le=1.0)
    retry_count: int = Field(ge=0)
    status: TraceStatus
    sponsor_tech: SponsorTech


class HypothesisItem(BaseModel):
    hypothesis_id: str
    title: str
    description: str
    status: Literal[
        "pending",
        "verified",
        "reconciled_after_dq_fix",
        "inconclusive",
        "blocked_untested",
        "refuted",
    ]
    confidence: float
    evidence_tables: List[str]
    sql_queries: List[str]
    finding_summary: str


class RunResponse(BaseModel):
    """Comprehensive structured output returned by POST /run and CLI mode."""

    run_id: str
    request_id: str
    status: Literal["success", "partial_success", "blocked", "error"]
    primary_track: str = "Track 02 — The Agent That Can Explain Why"
    secondary_tracks: List[str] = Field(
        default_factory=lambda: [
            "Track 01 — Solving Fragmented Intelligence",
            "Track 03 — Reasoning Architecture",
        ]
    )
    query: str
    sample_mode: bool
    executive_summary: str
    why_explanation: Dict[str, Any]
    taxonomic_anomaly_resolution: Dict[str, Any]
    hypotheses: List[HypothesisItem]
    verified_findings: List[Dict[str, Any]]
    multivariate_correlations: Dict[str, Any] = Field(default_factory=dict)
    enterprise_360_matrix: List[Dict[str, Any]] = Field(default_factory=list)
    untested_factors: List[Dict[str, Any]]
    correlation_vs_causation_caveat: str
    recommended_actions: List[Dict[str, Any]]
    evidence_ledger: List[Dict[str, Any]] = Field(default_factory=list)
    zetaris_lineage: Dict[str, Any]
    meterless_engines: Dict[str, Any]
    provider_routing: Dict[str, Any]
    trace_summary: Dict[str, Any]
    trace_events: List[TraceEvent]
    confidence: float
    duration_ms: float

    # Hackathon "Technical Execution Requirements" /run contract (additive; mirrors the fields above)
    output: Dict[str, Any] = Field(default_factory=dict)  # {summary, recommendations, artifacts}
    agents: List[Dict[str, str]] = Field(default_factory=list)  # [{name, role}]
    trace_id: str = ""
    log_file: str = ""
    execution_time_seconds: float = 0.0
    error: Optional[Dict[str, Any]] = None  # {type, message, recoverable} when status == "error"

    # Vibe Coding Starter Guide contract (additive)
    agents_used: List[str] = Field(default_factory=list)
    result: Dict[str, Any] = Field(default_factory=dict)  # {summary, findings, confidence}
    collaboration: Dict[str, Any] = Field(default_factory=dict)
    log_path: str = ""
