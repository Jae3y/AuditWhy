"""Agent 6: ExplanationWriterAgent — Synthesizes the executive root-cause narrative from verified findings only."""

from __future__ import annotations

from typing import Any, Dict, List

from app.analytics import fmt_usd
from app.llm import LLMRouter
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger


class ExplanationWriterAgent:
    """Drafts the executive report, then revises it against the FinalReviewerAgent's audit issues."""

    name = "ExplanationWriterAgent"

    def __init__(self, llm_router: LLMRouter, trace_logger: TraceLogger) -> None:
        self.llm = llm_router
        self.trace = trace_logger

    @staticmethod
    def _finding(state: InvestigationState, prefix: str) -> List[Dict[str, Any]]:
        return [f for f in state.verified_findings if f["finding_id"].startswith(prefix)]

    def _executive_summary(self, state: InvestigationState) -> str:
        m = state.metrics
        cohort, comp = m["cohort"], m["comparison"]
        h = {x["hypothesis_id"]: x for x in state.hypotheses}
        parts: List[str] = [f"Investigation for \"{state.user_query[:140]}\" (focus: {cohort['label']})."]

        verb = {"verified": "contracted", "refuted": "did not contract", "inconclusive": "moved"}.get(h["H1"]["status"], "moved")
        parts.append(
            f"Q3 net revenue retention in the focus cohort {verb}: {cohort['avg_q3_net_retention_pct']}% across "
            f"{cohort['accounts']} accounts ({fmt_usd(cohort['arr_delta_usd'])} ARR, {cohort['contract_events']} contract events) "
            f"vs {comp['avg_q3_net_retention_pct']}% across {comp['accounts']} comparison accounts."
        )
        if state.focus.get("mode") == "named_accounts":
            parts.append(
                "Per account: "
                + "; ".join(
                    f"{a['account_name']} {a['q3_net_retention_pct']}% NRR, {fmt_usd(a['q3_arr_delta_usd'])} ARR, "
                    f"p99 {a['api_p99_latency_ms_q3']:,}ms, {a['q3_p1_escalations']} Q3 P1s"
                    for a in m["per_account"]
                )
                + "."
            )

        operational: List[str] = []
        for f in self._finding(state, "F1_"):
            mt = f["metrics"]
            operational.append(
                f"Operational driver (correlational): P1 escalations {mt['cohort_q2_p1_tickets']}->{mt['cohort_q3_p1_tickets']}, "
                f"p99 latency up to {mt['peak_p99_latency_ms']:,}ms, timeout rate r={mt['correlation_with_retention_drop_r']} "
                f"with retention loss, {fmt_usd(mt['sla_credits_issued_usd'])} SLA credits"
            )
        taxonomic: List[str] = []
        for f in self._finding(state, "F2_"):
            mt = f["metrics"]
            how = (
                "the H-MEM reconciliation rule recalled at chunk 0 kept it in the Pass 1 join"
                if mt.get("resolution_source") == "hmem"
                else "fk_sic_check flagged it and Pass 2 recovered it"
            )
            taxonomic.append(
                f"Taxonomic data blindspot: {mt['orphan_company']} (CIK {mt['orphan_cik']}) files under summary SIC "
                f"{mt['edgar_reported_sic']}, which a naive EDGAR x SIC join drops ({fmt_usd(mt['restored_fy2025_revenue_usd'])} "
                f"FY2025 revenue); {how}, reconciled to leaf {mt['reconciled_leaf_sic']}"
            )
        forward = [f"Forward risk: {f['explanation']}".rstrip(".") for f in self._finding(state, "F3_")]
        # Lead with what the question asked about: forward-looking questions get the exposure first.
        q = state.user_query.lower()
        forward_first = any(k in q for k in ("q4", "exposure", "weather", "forward", "risk"))
        drivers = forward + operational + taxonomic if forward_first else operational + taxonomic + forward
        if drivers:
            parts.append(" ".join(f"({i}) {d}." for i, d in enumerate(drivers, 1)))
        else:
            parts.append("No driver met the evidence bar for a verified finding.")

        if state.untested_factors:
            parts.append(
                "Blocked as untested (no supporting rows in Zetaris): "
                + "; ".join(f"{u['hypothesis_id']} {u['title']}" for u in state.untested_factors)
                + "."
            )
        return " ".join(parts)

    def _recommended_actions(self, state: InvestigationState) -> List[Dict[str, Any]]:
        m = state.metrics
        actions: List[Dict[str, Any]] = []
        for f in self._finding(state, "F2_"):
            mt = f["metrics"]
            actions.append(
                {
                    "priority": "P0_IMMEDIATE",
                    "target_system": "Zetaris Unified Semantic Layer (sic_edgar_usl)",
                    "action": (
                        f"Deploy a hierarchy-aware USL join rule mapping summary SIC {mt['edgar_reported_sic']} to leaf "
                        f"{mt['reconciled_leaf_sic']} so {mt['orphan_company']} is never dropped from cohort reporting."
                    ),
                    "quantified_impact": (
                        f"Restores {fmt_usd(mt['restored_fy2025_revenue_usd'])} FY2025 SEC revenue and "
                        f"{fmt_usd(mt['restored_q3_arr_usd'])} Q3 ARR visibility."
                    ),
                }
            )
        for f in self._finding(state, "F1_"):
            op = m["operational"]
            worst = op["worst_latency_accounts"]
            causes = ", ".join(f"`{c}`" for c in op["root_causes_by_q3_p1"]) or "the dominant root causes"
            actions.append(
                {
                    "priority": "P0_IMMEDIATE",
                    "target_system": "API Gateway & Batch Connector Runtime",
                    "action": (
                        f"Remediate {causes} for the highest-latency accounts "
                        f"({', '.join(w['ticker'] or w['account_name'] for w in worst)}) and bring p99 latency back to the "
                        f"Q2 median of {op['baseline_q2_p99_median_ms']:,}ms (Q3 peak {op['peak_p99_ms']:,}ms)."
                    ),
                    "quantified_impact": (
                        f"Targets the {fmt_usd(m['cohort']['arr_delta_usd'])} Q3 ARR contraction trend and "
                        f"{fmt_usd(m['cohort']['sla_credits_usd'])}/quarter in SLA credit payouts."
                    ),
                }
            )
        for f in self._finding(state, "F3_"):
            mt = f["metrics"]
            regions = list(mt["by_region"].items())[:3]
            actions.append(
                {
                    "priority": "P1_PRE_Q4_FREEZE",
                    "target_system": f"Multi-Region Datacenter Failover ({', '.join(r for r, _ in regions)})",
                    "action": (
                        "Pre-stage active-active regional failover ahead of Q4 weather advisories for "
                        + ", ".join(f"{t['ticker'] or t['account_name']} ({t['region']})" for t in mt["top_accounts"])
                        + "."
                    ),
                    "quantified_impact": (
                        f"Protects {fmt_usd(mt['cohort_weather_sla_exposure_usd'])} in Q4 weather SLA exposure "
                        + "(" + ", ".join(f"{r} {fmt_usd(v)}" for r, v in regions) + ")."
                    ),
                }
            )
        for u in state.untested_factors:
            actions.append(
                {
                    "priority": "P2_EVIDENCE_GAP",
                    "target_system": "Zetaris Data Catalog",
                    "action": f"Mount {u['required_data_to_test'].rstrip('.')} so {u['hypothesis_id']} ({u['title']}) can be tested.",
                    "quantified_impact": "Converts an untested factor into a falsifiable hypothesis on the next run.",
                }
            )
        return actions

    def draft_explanation(self, state: InvestigationState) -> InvestigationState:
        summary = self._executive_summary(state)
        state.recommended_actions = self._recommended_actions(state)
        facts = "\n".join(f"- {f['title']}: {f['explanation']}" for f in state.verified_findings)
        llm_out = self.llm.generate(
            task="synthesize",
            system_prompt=(
                "You are ExplanationWriterAgent in AuditWhy. Write a 3-sentence executive narrative using ONLY these "
                "verified findings. Do not attribute causation to anything listed as untested."
            ),
            user_prompt=f"Question: {state.user_query}\nVerified findings:\n{facts}\nUntested: "
            + ", ".join(u["title"] for u in state.untested_factors),
            fallback_text=summary,
        )
        state.writer_draft = {
            "executive_summary": summary,
            "llm_narrative": llm_out["content"] if llm_out.get("is_live") else "",
            "has_explicit_caveat": False,
        }
        state.correlation_vs_causation_caveat = ""
        self.trace.log(
            agent_name=self.name,
            action="draft_executive_explanation",
            input_summary=(
                f"Synthesizing narrative from {len(state.verified_findings)} verified finding(s), "
                f"{len(state.untested_factors)} untested factor(s) and {len(state.evidence_ledger)} lineage entries"
            ),
            output_summary=(
                f"Drafted executive summary ({len(summary)} chars) and {len(state.recommended_actions)} data-derived "
                "actions; caveat section deferred to FinalReviewerAgent audit."
            ),
            target_agent="FinalReviewerAgent",
            confidence=0.9,
            retry_count=0,
            status="success",
            sponsor_tech="nvidia_nemotron",
        )
        return state

    def revise(self, state: InvestigationState, issues: List[Dict[str, Any]]) -> InvestigationState:
        """Address each reviewer issue: author the caveat it asked for and strip unsupported causal language."""
        summary = state.writer_draft.get("executive_summary", "")
        for issue in issues:
            if issue["type"] == "unsupported_causal_language":
                sentences = [s for s in summary.split(". ") if issue["sentence"].strip(". ") not in s]
                summary = ". ".join(sentences)
            if issue["type"] == "narrative_unsupported_causal_language":
                state.writer_draft["llm_narrative"] = ""

        caveat_issue = next((i for i in issues if i["type"] == "missing_caveat_coverage"), None)
        if caveat_issue or not state.correlation_vs_causation_caveat:
            correlational = [f for f in state.verified_findings if f.get("evidence_type") == "correlational"]
            lines = ["CORRELATION VS. CAUSATION & UNTESTED FACTORS CAVEAT:"]
            for f in correlational:
                mt = f["metrics"]
                ci = mt.get("correlation_ci95", {})
                lines.append(
                    f"The association between operational degradation and retention loss (r = {mt['correlation_with_retention_drop_r']}, "
                    f"95% CI {ci.get('low')}..{ci.get('high')}) is observational; telemetry alone cannot rule out confounders."
                )
            if state.untested_factors:
                lines.append(
                    "The following remain UNTESTED FACTORS because no supporting rows exist in the mounted Zetaris catalog: "
                    + "; ".join(f"{u['hypothesis_id']} {u['title']} (needs {u['required_data_to_test'].rstrip('.')})" for u in state.untested_factors)
                    + "."
                )
            if not correlational and not state.untested_factors:
                lines.append("All findings are structural or direct measurements; no correlational or untested claims were made.")
            state.correlation_vs_causation_caveat = " ".join(lines)

        state.writer_draft.update(executive_summary=summary, has_explicit_caveat=True)
        self.trace.log(
            agent_name=self.name,
            action="revise_explanation_with_caveat",
            input_summary=f"Addressing {len(issues)} FinalReviewerAgent issue(s): {', '.join(sorted({i['type'] for i in issues}))}",
            output_summary=(
                f"Added caveat covering {len(state.untested_factors)} untested factor(s) and "
                f"{sum(1 for f in state.verified_findings if f.get('evidence_type') == 'correlational')} correlational finding(s)."
            ),
            target_agent="FinalReviewerAgent",
            confidence=0.96,
            retry_count=1,
            status="success",
            sponsor_tech="nvidia_nemotron",
        )
        return state
