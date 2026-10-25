"""Agent 3: DataRetrieverAgent — Executes the Zetaris retrieval plan and grounds rows in the World Model."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.adapters.zetaris import ZetarisClient
from app.engines.world_model import WorldModelEngine
from app.orchestration.state import InvestigationState
from app.reconciliation import build_leaf_mapping, edgar_sic_join_sql, is_valid_sic_code
from app.tracing import TraceLogger

_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", re.IGNORECASE)


class DataRetrieverAgent:
    """Queries Zetaris USL & operational tables under the Scout ExecutionContract and populates the World Model."""

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

    def _run(self, state: InvestigationState, purpose: str, sql: str, hypotheses: List[str], pass_no: int) -> Dict[str, Any]:
        res = self.zetaris.run_sql(
            sql,
            limit=1000,
            contract=state.execution_contract,
            tool_name="query_zetaris",
            agent_name=self.name,
        )
        state.datasets[purpose] = res
        state.sql_history.append(sql)
        state.evidence_ledger.append(
            {
                "pass": pass_no,
                "agent": self.name,
                "purpose": purpose,
                "hypotheses": hypotheses,
                "tables": list(dict.fromkeys(_TABLE_REF.findall(sql))),
                "sql": sql,
                "status": res.get("status"),
                "row_count": int(res.get("row_count") or 0),
                "engine": res.get("execution_engine"),
            }
        )
        return res

    def _ingest_accounts(self, state: InvestigationState, source: str) -> Dict[str, Any]:
        """Ground each source's rows as enterprise_account observations so the World Model resolves them by name."""
        obs: List[Dict[str, Any]] = []
        for row in state.edgar_sic_joined_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["company_name"], **row})
        for row in state.billing_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["account_name"], **row})
        for row in state.support_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["account_name"], **row})
        return self.world_model.pipeline.ingest(obs, source=source, run_id=state.run_id)

    def execute_pass_1(self, state: InvestigationState) -> InvestigationState:
        state.retriever_pass = 1
        for step in state.retrieval_plan:
            self._run(state, step["purpose"], step["sql"], step["hypotheses"], pass_no=1)

        state.billing_segment_summary = state.dataset_rows("segment_summary")
        state.billing_rows = state.dataset_rows("billing_cohorts")
        state.edgar_sic_joined_rows = state.dataset_rows("edgar_sic_join")
        state.support_rows = state.dataset_rows("support_telemetry")
        for code, rule in state.learned_sic_rules.items():
            state.reconciliation_map[code] = {**rule, "source": "hmem"}

        empty = [e["purpose"] for e in state.evidence_ledger if e["pass"] == 1 and e["row_count"] == 0]
        self.trace.log(
            agent_name=self.name,
            action="query_zetaris_pass_1",
            input_summary=(
                f"Executed {len(state.retrieval_plan)} planned Zetaris queries "
                f"(H-MEM rules applied to EDGAR x SIC join: {sorted(state.learned_sic_rules) or 'none'})"
            ),
            output_summary=(
                f"EDGAR x SIC join returned {len(state.edgar_sic_joined_rows)} filings; "
                f"{len(state.billing_rows)} billing cohorts; {len(state.support_rows)} support telemetry rows; "
                f"empty or missing evidence: {', '.join(empty) or 'none'}."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.92,
            retry_count=0,
            status="success",
            sponsor_tech="zetaris",
        )

        wm_ingest = self._ingest_accounts(state, source="zetaris_pass_1")
        self.trace.log(
            agent_name=self.name,
            action="ingest_world_model_pass_1",
            input_summary="Grounded EDGAR, billing and support rows as enterprise_account observations (6-stage pipeline)",
            output_summary=(
                f"Projected {wm_ingest['total_entities']} deterministic ent_ entities "
                f"({wm_ingest['diff_count']} new/changed projections from {wm_ingest['input_count']} observations)."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.95,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_world_model",
        )
        return state

    def execute_pass_2_hierarchy_recovery(self, state: InvestigationState) -> InvestigationState:
        """Hierarchy recovery: expand each dropped summary SIC through its leaf codes and re-run the join."""
        state.retriever_pass += 1
        pass_no = state.retriever_pass
        orphans = state.dq_orphans_dropped or state.dq_invalid_rows

        prefixes = sorted({str(o.get("invalid_sic_code", ""))[:3] for o in orphans if is_valid_sic_code(o.get("invalid_sic_code"))})
        if prefixes:
            where = " OR ".join(f"sic_code LIKE '{p}%'" for p in prefixes)
            hier = self._run(
                state,
                "sic_hierarchy",
                f"SELECT * FROM company_dns.sic_codes WHERE {where} ORDER BY sic_code LIMIT 100",
                ["H2"],
                pass_no,
            )
            state.sic_hierarchy_rows = hier["rows"]

        discovered = build_leaf_mapping(orphans, state.sic_hierarchy_rows, peer_rows=state.edgar_sic_joined_rows)
        for code, entry in discovered.items():
            # These orphans are still missing from the join, so any H-MEM rule covering them demonstrably failed:
            # fresh discovery replaces it, and the reviewer later scores the failed record as "wrong".
            prior = state.reconciliation_map.get(code) or {}
            replaced = prior.get("record_id") if prior.get("source") == "hmem" else None
            state.reconciliation_map[code] = {**entry, "source": "dq_discovery", **({"replaced_hmem_record": replaced} if replaced else {})}

        mapping = {code: entry["leaf_code"] for code, entry in state.reconciliation_map.items()}
        rec = self._run(state, "edgar_sic_join", edgar_sic_join_sql(mapping), ["H2"], pass_no)
        state.edgar_sic_joined_rows = rec["rows"]

        restored = [r for r in state.edgar_sic_joined_rows if str(r.get("edgar_sic_code")) in discovered]
        unmapped = sorted({str(o.get("invalid_sic_code")) for o in orphans} - set(state.reconciliation_map))
        self.trace.log(
            agent_name=self.name,
            action=f"recover_sic_hierarchy_pass_{pass_no}",
            input_summary=(
                f"Expanded summary SIC code(s) {sorted(discovered) or unmapped} through company_dns.sic_codes leaves "
                f"and re-ran the EDGAR x SIC join"
            ),
            output_summary=(
                "Mapped "
                + (", ".join(f"{c}->{e['leaf_code']} ({e['leaf_description']})" for c, e in discovered.items()) or "nothing")
                + f"; restored {len(restored)} filing(s) "
                + f"({', '.join(str(r.get('ticker')) for r in restored) or 'none'}); "
                + f"join now returns {len(state.edgar_sic_joined_rows)} filings"
                + (f"; unreconcilable: {', '.join(unmapped)}" if unmapped else "")
                + "."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.97 if discovered and not unmapped else 0.6,
            retry_count=state.plan_revision_count,
            status="success" if discovered else "partial_success",
            sponsor_tech="zetaris",
        )

        obs: List[Dict[str, Any]] = [
            {"entity_type": "sic_taxonomy_leaf", "name": f"SIC-{r['sic_code']}-{r.get('description', '')}", **r}
            for r in state.sic_hierarchy_rows
        ]
        for row in state.edgar_sic_joined_rows:
            obs.append({"entity_type": "enterprise_account", "name": row["company_name"], **row})
        wm_ingest = self.world_model.pipeline.ingest(obs, source=f"zetaris_pass_{pass_no}_reconciled", run_id=state.run_id)
        self.trace.log(
            agent_name=self.name,
            action=f"ingest_world_model_pass_{pass_no}",
            input_summary="Grounded reconciled filings and US SIC V4 leaf taxonomy in the World Model",
            output_summary=(
                f"World Model now holds {wm_ingest['total_entities']} entities "
                f"({wm_ingest['diff_count']} new/updated projections)."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.97,
            retry_count=state.plan_revision_count,
            status="success",
            sponsor_tech="meterless_world_model",
        )
        return state
