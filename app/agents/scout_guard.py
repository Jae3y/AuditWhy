"""Agent 1: ScoutGuardAgent — 5-Layer Intent Guard, H-MEM Recall, and Signed ExecutionContract."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.agents.reviewer import SIC_RULE_TYPE, sic_rule_record_id
from app.engines.hmem import HMemEngine, MemoryRecord
from app.engines.scout_intent import ScoutIntentEngine
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger

# Prior-session knowledge used only when a caller explicitly asks for a warm start (warm_memory_seed=true) on an
# empty store, so example_2 is reproducible on a fresh clone. It is labelled as a fixture in provenance and trace.
SEED_SIC_RULE: Dict[str, Any] = {
    "summary_code": "3570",
    "leaf_code": "3571",
    "leaf_description": "Electronic Computers",
    "hierarchy": "Division D (Manufacturing) -> Major Group 35 -> Industry Group 357 -> Leaf 3571 (Electronic Computers)",
    "ciks": ["0000051143"],
    "ticker": "IBM",
    "restored_revenue_usd": 62753000000,
}

# Every investigation joins EDGAR to the SIC taxonomy (H2), so taxonomy rules are always in scope for recall.
PLAN_SCOPE_TAGS = ["zetaris", "taxonomy", "sic"]


def _query_entities(query: str) -> List[str]:
    """CIKs, SIC codes and ticker-like tokens mentioned in the query (used as H-MEM entity signals)."""
    found = re.findall(r"\b\d{10}\b|\b\d{4}\b", query)
    found += [t for t in re.findall(r"\b[A-Z]{2,5}\b", query) if t not in {"SEC", "SIC", "ARR", "SLA", "API", "ERP", "USL", "DQ"}]
    return list(dict.fromkeys(found))


class ScoutGuardAgent:
    """Screens incoming queries via Scout Intent, recalls H-MEM rules, and issues HMAC-SHA256 ExecutionContracts."""

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

    def _run_conflict_test(self, state: InvestigationState, cfg: Dict[str, Any]) -> None:
        """Safe post-block check: a low-trust contradicting claim must lose to the incumbent high-trust fact."""
        entity = str(cfg.get("entity") or SEED_SIC_RULE["ticker"])
        cik = str(cfg.get("cik") or SEED_SIC_RULE["ciks"][0])
        code = str(cfg.get("sic_code") or SEED_SIC_RULE["summary_code"])
        incumbent = next((r for r in self.hmem.store.active_rules(SIC_RULE_TYPE) if r.provenance["rule"].get("summary_code") == code), None)
        if incumbent is None:
            incumbent = self.hmem.store.create(
                MemoryRecord(
                    id=f"mem-conflict-verified-{code}",
                    content=f"{entity} SIC {code} is a summary header reconciled to a 4-digit leaf code by fk_sic_check.",
                    type="factual",
                    layer="long_term",
                    confidence=0.96,
                    source="zetaris_dq_fk_sic_check",
                    domain="taxonomy",
                    entities=[entity, cik, code],
                    runId=state.run_id,
                )
            )
        challenger = MemoryRecord(
            id=f"mem-conflict-unverified-{code}",
            content=f"{entity} SIC {code} is a valid 4-digit leaf code requiring no reconciliation (0% revenue impact).",
            type="factual",
            layer="short_term",
            confidence=0.42,
            source="unverified_external_note",
            domain="taxonomy",
            entities=[entity, cik, code],
            runId=state.run_id,
        )
        report = self.hmem.conflict.detect_and_resolve(challenger, run_id=state.run_id)
        self.hmem.store.create(challenger)
        state.conflict_resolution_report = {**report, "incumbent_id": incumbent.id, "challenger_id": challenger.id}
        self.trace.log(
            agent_name=self.name,
            action="resolve_hmem_entity_conflict",
            input_summary=f"Post-block H-MEM ConflictDetectionService check on {entity}/SIC {code}",
            output_summary=(
                f"Found {report['conflicts_found']} conflict(s); high-trust '{incumbent.id}' ({incumbent.confidence}) "
                f"retained over low-trust claim ({challenger.confidence}); challenger supersededBy={challenger.supersededBy}."
            ),
            target_agent="None",
            confidence=incumbent.confidence,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_hmem",
        )

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
            failed = {k: v for k, v in eval_res["risk"]["layers"].items() if not v.get("passed", True)}
            violations = eval_res["risk"]["layers"]["layer1_prompt_injection"]["violations"]
            state.block_reason = (
                f"Scout Intent 5-Layer Guard intercepted adversarial input "
                f"(failed layers={sorted(failed)}, violations={violations}). Tool execution contract denied."
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
            conflict_cfg = state.context.get("follow_up_conflict_test")
            if isinstance(conflict_cfg, dict) and conflict_cfg.get("enabled"):
                self._run_conflict_test(state, conflict_cfg)
            return state

        self.trace.log(
            agent_name=self.name,
            action="issue_signed_execution_contract",
            input_summary=f"Query passed 5-layer guard (score={eval_res['score']}): {eval_res['sanitized_query'][:120]}",
            output_summary=(
                f"Issued HMAC-SHA256 ExecutionContract id={state.execution_contract.contract_id} "
                f"signedBy=scout TTL=300s tools={state.execution_contract.allowed_tools}"
            ),
            target_agent="PlannerAgent",
            confidence=max(0.5, eval_res["score"]),
            retry_count=0,
            status="success",
            sponsor_tech="meterless_scout",
        )

        seeded = False
        if state.memory_mode == "warm" and not self.hmem.store.active_rules(SIC_RULE_TYPE):
            seed = SEED_SIC_RULE
            self.hmem.mining.mine_fact(
                content=(
                    f"Reconciliation Rule [SIC-{seed['summary_code']}-LEAF]: SEC EDGAR CIK {seed['ciks'][0]} ({seed['ticker']}) "
                    f"reports summary SIC code {seed['summary_code']}. Map {seed['summary_code']} -> {seed['hierarchy']} to prevent "
                    f"dropping $62.753B FY2025 revenue in Zetaris USL joins."
                ),
                source="zetaris_usl_dq:fk_sic_check",
                confidence=0.95,
                domain="taxonomy",
                layer="long_term",
                memory_type="procedural",
                tags=["zetaris", "taxonomy", "sic", f"sic_{seed['summary_code']}", seed["leaf_code"], seed["ticker"].lower(), "enterprise"],
                entities=[seed["ticker"], seed["ciks"][0], seed["summary_code"], seed["leaf_code"], "sic"],
                agent_id=self.name,
                run_id=state.run_id,
                record_id=sic_rule_record_id(seed["summary_code"]),
                rule={"rule_type": SIC_RULE_TYPE, **{k: v for k, v in seed.items() if k != "ticker"}, "seeded_from": "prior_session_fixture"},
            )
            seeded = True

        retrieved = self.hmem.retrieval.retrieve(
            state.user_query,
            query_tags=PLAN_SCOPE_TAGS,
            query_domain="taxonomy",
            query_entities=_query_entities(state.user_query),
            threshold=0.35,
            top_n=5,
            run_id=state.run_id,
        )
        state.retrieved_memories = retrieved

        rules = []
        for item in retrieved.get("memories", []):
            rec = item["record"]
            rule = (rec.get("provenance") or {}).get("rule") or {}
            if (
                rule.get("rule_type") == SIC_RULE_TYPE
                and rec["confidence"] >= 0.85
                and not rec.get("supersededBy")
                and "needs_review" not in rec.get("tags", [])
            ):
                rules.append((item, rule))

        if state.memory_mode != "cold" and rules:
            state.warm_mode = True
            for item, rule in rules:
                state.learned_sic_rules[rule["summary_code"]] = {
                    "leaf_code": rule["leaf_code"],
                    "leaf_description": rule.get("leaf_description", ""),
                    "hierarchy": rule.get("hierarchy", ""),
                    "ciks": list(rule.get("ciks") or []),
                    "record_id": item["record"]["id"],
                    "confidence": item["record"]["confidence"],
                    "candidates": list(rule.get("candidates") or [rule["leaf_code"]]),
                    "leaf_policy": rule.get("leaf_policy", "hmem_learned_rule"),
                }
            state.memory_context_str = "\n".join(
                f"[H-MEM {item['record']['id']} score={item['score']:.3f} conf={item['record']['confidence']}]: {item['record']['content']}"
                for item, _ in rules
            )

        self.trace.log(
            agent_name=self.name,
            action="retrieve_hmem_context",
            input_summary=f"8-factor H-MEM recall for '{state.user_query[:90]}' (memory_mode={state.memory_mode}, fixture_seeded={seeded})",
            output_summary=(
                f"Recalled {retrieved['total_matches']} record(s) above 0.35; {len(rules)} high-trust structured rule(s) "
                f"{'applied' if state.warm_mode else 'not applied'}"
                + (": " + ", ".join(f"{c}->{r['leaf_code']}" for c, r in state.learned_sic_rules.items()) if state.warm_mode else "")
                + (" (cold baseline requested)" if state.memory_mode == "cold" and rules else "")
                + "."
            ),
            target_agent="PlannerAgent",
            confidence=max((item["score"] for item, _ in rules), default=0.9),
            retry_count=0,
            status="success",
            sponsor_tech="meterless_hmem",
        )
        return state
