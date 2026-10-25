"""Agent 7: FinalReviewerAgent — Epistemic quality gate and H-MEM institutional compounding."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.analytics import fmt_usd
from app.engines.hmem import HMemEngine, save_hmem_state
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger

SIC_RULE_TYPE = "sic_summary_to_leaf"
_UNTESTED_MARKERS = ("untested", "blocked", "not tested", "no supporting")


def sic_rule_record_id(summary_code: str) -> str:
    return f"mem-sic-{summary_code}-reconciliation-rule"


def _sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


class FinalReviewerAgent:
    """Audits the writer's draft against the evidence, then mines verified rules into H-MEM."""

    name = "FinalReviewerAgent"

    def __init__(self, hmem_engine: HMemEngine, trace_logger: TraceLogger, persist: bool = True) -> None:
        self.hmem = hmem_engine
        self.trace = trace_logger
        self.persist = persist

    def _issues(self, state: InvestigationState) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        summary = state.writer_draft.get("executive_summary", "")
        narrative = state.writer_draft.get("llm_narrative", "")

        # Gate 1: no causal language about anything the evaluator blocked.
        for factor in state.untested_factors:
            for term in factor.get("forbidden_terms", []):
                for sentence in _sentences(summary):
                    low = sentence.lower()
                    if term in low and not any(mark in low for mark in _UNTESTED_MARKERS):
                        issues.append({"type": "unsupported_causal_language", "factor_id": factor["factor_id"], "sentence": sentence})
                for sentence in _sentences(narrative):
                    low = sentence.lower()
                    if term in low and not any(mark in low for mark in _UNTESTED_MARKERS):
                        issues.append({"type": "narrative_unsupported_causal_language", "factor_id": factor["factor_id"], "sentence": sentence})

        # Gate 2: the caveat must name every untested factor and every correlational statistic.
        caveat = state.correlation_vs_causation_caveat
        required = [u["hypothesis_id"] for u in state.untested_factors]
        required += [
            str(f["metrics"]["correlation_with_retention_drop_r"])
            for f in state.verified_findings
            if f.get("evidence_type") == "correlational"
        ]
        missing = [token for token in required if token not in caveat]
        if not caveat.strip() or missing:
            issues.append({"type": "missing_caveat_coverage", "missing": missing or ["caveat section"]})

        # Gate 3: every finding must cite evidence tables.
        for f in state.verified_findings:
            if not f.get("evidence_tables"):
                issues.append({"type": "finding_without_provenance", "finding_id": f["finding_id"]})
        return issues

    def audit_draft(self, state: InvestigationState) -> bool:
        """Returns True if the draft passes all gates; otherwise records issues for the writer and returns False."""
        issues = self._issues(state)
        state.reviewer_issues = issues
        if issues:
            state.reviewer_revision_triggered = True
            self.trace.log(
                agent_name=self.name,
                action="enforce_epistemological_caveat",
                input_summary="Audited draft for causal discipline, caveat coverage and finding provenance",
                output_summary=(
                    f"{len(issues)} issue(s): "
                    + "; ".join(
                        f"{i['type']}" + (f" (missing {', '.join(i['missing'])})" if i.get("missing") else "")
                        for i in issues
                    )
                    + ". Returning needs_revision to ExplanationWriterAgent."
                ),
                target_agent="ExplanationWriterAgent",
                confidence=0.98,
                retry_count=1,
                status="needs_revision",
                sponsor_tech="nvidia_nemotron",
            )
            return False

        state.review_approved = True
        self.trace.log(
            agent_name=self.name,
            action="approve_final_explanation",
            input_summary="Re-audited revised draft against all three gates",
            output_summary=(
                f"Approved: {len(state.verified_findings)} findings cite Zetaris lineage, "
                f"{len(state.untested_factors)} untested factor(s) disclosed in the caveat, no unsupported causal language."
            ),
            target_agent="HMemMiningService",
            confidence=0.99,
            retry_count=1,
            status="success",
            sponsor_tech="nvidia_nemotron",
        )
        return True

    def compound_institutional_memory(self, state: InvestigationState) -> InvestigationState:
        """Mine each verified summary->leaf reconciliation into H-MEM and score recalled rules by outcome."""
        orphans = {o["edgar_sic_code"]: o for o in state.metrics.get("taxonomy", {}).get("orphans", [])}
        joined = {r.get("cik") for r in state.edgar_sic_joined_rows if r.get("leaf_sic_code")}
        mined_records: List[Dict[str, Any]] = []
        feedback: List[str] = []

        existing_ids = {r.id for r in self.hmem.store.all_records()}
        for code, entry in sorted(state.reconciliation_map.items()):
            record_id = entry.get("record_id") or sic_rule_record_id(code)
            replaced = entry.get("replaced_hmem_record")
            if replaced and replaced in existing_ids:
                # The recalled rule failed to restore its filer: penalise it, and store the correction under a new
                # id so ConflictDetectionService supersedes the bad record instead of silently overwriting it.
                self.hmem.store.apply_feedback(replaced, "wrong", run_id=state.run_id)
                feedback.append(f"{replaced}: wrong")
                revision = 2
                while f"{sic_rule_record_id(code)}-r{revision}" in existing_ids:
                    revision += 1
                record_id = f"{sic_rule_record_id(code)}-r{revision}"
            # A rule is verified only if every filer it covers is actually present in the reconciled join.
            covered = entry.get("ciks") or [o["cik"] for o in orphans.values() if o["edgar_sic_code"] == code]
            restored = bool(covered) and all(c in joined for c in covered)
            if not restored:
                if entry.get("source") == "hmem" and any(r.id == record_id for r in self.hmem.store.all_records()):
                    self.hmem.store.apply_feedback(record_id, "wrong", run_id=state.run_id)
                    feedback.append(f"{record_id}: wrong")
                continue
            o = orphans.get(code, {})
            ticker = o.get("ticker") or "UNKNOWN"
            cik = o.get("cik") or (entry.get("ciks") or [""])[0]
            revenue = int(o.get("restored_fy2025_revenue_usd") or entry.get("restored_revenue_usd") or 0)
            content = (
                f"Reconciliation Rule [SIC-{code}-LEAF]: SEC EDGAR CIK {cik} ({ticker}) reports summary SIC code {code}. "
                f"Map {code} -> {entry.get('hierarchy') or 'leaf ' + entry['leaf_code']} to prevent dropping "
                f"{fmt_usd(revenue)} FY2025 revenue in Zetaris USL joins."
            )
            mined = self.hmem.mining.mine_fact(
                content=content,
                source="zetaris_usl_dq:fk_sic_check",
                confidence=0.96,
                domain="taxonomy",
                layer="long_term",
                memory_type="procedural",
                tags=["zetaris", "taxonomy", "sic", f"sic_{code}", entry["leaf_code"], ticker.lower(), "enterprise"],
                entities=[ticker, cik, code, entry["leaf_code"], "sic"],
                agent_id=self.name,
                run_id=state.run_id,
                record_id=record_id,
                rule={
                    "rule_type": SIC_RULE_TYPE,
                    "summary_code": code,
                    "leaf_code": entry["leaf_code"],
                    "leaf_description": entry.get("leaf_description", ""),
                    "hierarchy": entry.get("hierarchy", ""),
                    "leaf_policy": entry.get("leaf_policy", "hmem_learned_rule"),
                    "candidates": entry.get("candidates", [entry["leaf_code"]]),
                    "ciks": entry.get("ciks") or [cik],
                    "restored_revenue_usd": revenue,
                    "verified_by": "fk_sic_check re-run after reconciliation",
                },
            )
            if entry.get("source") == "hmem":
                self.hmem.store.apply_feedback(record_id, "helpful", run_id=state.run_id)
                feedback.append(f"{record_id}: helpful")
            mined_records.append(mined)

        state.mined_memory = {
            "record": mined_records[0]["record"] if mined_records else {},
            "records": [m["record"] for m in mined_records],
            "feedback_applied": feedback,
        }
        state.sleep_preview = self.hmem.sleep.preview(run_id=state.run_id)
        state.dreams = self.hmem.dreaming.propose_dreams(run_id=state.run_id)
        if self.persist:
            save_hmem_state(self.hmem)

        self.trace.log(
            agent_name=self.name,
            action="mine_rule_into_hmem_and_sleep_preview",
            input_summary=f"Mining {len(state.reconciliation_map)} reconciliation(s) into H-MEM long_term and running SleepCycleService.preview()",
            output_summary=(
                f"Stored/reinforced {len(mined_records)} rule(s) "
                f"({', '.join(m['record']['id'] for m in mined_records) or 'none'}); feedback: {', '.join(feedback) or 'none'}; "
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
