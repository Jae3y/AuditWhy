"""Shared state container for the 6-Agent non-linear investigation workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.engines.scout_intent import ExecutionContract


@dataclass
class InvestigationState:
    run_id: str
    request_id: str
    user_query: str
    sample_mode: bool = True
    context: Dict[str, Any] = field(default_factory=dict)

    # Scout & H-MEM state
    blocked: bool = False
    block_reason: str = ""
    scout_evaluation: Dict[str, Any] = field(default_factory=dict)
    execution_contract: Optional[ExecutionContract] = None
    retrieved_memories: Dict[str, Any] = field(default_factory=dict)
    warm_mode: bool = False
    memory_context_str: str = ""

    # Planner state
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    plan_revision_count: int = 0

    # Retriever & Zetaris state
    edgar_sic_joined_rows: List[Dict[str, Any]] = field(default_factory=list)
    sic_hierarchy_rows: List[Dict[str, Any]] = field(default_factory=list)
    billing_rows: List[Dict[str, Any]] = field(default_factory=list)
    billing_segment_summary: List[Dict[str, Any]] = field(default_factory=list)
    support_rows: List[Dict[str, Any]] = field(default_factory=list)
    pricing_rows: List[Dict[str, Any]] = field(default_factory=list)
    dq_invalid_rows: List[Dict[str, Any]] = field(default_factory=list)
    sql_history: List[str] = field(default_factory=list)
    retriever_pass: int = 0

    # Evaluator state
    dq_reconciled: bool = False
    dq_revision_triggered: bool = False
    unsupported_claims_blocked: List[str] = field(default_factory=list)

    # Markovian Analyst state
    markovian_telemetry: Dict[str, Any] = field(default_factory=dict)
    analyst_draft: Dict[str, Any] = field(default_factory=dict)
    verified_findings: List[Dict[str, Any]] = field(default_factory=list)
    untested_factors: List[Dict[str, Any]] = field(default_factory=list)

    # Writer & Reviewer state
    writer_draft: Dict[str, Any] = field(default_factory=dict)
    reviewer_revision_triggered: bool = False
    correlation_vs_causation_caveat: str = ""
    recommended_actions: List[Dict[str, Any]] = field(default_factory=list)
    mined_memory: Dict[str, Any] = field(default_factory=dict)
    sleep_preview: Dict[str, Any] = field(default_factory=dict)
    dreams: List[Dict[str, Any]] = field(default_factory=list)
    conflict_resolution_report: Dict[str, Any] = field(default_factory=dict)
