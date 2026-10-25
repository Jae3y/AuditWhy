"""Agent 4: EvidenceEvaluatorAgent — Zetaris DQ verification and provenance gate on analyst claims."""

from __future__ import annotations

from typing import Any, Dict, List

from app.adapters.zetaris import ZetarisClient
from app.analytics import fmt_usd
from app.llm import LLMRouter
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger

DQ_SQL = "SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl"
MIN_CORRELATION = 0.7
MIN_CORRELATION_SAMPLES = 5


class EvidenceEvaluatorAgent:
    """Independently re-checks evidence: runs its own DQ query and blocks claims that lack row-level provenance."""

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
        """Critique & Revision Loop 1.

        Runs fk_sic_check independently of the retriever and compares the orphans it reports against the filings
        actually present in the joined evidence. Any orphan missing from the join means the join silently dropped
        data, so the plan is sent back for revision.
        """
        dq_res = self.zetaris.run_sql(DQ_SQL, contract=state.execution_contract, tool_name="verify_dq", agent_name=self.name)
        state.dq_invalid_rows = dq_res["rows"]
        # Zero orphans is a valid, evidence-bearing result for a DQ rule (it proves the join is clean).
        state.datasets["dq_fk_sic_check"] = {**dq_res, "empty_is_evidence": True}
        state.sql_history.append(DQ_SQL)
        state.evidence_ledger.append(
            {
                "pass": state.retriever_pass,
                "agent": self.name,
                "purpose": "dq_fk_sic_check",
                "hypotheses": ["H2"],
                "tables": ["lightning.metastore.usl_demo.sic_edgar_usl"],
                "sql": DQ_SQL,
                "status": dq_res["status"],
                "row_count": dq_res["row_count"],
                "engine": dq_res.get("execution_engine"),
            }
        )

        # A filer counts only if it carries a leaf: the hierarchy-aware join is a LEFT JOIN, so a bad mapping still
        # returns the row with a NULL leaf and would otherwise look reconciled.
        joined_ciks = {r.get("cik") for r in state.edgar_sic_joined_rows if r.get("leaf_sic_code")}
        dropped = [o for o in state.dq_invalid_rows if o.get("cik") not in joined_ciks]
        state.dq_orphans_dropped = dropped
        is_first_check = not state.dq_revision_triggered

        if dropped:
            lost_revenue = sum(int(o.get("revenue_2025") or 0) for o in dropped)
            detail = "; ".join(
                f"{o.get('ticker')} (CIK {o.get('cik')}) reports summary SIC {o.get('invalid_sic_code')}, "
                f"{fmt_usd(o.get('revenue_2025'))} FY2025 revenue"
                for o in dropped
            )
            reason = (
                f"fk_sic_check flagged {len(state.dq_invalid_rows)} orphan(s); {len(dropped)} missing from the EDGAR x SIC "
                f"join ({detail}). Naive join understates cohort revenue by {fmt_usd(lost_revenue)}."
            )
            state.dq_revision_triggered = True
            state.dq_reconciled = False
            state.analyst_draft["dq_revision_reason"] = reason
            fallback = f"needs_revision: {reason}"
            status, target, conf = "needs_revision", "PlannerAgent", 0.99
            action = "detect_zetaris_dq_orphan"
        else:
            state.dq_reconciled = True
            if not state.dq_invalid_rows:
                fallback = "fk_sic_check returned 0 orphans: every EDGAR SIC joins to a 4-digit leaf."
            elif is_first_check and state.learned_sic_rules:
                fallback = (
                    f"All {len(state.dq_invalid_rows)} fk_sic_check orphan(s) already present in the Pass 1 join via "
                    f"H-MEM rule(s) {sorted(state.learned_sic_rules)}; no revision needed."
                )
            else:
                fallback = (
                    f"Re-verified after revision: all {len(state.dq_invalid_rows)} fk_sic_check orphan(s) "
                    f"({', '.join(str(o.get('ticker')) for o in state.dq_invalid_rows)}) are now present in the join."
                )
            status, target, conf = "success", "MarkovianAnalystAgent", 0.99
            action = "verify_warm_dq_reconciliation" if (is_first_check and state.learned_sic_rules) else "verify_dq_reconciliation"

        llm_eval = self.llm.generate(
            task="evaluate",
            system_prompt="You are EvidenceEvaluatorAgent. Summarize this Zetaris Data Quality verdict in one sentence; do not change it.",
            user_prompt=fallback,
            fallback_text=fallback,
        )
        self.trace.log(
            agent_name=self.name,
            action=action,
            input_summary=(
                f"Ran `{DQ_SQL}` against {len(state.edgar_sic_joined_rows)} joined filings "
                f"(retriever pass {state.retriever_pass})"
            ),
            output_summary=llm_eval["content"],
            target_agent=target,
            confidence=conf,
            retry_count=state.plan_revision_count,
            status=status,
            sponsor_tech="zetaris",
        )
        return state

    def evaluate_analyst_claims(self, state: InvestigationState) -> InvestigationState:
        """Critique & Revision Loop 2: gate every candidate claim on provenance, then on statistics."""
        verdicts: Dict[str, Dict[str, Any]] = {}
        blocked: List[Dict[str, Any]] = []
        for claim in state.candidate_claims:
            required = claim.get("requires", [])
            missing = [p for p in required if not state.dataset_has_evidence(p)]
            if missing:
                evidence = [
                    f"{p}: {(state.datasets.get(p) or {}).get('status', 'NOT_RUN')}, "
                    f"{(state.datasets.get(p) or {}).get('row_count', 0)} rows"
                    for p in missing
                ]
                verdict = {"verdict": "blocked", "reason": f"No row-level provenance ({'; '.join(evidence)})."}
                blocked.append(claim)
            elif claim.get("statistic") == "pearson_r":
                r = float(claim.get("value") or 0.0)
                n = int(claim.get("n") or 0)
                if n >= MIN_CORRELATION_SAMPLES and abs(r) >= MIN_CORRELATION:
                    verdict = {"verdict": "supported_correlational", "reason": f"|r|={abs(r):.3f} >= {MIN_CORRELATION} with n={n}."}
                else:
                    verdict = {"verdict": "inconclusive", "reason": f"|r|={abs(r):.3f}, n={n} below the {MIN_CORRELATION}/{MIN_CORRELATION_SAMPLES} bar."}
            else:
                verdict = {"verdict": "supported", "reason": "All required datasets returned rows."}
            verdicts[claim["claim_id"]] = {**verdict, "hypothesis_id": claim.get("hypothesis_id"), "statement": claim["statement"]}
        state.claim_verdicts = verdicts

        for claim in blocked:
            state.unsupported_claims_blocked.append(
                f"{claim.get('hypothesis_id') or claim['claim_id']}: {claim['statement']} -> {verdicts[claim['claim_id']]['reason']}"
            )

        supported = sum(1 for v in verdicts.values() if v["verdict"].startswith("supported"))
        fallback = (
            f"Audited {len(verdicts)} candidate claim(s): {supported} supported, "
            f"{sum(1 for v in verdicts.values() if v['verdict'] == 'inconclusive')} inconclusive, {len(blocked)} blocked"
            + (f" ({'; '.join(c['claim_id'] for c in blocked)})" if blocked else "")
            + ". Blocked claims must move to untested_factors."
        )
        llm_eval = self.llm.generate(
            task="evaluate",
            system_prompt="You are EvidenceEvaluatorAgent. Summarize the claim audit in one sentence; never un-block a claim.",
            user_prompt=fallback,
            fallback_text=fallback,
        )
        self.trace.log(
            agent_name=self.name,
            action="flag_unsupported_claim" if blocked else "approve_analyst_claims",
            input_summary=(
                f"Audited {len(verdicts)} MarkovianAnalystAgent claims against the evidence ledger "
                f"({len(state.evidence_ledger)} Zetaris queries)"
            ),
            output_summary=llm_eval["content"],
            target_agent="MarkovianAnalystAgent",
            confidence=0.98,
            retry_count=1 if blocked else 0,
            status="partial_success" if blocked else "success",
            sponsor_tech="nvidia_nemotron",
        )
        return state
