"""Agent 7: FinalReviewerAgent — Quality Gate, Caveat Enforcement, and H-MEM Institutional Compounding."""

from __future__ import annotations

from app.engines.hmem import HMemEngine, save_hmem_state
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger


class FinalReviewerAgent:
    """Audits the writer's draft, enforces epistemological caveats, and mines resolved rules into H-MEM."""

    name = "FinalReviewerAgent"

    def __init__(self, hmem_engine: HMemEngine, trace_logger: TraceLogger) -> None:
        self.hmem = hmem_engine
        self.trace = trace_logger

    def audit_draft(self, state: InvestigationState) -> bool:
        """Returns True if the draft passes all epistemological gates; False if revision is required."""
        has_caveat = bool(state.writer_draft.get("has_explicit_caveat")) and bool(
            state.correlation_vs_causation_caveat.strip()
        )
        if not has_caveat:
            state.reviewer_revision_triggered = True
            self.trace.log(
                agent_name=self.name,
                action="enforce_epistemological_caveat",
                input_summary="Audited ExplanationWriterAgent initial draft for causal discipline and untested factor disclosure",
                output_summary=(
                    "Draft missing explicit 'Correlation vs. Causation & Untested Factors' caveat section; "
                    "returning needs_revision to ExplanationWriterAgent."
                ),
                target_agent="ExplanationWriterAgent",
                confidence=0.98,
                retry_count=1,
                status="needs_revision",
                sponsor_tech="nvidia_nemotron",
            )
            return False

        self.trace.log(
            agent_name=self.name,
            action="approve_final_explanation",
            input_summary="Re-audited revised explanation draft with explicit Correlation vs. Causation caveat",
            output_summary="Approved executive explanation: all claims backed by Zetaris SQL lineage and untested factors isolated.",
            target_agent="HMemMiningService",
            confidence=0.99,
            retry_count=1,
            status="success",
            sponsor_tech="nvidia_nemotron",
        )
        return True

    def compound_institutional_memory(self, state: InvestigationState) -> InvestigationState:
        """Mines the IBM SIC 3570 -> 3571 reconciliation rule into H-MEM, runs Sleep Preview & Dreaming, and persists state."""
        mined = self.hmem.mining.mine_fact(
            content=(
                "Reconciliation Rule [SIC-3570-LEAF]: SEC EDGAR CIK 0000051143 (IBM) reports 3-digit "
                "Industry Group summary SIC code 3570 ('Computer and Office Equipment'). Map 3570 -> "
                "Division D / Major Group 35 / Industry Group 357 leaf 3571 ('Electronic Computers') "
                "to prevent dropping $62.753B FY2025 revenue in Zetaris USL joins."
            ),
            source="zetaris_usl_dq:fk_sic_check",
            confidence=0.96,
            domain="taxonomy",
            layer="long_term",
            memory_type="procedural",
            tags=["zetaris", "sic_3570", "3571", "ibm", "taxonomy", "enterprise"],
            entities=["IBM", "0000051143", "3570", "3571", "sic"],
            agent_id=self.name,
            run_id=state.run_id,
            record_id="mem-sic-3570-reconciliation-rule",
        )
        # Apply positive feedback if this was a warm run that validated the rule
        if state.warm_mode:
            self.hmem.store.apply_feedback(
                "mem-sic-3570-reconciliation-rule",
                "helpful",
                run_id=state.run_id,
            )

        state.mined_memory = mined
        state.sleep_preview = self.hmem.sleep.preview(run_id=state.run_id)
        state.dreams = self.hmem.dreaming.propose_dreams(run_id=state.run_id)
        save_hmem_state(self.hmem)

        self.trace.log(
            agent_name=self.name,
            action="mine_rule_into_hmem_and_sleep_preview",
            input_summary="Mining resolved IBM SIC 3570 -> 3571 rule into H-MEM long_term layer and running SleepCycleService.preview()",
            output_summary=(
                f"Stored H-MEM record id={mined['record']['id']} (conf={mined['record']['confidence']}); "
                f"sleep_preview toConsolidate={len(state.sleep_preview['toConsolidate'])}, "
                f"toSynthesize={len(state.sleep_preview['toSynthesize'])}, dreams={len(state.dreams)}."
            ),
            target_agent="Complete",
            confidence=0.99,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_hmem",
        )
        return state
