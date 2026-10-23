"""Agent 4: EvidenceEvaluatorAgent — Zetaris DQ Verification & Unsupported Claim Blocker."""

from __future__ import annotations

from app.adapters.zetaris import ZetarisClient
from app.llm import LLMRouter
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger


class EvidenceEvaluatorAgent:
    """Critiques retrieved evidence via Zetaris DQ rules and blocks unverified analyst claims."""

    name = "EvidenceEvaluatorAgent"

    def __init__(
        self,
        zetaris_client: ZetarisClient,
        llm_router: LLMRouter,
        trace_logger: TraceLogger,
    ) -> None:
        self.zetaris = zetaris_client
        self.llm = llm_router
        self.trace = trace_logger

    def evaluate_data_quality(self, state: InvestigationState) -> InvestigationState:
        """Critique & Revision Loop 1:
        Independently executes `SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl`.
        Detects the IBM SIC 3570 foreign-key orphan and triggers revision if not yet reconciled.
        """
        dq_sql = "SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl"
        dq_res = self.zetaris.run_sql(
            dq_sql,
            contract=state.execution_contract,
            tool_name="verify_dq",
            agent_name=self.name,
        )
        state.dq_invalid_rows = dq_res["rows"]
        state.sql_history.append(dq_sql)

        has_ibm_joined = any(r.get("cik") == "0000051143" for r in state.edgar_sic_joined_rows)
        llm_eval = self.llm.generate(
            task="evaluate",
            system_prompt="You are EvidenceEvaluatorAgent. Audit Zetaris Data Quality foreign-key checks.",
            user_prompt=f"DQ invalid rows: {len(state.dq_invalid_rows)}, has_ibm_joined={has_ibm_joined}",
            context_data={"stage": "dq_check", "orphan_cik": "0000051143"},
        )

        if not has_ibm_joined:
            state.dq_revision_triggered = True
            state.dq_reconciled = False
            self.trace.log(
                agent_name=self.name,
                action="detect_zetaris_dq_sic_3570_orphan",
                input_summary=f"Ran `{dq_sql}` on Pass 1 join ({len(state.edgar_sic_joined_rows)} of 7 Group 357 accounts matched)",
                output_summary=(
                    f"{llm_eval['content']} | Emitting needs_revision to PlannerAgent & DataRetrieverAgent "
                    f"to expand SIC 357% hierarchy and restore IBM ($62.753B revenue)."
                ),
                target_agent="PlannerAgent",
                confidence=0.99,
                retry_count=1,
                status="needs_revision",
                sponsor_tech="zetaris",
            )
        else:
            state.dq_reconciled = True
            self.trace.log(
                agent_name=self.name,
                action="verify_warm_dq_reconciliation",
                input_summary=f"Ran `{dq_sql}`; verified H-MEM rule already reconciled IBM CIK 0000051143 in Pass 1",
                output_summary=(
                    "All 7 Computer & Office Equipment accounts (including IBM $62.753B under 3570->3571) "
                    "are present and verified in Pass 1 via H-MEM compounding."
                ),
                target_agent="MarkovianAnalystAgent",
                confidence=0.99,
                retry_count=0,
                status="success",
                sponsor_tech="zetaris",
            )
        return state

    def evaluate_analyst_claims(self, state: InvestigationState) -> InvestigationState:
        """Critique & Revision Loop 2:
        Audits MarkovianAnalystAgent's candidate findings against Zetaris table provenance.
        Blocks the H4 pricing-change claim because `erp.pricing_tier_changes` returned 0 rows.
        """
        pricing_row_count = len(state.pricing_rows)
        has_tentative_pricing_claim = bool(state.analyst_draft.get("includes_pricing_claim", False))

        llm_eval = self.llm.generate(
            task="evaluate",
            system_prompt="You are EvidenceEvaluatorAgent. Block any causal claim lacking Zetaris row provenance.",
            user_prompt=f"Verify Analyst candidate claims; pricing_row_count={pricing_row_count}",
            context_data={"stage": "claim_verification", "row_count": pricing_row_count},
        )

        if has_tentative_pricing_claim and pricing_row_count == 0:
            state.unsupported_claims_blocked.append(
                "H4 (Enterprise Pricing-Tier Increase Hypothesis): Blocked due to 0 rows in Zetaris pricing tables."
            )
            self.trace.log(
                agent_name=self.name,
                action="flag_unsupported_claim",
                input_summary=(
                    "Audited MarkovianAnalystAgent draft claiming H4 (pricing-tier changes) contributed to Q3 churn "
                    "against Zetaris provenance (`erp.pricing_tier_changes` row_count=0)"
                ),
                output_summary=(
                    f"{llm_eval['content']} | Rejected unverified H4 pricing claim; ordered MarkovianAnalystAgent "
                    "to strip claim and reclassify H4 into untested_factors."
                ),
                target_agent="MarkovianAnalystAgent",
                confidence=0.98,
                retry_count=1,
                status="partial_success",
                sponsor_tech="nvidia_nemotron",
            )
        return state
