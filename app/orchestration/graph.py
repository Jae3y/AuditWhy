"""State-driven 6-Agent non-linear orchestration graph for AuditWhy."""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from app.adapters.zetaris import ZetarisClient
from app.agents.analyst import MarkovianAnalystAgent
from app.agents.evaluator import EvidenceEvaluatorAgent
from app.agents.planner import PlannerAgent
from app.agents.retriever import DataRetrieverAgent
from app.agents.reviewer import FinalReviewerAgent
from app.agents.scout_guard import ScoutGuardAgent
from app.agents.writer import ExplanationWriterAgent
from app.analytics import fmt_usd, mean
from app.config import redact_sensitive_text
from app.engines.hmem import GLOBAL_HMEM, HMemEngine, load_hmem_state
from app.engines.markovian import MarkovianEngine
from app.engines.scout_intent import ScoutIntentEngine
from app.engines.world_model import WorldModelEngine
from app.llm import LLMRouter
from app.orchestration.state import InvestigationState
from app.schemas import HypothesisItem, RunRequest, RunResponse
from app.tracing import TraceLogger


class AuditWhyOrchestrator:
    """Executes the full 6-Agent non-linear investigation loop with Zetaris, Meterless Engines, and Nemotron."""

    def __init__(
        self,
        *,
        hmem_engine: Optional[HMemEngine] = None,
        sample_mode: Optional[bool] = None,
        sample_dir: Optional[Path] = None,
        persist_memory: bool = True,
    ) -> None:
        self.hmem = hmem_engine or GLOBAL_HMEM
        if hmem_engine is None:
            load_hmem_state(self.hmem)
        self.sample_mode = sample_mode
        self.sample_dir = sample_dir
        self.persist_memory = persist_memory

    def run(self, request: RunRequest) -> RunResponse:
        start_time = time.perf_counter()
        run_id = request.effective_run_id()
        eff_sample_mode = self.sample_mode if self.sample_mode is not None else request.effective_sample_mode()
        trace_logger = TraceLogger(run_id=run_id)
        llm_router = LLMRouter(sample_mode=eff_sample_mode)
        state = InvestigationState(
            run_id=run_id,
            request_id=request.request_id or run_id,
            user_query=request.effective_query(),
            sample_mode=eff_sample_mode,
            context=request.effective_context(),
            memory_mode=request.effective_memory_mode(),
            max_retries=request.effective_max_retries(),
        )
        try:
            return self._execute(state, llm_router, trace_logger, start_time)
        except Exception as exc:  # noqa: BLE001 - any failure becomes a structured, traced error response
            trace_logger.log(
                agent_name="Orchestrator",
                action="recover_from_unhandled_error",
                input_summary=f"Unhandled {type(exc).__name__} during run",
                output_summary=redact_sensitive_text(f"{exc} | {traceback.format_exc(limit=3)}")[:600],
                target_agent="None",
                confidence=0.0,
                retry_count=0,
                status="fallback",
                sponsor_tech="meterless_markovian",
            )
            return self._build_error_response(state, llm_router, trace_logger, exc, self._elapsed(start_time))

    @staticmethod
    def _elapsed(start: float) -> float:
        return round((time.perf_counter() - start) * 1000.0, 2)

    def _execute(self, state: InvestigationState, llm_router: LLMRouter, trace_logger: TraceLogger, start_time: float) -> RunResponse:
        scout_engine = ScoutIntentEngine()
        world_model = WorldModelEngine()
        zetaris_client = ZetarisClient(
            sample_mode=state.sample_mode,
            scout_engine=scout_engine,
            trace_logger=trace_logger,
            sample_dir=self.sample_dir,
        )

        scout_agent = ScoutGuardAgent(scout_engine, self.hmem, trace_logger)
        planner_agent = PlannerAgent(world_model, llm_router, trace_logger)
        retriever_agent = DataRetrieverAgent(zetaris_client, world_model, trace_logger)
        evaluator_agent = EvidenceEvaluatorAgent(zetaris_client, llm_router, trace_logger)
        analyst_agent = MarkovianAnalystAgent(MarkovianEngine(), world_model, llm_router, trace_logger)
        writer_agent = ExplanationWriterAgent(llm_router, trace_logger)
        reviewer_agent = FinalReviewerAgent(self.hmem, trace_logger, persist=self.persist_memory)

        # 1. Scout Guard: 5-layer screen, H-MEM recall, signed ExecutionContract
        state = scout_agent.execute(state)
        if state.blocked:
            return self._build_blocked_response(state, llm_router, trace_logger, self._elapsed(start_time))

        # 2-3. Plan, then Pass-1 retrieval of every planned dataset
        state = planner_agent.execute(state)
        state = retriever_agent.execute_pass_1(state)

        # 4. Critique & Revision Loop 1: the evaluator's own DQ check decides whether the plan must change
        for _ in range(state.max_retries + 1):
            state = evaluator_agent.evaluate_data_quality(state)
            if state.dq_reconciled or state.plan_revision_count >= state.max_retries:
                break
            state = planner_agent.execute(state, revision_reason=state.analyst_draft.get("dq_revision_reason", "fk_sic_check orphan"))
            state = retriever_agent.execute_pass_2_hierarchy_recovery(state)

        # 5-6. Bounded Markovian analysis, then Critique & Revision Loop 2 on the candidate claims
        state = analyst_agent.execute_initial_analysis(state)
        state = evaluator_agent.evaluate_analyst_claims(state)
        state = analyst_agent.revise_after_claim_block(state)

        # 7. Draft -> review -> revise until the reviewer's gates pass (bounded)
        state = writer_agent.draft_explanation(state)
        for _ in range(state.max_retries + 1):
            if reviewer_agent.audit_draft(state):
                break
            state = writer_agent.revise(state, state.reviewer_issues)

        state = reviewer_agent.compound_institutional_memory(state)
        return self._build_success_response(
            state=state,
            zetaris_client=zetaris_client,
            world_model=world_model,
            llm_router=llm_router,
            trace_logger=trace_logger,
            duration_ms=self._elapsed(start_time),
        )

    # ------------------------------------------------------------------ response builders
    @staticmethod
    def _overall_confidence(state: InvestigationState) -> float:
        """Mean confidence of tested hypotheses, discounted 5% per hypothesis that could not be tested."""
        tested = [h["confidence"] for h in state.hypotheses if h["status"] not in ("blocked_untested", "pending")]
        untested = sum(1 for h in state.hypotheses if h["status"] == "blocked_untested")
        if not tested:
            return 0.0
        return round(max(0.0, min(1.0, mean(tested) * (1 - 0.05 * untested))), 3)

    def _build_blocked_response(
        self,
        state: InvestigationState,
        llm_router: LLMRouter,
        trace_logger: TraceLogger,
        duration_ms: float,
    ) -> RunResponse:
        contract_dict = state.execution_contract.to_dict() if state.execution_contract else {}
        return RunResponse(
            run_id=state.run_id,
            request_id=state.request_id,
            status="blocked",
            query=state.scout_evaluation.get("sanitized_query", state.user_query),
            sample_mode=state.sample_mode,
            executive_summary=f"SECURITY REFUSAL: {state.block_reason} Zero Zetaris SQL queries or LLM calls were executed.",
            why_explanation={
                "security_decision": "blocked_by_scout_intent_5_layer_guard",
                "reason": state.block_reason,
                "risk_assessment": state.scout_evaluation.get("risk", {}),
                "post_block_hmem_conflict_test": state.conflict_resolution_report,
            },
            taxonomic_anomaly_resolution={"status": "not_executed_due_to_security_block"},
            hypotheses=[],
            verified_findings=[],
            untested_factors=[],
            correlation_vs_causation_caveat="Execution halted at Layer 1 (Scout Intent Guard) prior to hypothesis testing.",
            recommended_actions=[
                {
                    "priority": "SECURITY_AUDIT",
                    "target_system": "Scout Intent Guard",
                    "action": "Review logs/scout_contracts.jsonl for the blocked payload and its failed guard layers.",
                    "quantified_impact": "No tool contract was issued: 0 SQL statements and 0 model calls executed.",
                }
            ],
            zetaris_lineage={"queries_executed_count": 0, "queries": []},
            meterless_engines={
                "scout_intent": {
                    "blocked": True,
                    "score": state.scout_evaluation.get("score", 0.0),
                    "risk": state.scout_evaluation.get("risk", {}),
                    "contract": contract_dict,
                },
                "hmem": {"conflict_resolution_test": state.conflict_resolution_report},
                "markovian": {"executed": False},
                "world_model": {"total_entities": 0},
            },
            provider_routing=llm_router.get_provider_status(),
            trace_summary={
                "total_events": len(trace_logger.events),
                "blocked_events": sum(1 for e in trace_logger.events if e.status == "blocked"),
            },
            trace_events=trace_logger.events,
            confidence=1.0,
            duration_ms=duration_ms,
        )

    def _build_error_response(
        self,
        state: InvestigationState,
        llm_router: LLMRouter,
        trace_logger: TraceLogger,
        exc: Exception,
        duration_ms: float,
    ) -> RunResponse:
        message = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:400]
        return RunResponse(
            run_id=state.run_id,
            request_id=state.request_id,
            status="error",
            query=state.user_query,
            sample_mode=state.sample_mode,
            executive_summary=f"Investigation aborted: {message}. Partial trace and lineage are attached.",
            why_explanation={"error": message},
            taxonomic_anomaly_resolution={"status": "not_completed"},
            hypotheses=[HypothesisItem(**h) for h in state.hypotheses],
            verified_findings=[],
            untested_factors=[],
            correlation_vs_causation_caveat="No conclusions are drawn from an incomplete run.",
            recommended_actions=[],
            evidence_ledger=state.evidence_ledger,
            zetaris_lineage={"queries_executed_count": len(state.sql_history), "sql_queries": state.sql_history},
            meterless_engines={},
            provider_routing=llm_router.get_provider_status(),
            trace_summary={"total_events": len(trace_logger.events)},
            trace_events=trace_logger.events,
            confidence=0.0,
            duration_ms=duration_ms,
        )

    def _build_success_response(
        self,
        *,
        state: InvestigationState,
        zetaris_client: ZetarisClient,
        world_model: WorldModelEngine,
        llm_router: LLMRouter,
        trace_logger: TraceLogger,
        duration_ms: float,
    ) -> RunResponse:
        wm_snapshot = world_model.context.build_context()
        contract_dict = state.execution_contract.to_dict() if state.execution_contract else {}
        mrk = state.markovian_telemetry
        tax = state.metrics.get("taxonomy", {})
        findings = {f["finding_id"].split("_")[0]: f for f in state.verified_findings}
        f2 = [f for f in state.verified_findings if f["finding_id"].startswith("F2_")]

        def driver(prefix: str, absent: str) -> str:
            f = findings.get(prefix)
            return f["explanation"] if f else absent

        orphans = tax.get("orphans", [])
        first = orphans[0] if orphans else {}
        taxonomic = {
            "dq_rule_triggered": "fk_sic_check",
            "usl_table": "lightning.metastore.usl_demo.sic_edgar_usl",
            "status": "clean" if not orphans else ("reconciled" if tax.get("reconciled_count") == len(orphans) else "partially_reconciled"),
            "orphan_entity": {
                "cik": first.get("cik"),
                "ticker": first.get("ticker"),
                "company_name": first.get("company_name"),
                "edgar_sic_code": first.get("edgar_sic_code"),
                "reconciled_leaf_sic_code": first.get("reconciled_leaf_sic_code"),
                "reconciled_hierarchy": first.get("reconciled_hierarchy"),
                "restored_fy2025_revenue_usd": first.get("restored_fy2025_revenue_usd"),
                "restored_q3_arr_usd": first.get("restored_q3_arr_usd"),
            }
            if first
            else {},
            "orphans": orphans,
            "restored_revenue_usd": tax.get("restored_revenue_usd", 0),
            "resolution_mode": (
                "warm_hmem_chunk0_recall"
                if state.warm_mode and not state.dq_revision_triggered
                else ("cold_dq_critique_and_pass2_hierarchy_recovery" if state.dq_revision_triggered else "no_reconciliation_needed")
            ),
            "leaf_codes_recovered": tax.get("leaf_codes_recovered", []),
            "group_357_leaf_codes_recovered": tax.get("leaf_codes_recovered", []),
        }

        return RunResponse(
            run_id=state.run_id,
            request_id=state.request_id,
            status="success",
            query=state.user_query,
            sample_mode=state.sample_mode,
            executive_summary=state.writer_draft.get("executive_summary", ""),
            why_explanation={
                "focus": state.focus,
                "primary_operational_driver": driver("F1", "No operational driver met the evidence bar."),
                "primary_data_discrepancy_driver": (
                    " ".join(f["explanation"] for f in f2) or "fk_sic_check found no taxonomy orphans in the joined evidence."
                ),
                "q4_forward_risk_driver": driver("F3", "No Q4 weather SLA exposure in the focus cohort."),
                "epistemological_boundary": (
                    "; ".join(f"{u['hypothesis_id']} ({u['title']}) moved to untested_factors: {u['reason_untested']}" for u in state.untested_factors)
                    or "All planned hypotheses had supporting evidence."
                ),
                "llm_narrative": state.writer_draft.get("llm_narrative", ""),
                "multivariate_correlations": state.analyst_draft.get("multivariate_correlations", {}),
            },
            taxonomic_anomaly_resolution=taxonomic,
            hypotheses=[HypothesisItem(**h) for h in state.hypotheses],
            verified_findings=state.verified_findings,
            multivariate_correlations=state.analyst_draft.get("multivariate_correlations", {}),
            enterprise_360_matrix=state.analyst_draft.get("enterprise_360_matrix", []),
            untested_factors=[{k: v for k, v in u.items() if k != "forbidden_terms"} for u in state.untested_factors],
            correlation_vs_causation_caveat=state.correlation_vs_causation_caveat,
            recommended_actions=state.recommended_actions,
            evidence_ledger=state.evidence_ledger,
            zetaris_lineage={
                "endpoint": zetaris_client.rest_url,
                "org_id": zetaris_client.org_id,
                "execution_modes": sorted({q["mode"] for q in zetaris_client.executed_queries}),
                "retriever_passes_executed": state.retriever_pass,
                "dq_revision_triggered": state.dq_revision_triggered,
                "queries_executed_count": len(state.sql_history),
                "sql_queries": state.sql_history,
                "segment_retention_summary": state.billing_segment_summary,
                "dq_invalid_rows": state.dq_invalid_rows,
                "enterprise_360_matrix": state.analyst_draft.get("enterprise_360_matrix", []),
            },
            meterless_engines={
                "hmem": {
                    "memory_mode": state.memory_mode,
                    "warm_mode_activated": state.warm_mode,
                    "rules_applied": state.learned_sic_rules,
                    "retrieved_count": state.retrieved_memories.get("total_matches", 0),
                    "retrieved_memories": state.retrieved_memories.get("memories", []),
                    "mined_record": state.mined_memory.get("record", {}),
                    "mined_records": state.mined_memory.get("records", []),
                    "feedback_applied": state.mined_memory.get("feedback_applied", []),
                    "sleep_preview": state.sleep_preview,
                    "dreams_proposed": state.dreams,
                },
                "markovian": {
                    "warm_mode": mrk.get("warm_mode", False),
                    "chunks_executed": mrk.get("chunks_executed", 0),
                    "stages": [c.get("stage") for c in mrk.get("chunks", [])],
                    "cold_baseline_chunks": mrk.get("cold_baseline_chunks", 0),
                    "cold_vs_warm_chunk_delta": mrk.get("cold_vs_warm_chunk_delta", 0),
                    "standardCumulative": mrk.get("standardCumulative", 0),
                    "totalTokensUsed": mrk.get("totalTokensUsed", 0),
                    "totalTokensSaved": mrk.get("totalTokensSaved", 0),
                    "efficiencyPercent": mrk.get("efficiencyPercent", 0.0),
                    "efficiencyBasis": mrk.get("efficiencyBasis", ""),
                    "specProjectedEfficiencyPercent": mrk.get("specProjectedEfficiencyPercent", 0.0),
                    "efficiencyCurve": mrk.get("efficiencyCurve", []),
                },
                "world_model": {
                    "total_entities": wm_snapshot["total_entities"],
                    "counts_by_type": wm_snapshot["counts_by_type"],
                    "sample_entity_ids": [e["entity_id"] for e in wm_snapshot["entities"][:8]],
                },
                "scout_intent": {
                    "blocked": False,
                    "score": state.scout_evaluation.get("score", 0.0),
                    "intent": state.scout_evaluation.get("intent", ""),
                    "contract": contract_dict,
                },
            },
            provider_routing={**llm_router.get_provider_status(), "calls": llm_router.call_history},
            trace_summary={
                "total_events": len(trace_logger.events),
                "revisions_triggered": sum(1 for e in trace_logger.events if e.status == "needs_revision"),
                "unsupported_claims_flagged": len(state.untested_factors),
                "claims_blocked": state.unsupported_claims_blocked,
                "agents_participated": list(dict.fromkeys(e.agent_name for e in trace_logger.events)),
                "restored_revenue": fmt_usd(tax.get("restored_revenue_usd", 0)),
            },
            trace_events=trace_logger.events,
            confidence=self._overall_confidence(state),
            duration_ms=duration_ms,
        )
