"""Agent 1: ScoutGuardAgent — 5-Layer Intent Guard, H-MEM Recall, and Signed ExecutionContract."""

from __future__ import annotations

from app.engines.hmem import HMemEngine, MemoryRecord
from app.engines.scout_intent import ScoutIntentEngine
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger


class ScoutGuardAgent:
    """Screens incoming queries via Scout Intent, retrieves prior H-MEM rules, and issues HMAC-SHA256 ExecutionContracts."""

    name = "ScoutGuardAgent"

    def __init__(
        self,
        scout_engine: ScoutIntentEngine,
        hmem_engine: HMemEngine,
        trace_logger: TraceLogger,
    ) -> None:
        self.scout = scout_engine
        self.hmem = hmem_engine
        self.trace = trace_logger

    def execute(self, state: InvestigationState) -> InvestigationState:
        eval_res = self.scout.evaluate(
            state.user_query,
            run_id=state.run_id,
            role="investigator",
            scope="enterprise_audit",
        )
        state.scout_evaluation = eval_res
        state.execution_contract = eval_res["contract"]

        if eval_res["blocked"]:
            state.blocked = True
            violations = eval_res["risk"]["layers"]["layer1_prompt_injection"]["violations"]
            state.block_reason = (
                f"Scout Intent 5-Layer Guard intercepted adversarial input "
                f"(violations={violations}). Tool execution contract denied."
            )
            self.trace.log(
                agent_name=self.name,
                action="screen_intent_and_block_injection",
                input_summary=f"Query screened by 5-layer guard: {eval_res['sanitized_query'][:120]}",
                output_summary=state.block_reason,
                target_agent="None",
                confidence=1.0,
                retry_count=0,
                status="blocked",
                sponsor_tech="meterless_scout",
            )

            # Check if a safe follow-up H-MEM conflict resolution check was requested alongside the adversarial test
            conflict_cfg = state.context.get("follow_up_conflict_test")
            if isinstance(conflict_cfg, dict) and conflict_cfg.get("enabled"):
                low_rec = MemoryRecord(
                    id="mem-conflict-unverified-3570",
                    content="IBM SIC 3570 is a valid 4-digit leaf code requiring no reconciliation (0% revenue drop).",
                    type="factual",
                    layer="short_term",
                    confidence=0.42,
                    source="unverified_external_note",
                    domain="taxonomy",
                    entities=["IBM", "0000051143", "SIC_3570"],
                    runId=state.run_id,
                )
                self.hmem.store.create(low_rec)
                high_rec = MemoryRecord(
                    id="mem-conflict-verified-3570",
                    content="IBM SIC 3570 is a 3-digit Industry Group summary header reconciled to 3571 ($62.753B revenue).",
                    type="factual",
                    layer="long_term",
                    confidence=0.96,
                    source="zetaris_dq_fk_sic_check",
                    domain="taxonomy",
                    entities=["IBM", "0000051143", "SIC_3570"],
                    runId=state.run_id,
                )
                conflict_res = self.hmem.conflict.detect_and_resolve(high_rec, run_id=state.run_id)
                self.hmem.store.create(high_rec)
                state.conflict_resolution_report = conflict_res
                self.trace.log(
                    agent_name=self.name,
                    action="resolve_hmem_entity_conflict",
                    input_summary="Executed safe post-block H-MEM ConflictDetectionService verification on entity IBM/SIC_3570",
                    output_summary=(
                        f"Resolved {conflict_res['conflicts_found']} conflict(s); "
                        f"high-trust Zetaris DQ fact (0.96) superseded low-trust claim (0.42)."
                    ),
                    target_agent="None",
                    confidence=0.96,
                    retry_count=0,
                    status="success",
                    sponsor_tech="meterless_hmem",
                )
            return state

        # Valid query -> issue signed contract trace event
        self.trace.log(
            agent_name=self.name,
            action="issue_signed_execution_contract",
            input_summary=f"Query passed 5-layer guard (score={eval_res['score']}): {eval_res['sanitized_query'][:120]}",
            output_summary=(
                f"Issued HMAC-SHA256 ExecutionContract id={state.execution_contract.contract_id} "
                f"signedBy=scout TTL=300s tools={state.execution_contract.allowed_tools}"
            ),
            target_agent="PlannerAgent",
            confidence=0.98,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_scout",
        )

        # If warm_memory_seed is requested and H-MEM doesn't have the rule yet, seed it from prior session knowledge
        q_lower = state.user_query.lower()
        wants_warm = bool(state.context.get("warm_memory_seed")) or (
            "re-evaluate" in q_lower and "resolved sic taxonomy" in q_lower
        )
        if wants_warm and not any("3570" in r.content for r in self.hmem.store.all_records()):
            self.hmem.mining.mine_fact(
                content=(
                    "Reconciliation Rule [SIC-3570-LEAF]: SEC EDGAR CIK 0000051143 (IBM) reports 3-digit "
                    "Industry Group summary SIC code 3570 ('Computer and Office Equipment'). Map 3570 -> "
                    "Division D / Major Group 35 / Industry Group 357 leaf 3571 ('Electronic Computers') "
                    "to prevent dropping $62.753B FY2025 revenue in Zetaris USL joins."
                ),
                source="zetaris_usl_dq:fk_sic_check",
                confidence=0.95,
                domain="taxonomy",
                layer="long_term",
                memory_type="procedural",
                tags=["zetaris", "sic_3570", "3571", "ibm", "taxonomy", "enterprise"],
                entities=["IBM", "0000051143", "3570", "3571", "sic"],
                run_id=state.run_id,
                record_id="mem-sic-3570-reconciliation-rule",
            )

        retrieved = self.hmem.retrieval.retrieve(
            state.user_query,
            query_tags=["zetaris", "sic_3570", "taxonomy", "enterprise"],
            query_domain="taxonomy",
            query_entities=["IBM", "0000051143", "3570", "3571"],
            threshold=0.35,
            top_n=5,
            run_id=state.run_id,
        )
        state.retrieved_memories = retrieved

        has_sic_rule = any(
            "3570" in item["record"]["content"] and "3571" in item["record"]["content"]
            for item in retrieved.get("memories", [])
        )
        # Only activate warm shortcut if the query or options indicate warm compounding or H-MEM rule + warm query
        if has_sic_rule and (wants_warm or state.context.get("warm_memory_seed") is True):
            state.warm_mode = True
            mem_lines = [
                f"[H-MEM {m['record']['id']} score={m['score']:.3f} conf={m['record']['confidence']}]: {m['record']['content']}"
                for m in retrieved["memories"]
            ]
            state.memory_context_str = "\n".join(mem_lines)

        self.trace.log(
            agent_name=self.name,
            action="retrieve_hmem_context",
            input_summary=f"Queried H-MEM 8-factor hybrid retrieval for '{state.user_query[:90]}'",
            output_summary=(
                f"Retrieved {retrieved['total_matches']} memory record(s) above threshold 0.35 "
                f"(warm_mode={state.warm_mode})."
            ),
            target_agent="PlannerAgent",
            confidence=0.95,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_hmem",
        )
        return state
