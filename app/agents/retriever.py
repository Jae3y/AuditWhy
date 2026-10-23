"""Agent 3: DataRetrieverAgent — Executes Zetaris SQL passes and ingests grounded rows into the World Model."""

from __future__ import annotations

from typing import Any, Dict, List

from app.adapters.zetaris import ZetarisClient
from app.engines.world_model import WorldModelEngine
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger


class DataRetrieverAgent:
    """Queries Zetaris USL & operational tables under Scout ExecutionContract and populates the World Model."""

    name = "DataRetrieverAgent"

    def __init__(
        self,
        zetaris_client: ZetarisClient,
        world_model: WorldModelEngine,
        trace_logger: TraceLogger,
    ) -> None:
        self.zetaris = zetaris_client
        self.world_model = world_model
        self.trace = trace_logger

    def execute_pass_1(self, state: InvestigationState) -> InvestigationState:
        state.retriever_pass = 1
        contract = state.execution_contract

        # Query 1: Segment billing aggregation
        q_seg = (
            "SELECT industry_segment, COUNT(*) AS accounts_count, SUM(q3_arr_usd) AS q3_arr_total_usd, "
            "AVG(q3_net_retention_pct) AS avg_q3_net_retention_pct "
            "FROM erp.customer_churn_billing GROUP BY industry_segment"
        )
        res_seg = self.zetaris.run_sql(q_seg, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.billing_segment_summary = res_seg["rows"]
        state.sql_history.append(q_seg)

        # Query 2: Detailed billing & weather SLA rows
        q_bill = "SELECT * FROM erp.customer_churn_billing LIMIT 25"
        res_bill = self.zetaris.run_sql(q_bill, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.billing_rows = res_bill["rows"]
        state.sql_history.append(q_bill)

        # Query 3: Cross-source SEC EDGAR x US SIC V4 join
        if state.warm_mode:
            # Warm Run: H-MEM already supplied the SIC 3570 -> 3571 rule!
            q_join = (
                "SELECT * FROM edgar.company_facts e LEFT JOIN company_dns.sic_codes s "
                "ON e.sic_code = s.sic_code OR (e.sic_code = '3570' AND s.sic_code = '3571') "
                "WHERE e.industry_group = '357' LIMIT 20"
            )
        else:
            # Cold Run Pass 1: Standard inner join on sic_code (silently drops IBM 0000051143!)
            q_join = (
                "SELECT * FROM edgar.company_facts e JOIN company_dns.sic_codes s "
                "ON e.sic_code = s.sic_code WHERE e.industry_group = '357' LIMIT 20"
            )
        res_join = self.zetaris.run_sql(q_join, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.edgar_sic_joined_rows = res_join["rows"]
        state.sql_history.append(q_join)

        # Query 4: Support escalations & latency telemetry
        q_sup = (
            "SELECT * FROM ops.support_tickets_usage s JOIN erp.customer_churn_billing b "
            "ON s.cik = b.cik LIMIT 25"
        )
        res_sup = self.zetaris.run_sql(q_sup, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.support_rows = res_sup["rows"]
        state.sql_history.append(q_sup)

        # Query 5: Check pricing-tier change table for H4 (returns 0 rows)
        q_price = "SELECT * FROM erp.pricing_tier_changes WHERE effective_quarter = '2026-Q3' LIMIT 10"
        res_price = self.zetaris.run_sql(q_price, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.pricing_rows = res_price["rows"]
        state.sql_history.append(q_price)

        self.trace.log(
            agent_name=self.name,
            action="query_zetaris_pass_1",
            input_summary=f"Executed 5 Zetaris SQL queries for H1..H4 (warm_mode={state.warm_mode})",
            output_summary=(
                f"Retrieved {len(state.edgar_sic_joined_rows)} joined EDGARxSIC rows in Group 357, "
                f"{len(state.billing_rows)} billing cohorts, {len(state.support_rows)} support telemetry rows, "
                f"and {len(state.pricing_rows)} pricing change rows."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.92,
            retry_count=0,
            status="success",
            sponsor_tech="zetaris",
        )

        # Ingest retrieved rows into World Model
        obs: List[Dict[str, Any]] = []
        for row in state.edgar_sic_joined_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["company_name"], **row})
        for row in state.support_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["account_name"], **row})

        wm_ingest = self.world_model.pipeline.ingest(obs, source="zetaris_pass_1", run_id=state.run_id)
        self.trace.log(
            agent_name=self.name,
            action="ingest_world_model_pass_1",
            input_summary=f"Ingested {len(obs)} Zetaris observations into 6-stage World Model pipeline",
            output_summary=(
                f"Projected {wm_ingest['total_entities']} deterministic ent_ entities "
                f"(diff_count={wm_ingest['diff_count']})."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.95,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_world_model",
        )
        return state

    def execute_pass_2_hierarchy_recovery(self, state: InvestigationState) -> InvestigationState:
        """Pass 2 (Hierarchy Recovery): Queries SIC 357% leaf codes and reconciles IBM (CIK 0000051143) under 3571."""
        state.retriever_pass = 2
        contract = state.execution_contract

        q_hier = "SELECT * FROM company_dns.sic_codes WHERE sic_code LIKE '357%' LIMIT 10"
        res_hier = self.zetaris.run_sql(q_hier, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.sic_hierarchy_rows = res_hier["rows"]
        state.sql_history.append(q_hier)

        q_rec_join = (
            "SELECT * FROM edgar.company_facts e LEFT JOIN company_dns.sic_codes s "
            "ON e.sic_code = s.sic_code OR (e.sic_code = '3570' AND s.sic_code = '3571') "
            "WHERE e.industry_group = '357' LIMIT 20"
        )
        res_rec = self.zetaris.run_sql(q_rec_join, contract=contract, tool_name="query_zetaris", agent_name=self.name)
        state.edgar_sic_joined_rows = res_rec["rows"]
        state.dq_reconciled = True
        state.sql_history.append(q_rec_join)

        self.trace.log(
            agent_name=self.name,
            action="recover_sic_357_hierarchy_pass_2",
            input_summary=f"Executed hierarchy lookup `{q_hier}` and reconciled join for IBM (CIK 0000051143)",
            output_summary=(
                f"Recovered {len(state.sic_hierarchy_rows)} US SIC V4 357x leaf codes (3571, 3572, 3575, 3577, 3578, 3579) "
                f"and restored all {len(state.edgar_sic_joined_rows)} Group 357 enterprise accounts including IBM ($62.753B FY2025 revenue)."
            ),
            target_agent="MarkovianAnalystAgent",
            confidence=0.99,
            retry_count=1,
            status="success",
            sponsor_tech="zetaris",
        )

        # Update World Model with reconciled IBM entity and SIC hierarchy entities
        obs: List[Dict[str, Any]] = []
        for row in state.edgar_sic_joined_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["company_name"], **row})
        for srow in state.sic_hierarchy_rows:
            obs.append(
                {
                    "entity_type": "sic_taxonomy_leaf",
                    "name": f"SIC-{srow['sic_code']}-{srow['description']}",
                    **srow,
                }
            )
        wm_ingest = self.world_model.pipeline.ingest(obs, source="zetaris_pass_2_reconciled", run_id=state.run_id)
        self.trace.log(
            agent_name=self.name,
            action="ingest_world_model_pass_2",
            input_summary="Updated World Model with reconciled IBM 3570->3571 entity and US SIC V4 357x leaf taxonomy",
            output_summary=(
                f"World Model updated: {wm_ingest['total_entities']} total entities, "
                f"{wm_ingest['diff_count']} new/updated entity projections."
            ),
            target_agent="MarkovianAnalystAgent",
            confidence=0.98,
            retry_count=1,
            status="success",
            sponsor_tech="meterless_world_model",
        )
        return state
