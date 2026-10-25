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
    memory_mode: str = "auto"  # "auto" | "cold" | "warm" (see RunRequest.effective_memory_mode)
    max_retries: int = 2

    # Scout & H-MEM state
    blocked: bool = False
    block_reason: str = ""
    scout_evaluation: Dict[str, Any] = field(default_factory=dict)
    execution_contract: Optional[ExecutionContract] = None
    retrieved_memories: Dict[str, Any] = field(default_factory=dict)
    warm_mode: bool = False
    memory_context_str: str = ""
    # summary SIC code -> {"leaf_code", "record_id", "confidence"} recalled from H-MEM and applied in Pass 1
    learned_sic_rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Planner state
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    # hypothesis_id -> dataset purposes that must return rows before a claim on it may be asserted
    hypothesis_requirements: Dict[str, List[str]] = field(default_factory=dict)
    retrieval_plan: List[Dict[str, Any]] = field(default_factory=list)
    plan_revision_count: int = 0

    # Retriever & Zetaris state
    datasets: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # purpose -> Zetaris result
    evidence_ledger: List[Dict[str, Any]] = field(default_factory=list)
    edgar_sic_joined_rows: List[Dict[str, Any]] = field(default_factory=list)
    sic_hierarchy_rows: List[Dict[str, Any]] = field(default_factory=list)
    billing_rows: List[Dict[str, Any]] = field(default_factory=list)
    billing_segment_summary: List[Dict[str, Any]] = field(default_factory=list)
    support_rows: List[Dict[str, Any]] = field(default_factory=list)
    dq_invalid_rows: List[Dict[str, Any]] = field(default_factory=list)
    sql_history: List[str] = field(default_factory=list)
    retriever_pass: int = 0
    # summary SIC code -> {"leaf_code", "leaf_description", "source": "hmem" | "dq_discovery", ...}
    reconciliation_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Evaluator state
    dq_reconciled: bool = False
    dq_revision_triggered: bool = False
    dq_orphans_dropped: List[Dict[str, Any]] = field(default_factory=list)
    unsupported_claims_blocked: List[str] = field(default_factory=list)

    # Markovian Analyst state
    focus: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    markovian_telemetry: Dict[str, Any] = field(default_factory=dict)
    analyst_draft: Dict[str, Any] = field(default_factory=dict)
    candidate_claims: List[Dict[str, Any]] = field(default_factory=list)
    claim_verdicts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    verified_findings: List[Dict[str, Any]] = field(default_factory=list)
    untested_factors: List[Dict[str, Any]] = field(default_factory=list)

    # Writer & Reviewer state
    writer_draft: Dict[str, Any] = field(default_factory=dict)
    reviewer_issues: List[Dict[str, Any]] = field(default_factory=list)
    reviewer_revision_triggered: bool = False
    review_approved: bool = False
    correlation_vs_causation_caveat: str = ""
    recommended_actions: List[Dict[str, Any]] = field(default_factory=list)
    mined_memory: Dict[str, Any] = field(default_factory=dict)
    sleep_preview: Dict[str, Any] = field(default_factory=dict)
    dreams: List[Dict[str, Any]] = field(default_factory=list)
    conflict_resolution_report: Dict[str, Any] = field(default_factory=dict)

    def dataset_rows(self, purpose: str) -> List[Dict[str, Any]]:
        return list((self.datasets.get(purpose) or {}).get("rows") or [])

    def dataset_has_evidence(self, purpose: str) -> bool:
        res = self.datasets.get(purpose) or {}
        if res.get("status") != "SUCCESS":
            return False
        return bool(res.get("empty_is_evidence")) or int(res.get("row_count") or 0) > 0
