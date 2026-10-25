"""Agent 2: PlannerAgent — Decomposes the query into falsifiable hypotheses and a retrieval plan."""

from __future__ import annotations

from typing import Any, Dict, List

from app.engines.world_model import WorldModelEngine
from app.llm import LLMRouter
from app.orchestration.state import InvestigationState
from app.reconciliation import edgar_sic_join_sql
from app.tracing import TraceLogger

# Each hypothesis names the dataset purposes that must return rows before any claim on it can be asserted.
# The EvidenceEvaluatorAgent enforces this generically; nothing downstream special-cases H4.
HYPOTHESIS_TEMPLATES: List[Dict[str, Any]] = [
    {
        "hypothesis_id": "H1",
        "title": "Cohort Retention & ARR Contraction Variance",
        "description": "Test whether the focus cohort's Q2->Q3 net revenue retention contracted, and by more than the comparison cohort.",
        "requires": ["billing_cohorts"],
        "evidence_tables": ["erp.customer_churn_billing"],
    },
    {
        "hypothesis_id": "H2",
        "title": "Cross-Source SEC EDGAR vs. US SIC V4 Taxonomic Integrity",
        "description": "Verify that every SEC EDGAR self-reported SIC code joins to a 4-digit US SIC V4 leaf, so no filer is silently dropped from cohort reporting.",
        "requires": ["edgar_sic_join", "dq_fk_sic_check"],
        "evidence_tables": [
            "lightning.datasource.rest.sec_edgar",
            "lightning.datasource.rest.company_dns",
            "lightning.metastore.usl_demo.sic_edgar_usl",
        ],
    },
    {
        "hypothesis_id": "H3",
        "title": "Support Escalation, Gateway Latency & Retention Correlation",
        "description": "Test whether Q3 retention loss co-moves with P1 escalations, p99 API latency, batch-sync timeouts and MTTR across accounts.",
        "requires": ["support_telemetry", "billing_cohorts"],
        "evidence_tables": ["ops.support_tickets_usage", "erp.customer_churn_billing"],
    },
    {
        "hypothesis_id": "H4",
        "title": "Enterprise Pricing-Tier & Rate-Card Change Impact",
        "description": "Test whether Q3 seat or tier price increases drove contraction in the focus cohort.",
        "requires": ["pricing_tier_changes"],
        "evidence_tables": ["erp.pricing_tier_changes"],
    },
]

# Common alternative explanations that are probed for data but are not primary hypotheses.
CONFOUNDER_PROBES: List[Dict[str, Any]] = [
    {
        "factor_id": "UF_COMPETITOR_DISPLACEMENT",
        "title": "Competitor Contract Displacement / Bundling",
        "purpose": "competitor_loss_reasons",
        "sql": "SELECT * FROM crm.opportunity_loss_reasons WHERE fiscal_quarter = '2026-Q3' LIMIT 50",
        "required_data_to_test": "crm.opportunity_loss_reasons joined on account CIK (win/loss notes).",
    },
]


class PlannerAgent:
    """Decomposes the investigation into explicit, pending hypotheses and a Zetaris retrieval plan."""

    name = "PlannerAgent"

    def __init__(
        self,
        world_model: WorldModelEngine,
        llm_router: LLMRouter,
        trace_logger: TraceLogger,
    ) -> None:
        self.world_model = world_model
        self.llm = llm_router
        self.trace = trace_logger

    def _retrieval_plan(self, state: InvestigationState) -> List[Dict[str, Any]]:
        learned = {code: rule["leaf_code"] for code, rule in state.learned_sic_rules.items()}
        plan = [
            {
                "purpose": "segment_summary",
                "hypotheses": ["H1"],
                "sql": (
                    "SELECT industry_segment, COUNT(*) AS accounts_count, SUM(q3_arr_usd) AS q3_arr_total_usd, "
                    "AVG(q3_net_retention_pct) AS avg_q3_net_retention_pct "
                    "FROM erp.customer_churn_billing GROUP BY industry_segment"
                ),
            },
            {
                "purpose": "billing_cohorts",
                "hypotheses": ["H1", "H3"],
                "sql": "SELECT * FROM erp.customer_churn_billing LIMIT 1000",
            },
            {
                # Raw filings, so DQ orphans the naive join drops can still be described (ticker, revenue, group).
                "purpose": "edgar_filings",
                "hypotheses": ["H2"],
                "sql": "SELECT * FROM edgar.company_facts LIMIT 1000",
            },
            {
                "purpose": "edgar_sic_join",
                "hypotheses": ["H2"],
                # Pass 1 applies only rules H-MEM already holds; with none it is the naive equi-join.
                "sql": edgar_sic_join_sql(learned),
            },
            {
                "purpose": "support_telemetry",
                "hypotheses": ["H3"],
                "sql": (
                    "SELECT s.*, b.industry_segment, b.sic_code_edgar, b.q2_arr_usd, b.q3_arr_usd, "
                    "b.q3_net_retention_pct, b.q3_arr_delta_usd, b.sla_credits_issued_usd, "
                    "b.weather_sla_exposure_usd, b.primary_datacenter_region, b.churn_risk_tier "
                    "FROM ops.support_tickets_usage s JOIN erp.customer_churn_billing b ON s.cik = b.cik LIMIT 1000"
                ),
            },
            {
                "purpose": "pricing_tier_changes",
                "hypotheses": ["H4"],
                "sql": "SELECT * FROM erp.pricing_tier_changes WHERE effective_quarter = '2026-Q3' LIMIT 50",
            },
        ]
        for probe in CONFOUNDER_PROBES:
            plan.append({"purpose": probe["purpose"], "hypotheses": [], "sql": probe["sql"]})
        return plan

    def execute(self, state: InvestigationState, revision_reason: str = "") -> InvestigationState:
        is_revision = bool(revision_reason)
        if is_revision:
            state.plan_revision_count += 1
        else:
            state.hypotheses = []
            state.hypothesis_requirements = {}
            for tpl in HYPOTHESIS_TEMPLATES:
                state.hypotheses.append(
                    {
                        "hypothesis_id": tpl["hypothesis_id"],
                        "title": tpl["title"],
                        "description": tpl["description"],
                        "status": "pending",
                        "confidence": 0.0,
                        "evidence_tables": list(tpl["evidence_tables"]),
                        "sql_queries": [],
                        "finding_summary": "Pending: not yet tested against retrieved evidence.",
                    }
                )
                state.hypothesis_requirements[tpl["hypothesis_id"]] = list(tpl["requires"])
            state.retrieval_plan = self._retrieval_plan(state)
            for step in state.retrieval_plan:
                for hid in step["hypotheses"]:
                    hyp = next(h for h in state.hypotheses if h["hypothesis_id"] == hid)
                    hyp["sql_queries"].append(step["sql"])

        if is_revision:
            fallback = (
                f"Revision #{state.plan_revision_count}: {revision_reason} Re-planning H2 retrieval as a "
                "hierarchy-aware join: expand each orphaned summary SIC through company_dns.sic_codes leaves."
            )
        else:
            fallback = (
                f"Decomposed '{state.user_query[:120]}' into {len(state.hypotheses)} falsifiable hypotheses "
                f"({', '.join(h['hypothesis_id'] for h in state.hypotheses)}) and a {len(state.retrieval_plan)}-query "
                f"retrieval plan; {len(state.learned_sic_rules)} H-MEM reconciliation rule(s) applied to Pass 1."
            )
        llm_out = self.llm.generate(
            task="plan",
            system_prompt=(
                "You are PlannerAgent in AuditWhy. Restate the investigation plan in two sentences. "
                "Do not assert findings; hypotheses are untested."
            ),
            user_prompt=f"Query: {state.user_query}\nPlan: {fallback}",
            fallback_text=fallback,
        )

        wm_res = self.world_model.pipeline.ingest(
            [
                {
                    "entity_type": "investigation_plan",
                    "name": f"Plan-{state.run_id}",
                    "warm_mode": state.warm_mode,
                    "revision": state.plan_revision_count,
                    "hypotheses": [h["hypothesis_id"] for h in state.hypotheses],
                    "revision_reason": revision_reason,
                }
            ],
            source="planner_agent",
            run_id=state.run_id,
        )

        self.trace.log(
            agent_name=self.name,
            action="revise_investigation_plan" if is_revision else "decompose_hypotheses",
            input_summary=(
                f"Evaluator critique: {revision_reason}"
                if is_revision
                else f"Decomposing query into {len(state.hypotheses)} hypotheses (memory_mode={state.memory_mode}, warm={state.warm_mode})"
            ),
            output_summary=f"{llm_out['content']} | World Model entities: {wm_res['total_entities']}.",
            target_agent="DataRetrieverAgent",
            confidence=0.9,
            retry_count=state.plan_revision_count,
            status="retrying" if is_revision else "success",
            sponsor_tech="nvidia_nemotron",
        )
        return state
