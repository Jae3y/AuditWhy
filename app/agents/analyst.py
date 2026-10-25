"""Agent 5: MarkovianAnalystAgent — bounded-context analysis stages, candidate claims, and post-critique revision."""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Tuple

from app.agents.planner import CONFOUNDER_PROBES
from app.analytics import fmt_usd, mean, pearson_ci95, pearson_r, resolve_focus
from app.engines.markovian import MarkovianEngine
from app.engines.world_model import WorldModelEngine
from app.llm import LLMRouter
from app.orchestration.state import InvestigationState
from app.tracing import TraceLogger

COLD_BASELINE_STAGES = ["cohort_scan", "taxonomy_reconciliation", "operational_correlation", "candidate_synthesis"]
CORRELATION_FEATURES = {
    "timeout_rate_vs_retention_loss_r": "batch_sync_timeout_rate_pct",
    "p99_latency_vs_retention_loss_r": "api_p99_latency_ms_q3",
    "p1_escalations_vs_retention_loss_r": "q3_p1_escalations",
    "mttr_hours_vs_retention_loss_r": "mttr_hours",
}


def _num(row: Dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


class MarkovianAnalystAgent:
    """Runs analysis as bounded Markovian chunks over World Model entities and revises claims after critique."""

    name = "MarkovianAnalystAgent"

    def __init__(
        self,
        markovian_engine: MarkovianEngine,
        world_model: WorldModelEngine,
        llm_router: LLMRouter,
        trace_logger: TraceLogger,
    ) -> None:
        self.markovian = markovian_engine
        self.world_model = world_model
        self.llm = llm_router
        self.trace = trace_logger

    # ------------------------------------------------------------------ data access
    def _accounts(self, state: InvestigationState) -> List[Dict[str, Any]]:
        """Account rows read back from the World Model, where EDGAR, ERP and support observations were merged."""
        ents = self.world_model.context.build_context("enterprise_account")["entities"]
        accounts = [dict(e["attributes"]) for e in ents if e["attributes"].get("cik")]
        for acc in accounts:
            edgar_code = str(acc.get("edgar_sic_code") or acc.get("sic_code_edgar") or acc.get("sic_code") or "")
            rule = state.reconciliation_map.get(edgar_code)
            acc["edgar_sic_code"] = edgar_code
            acc["reconciled_sic_leaf"] = rule["leaf_code"] if rule else str(acc.get("leaf_sic_code") or edgar_code)
            acc["dq_status"] = f"reconciled_from_summary_{edgar_code}" if rule else "valid_leaf"
            acc.setdefault("account_name", acc.get("company_name", ""))
        return sorted(accounts, key=lambda a: str(a["cik"]))

    # ------------------------------------------------------------------ analysis stages
    def _stage_cohort_scan(self, state: InvestigationState, accounts: List[Dict[str, Any]]) -> Tuple[str, str]:
        focus = state.focus
        cohort = [a for a in accounts if a["cik"] in focus["ciks"]]
        comparison = [a for a in accounts if a["cik"] not in focus["ciks"]]

        def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
            return {
                "accounts": len(rows),
                "q2_arr_usd": int(sum(_num(r, "q2_arr_usd") for r in rows)),
                "q3_arr_usd": int(sum(_num(r, "q3_arr_usd") for r in rows)),
                "arr_delta_usd": int(sum(_num(r, "q3_arr_delta_usd") for r in rows)),
                "avg_q3_net_retention_pct": mean(_num(r, "q3_net_retention_pct") for r in rows),
                "sla_credits_usd": int(sum(_num(r, "sla_credits_issued_usd") for r in rows)),
                "weather_sla_exposure_usd": int(sum(_num(r, "weather_sla_exposure_usd") for r in rows)),
                "contract_events": int(sum(_num(r, "contract_events_count") for r in rows)),
            }

        c, k = summarize(cohort), summarize(comparison)
        contracted = c["accounts"] > 0 and c["avg_q3_net_retention_pct"] < 100.0
        worse = not comparison or c["avg_q3_net_retention_pct"] < k["avg_q3_net_retention_pct"]
        outcome = "verified" if contracted and worse else ("refuted" if not contracted else "inconclusive")
        state.metrics["cohort"] = {**c, "label": focus["label"], "focus_mode": focus["mode"]}
        state.metrics["comparison"] = k
        state.metrics["h1_outcome"] = outcome
        state.metrics["per_account"] = [
            {
                "cik": a["cik"],
                "ticker": a.get("ticker", ""),
                "account_name": a.get("account_name", ""),
                "q3_net_retention_pct": _num(a, "q3_net_retention_pct"),
                "q3_arr_delta_usd": int(_num(a, "q3_arr_delta_usd")),
                "api_p99_latency_ms_q3": int(_num(a, "api_p99_latency_ms_q3")),
                "q3_p1_escalations": int(_num(a, "q3_p1_escalations")),
            }
            for a in cohort
        ]

        text = (
            f"Stage cohort_scan: focus '{focus['label']}' ({focus['mode']}) = {c['accounts']} accounts, "
            f"avg Q3 NRR {c['avg_q3_net_retention_pct']}% ({fmt_usd(c['arr_delta_usd'])} ARR delta, "
            f"{c['contract_events']} contract events) vs {k['avg_q3_net_retention_pct']}% across {k['accounts']} comparison accounts."
        )
        checkpoint = (
            f"COMPLETED: Cohort scan; H1 outcome={outcome}. "
            f"REMAINING: taxonomy integrity, operational correlation, synthesis. "
            f"DECISIONS: Focus cohort ciks={','.join(focus['ciks'])}. "
            f"CONTEXT: cohort NRR {c['avg_q3_net_retention_pct']}% vs {k['avg_q3_net_retention_pct']}%."
        )
        return text, checkpoint

    def _stage_taxonomy(self, state: InvestigationState, accounts: List[Dict[str, Any]]) -> Tuple[str, str]:
        by_cik = {a["cik"]: a for a in accounts}
        orphans = []
        for o in state.dq_invalid_rows:
            acc = by_cik.get(o.get("cik"), {})
            rule = state.reconciliation_map.get(str(o.get("invalid_sic_code")))
            orphans.append(
                {
                    "cik": o.get("cik"),
                    "ticker": o.get("ticker"),
                    "company_name": o.get("company_name"),
                    "edgar_sic_code": o.get("invalid_sic_code"),
                    "industry_group": o.get("industry_group"),
                    "reconciled_leaf_sic_code": rule["leaf_code"] if rule else None,
                    "reconciled_leaf_description": rule.get("leaf_description") if rule else None,
                    "reconciled_hierarchy": rule.get("hierarchy") if rule else None,
                    "resolution_source": rule.get("source") if rule else None,
                    "restored_fy2025_revenue_usd": int(o.get("revenue_2025") or 0),
                    "restored_q3_arr_usd": int(_num(acc, "q3_arr_usd")),
                    "restored_q3_arr_delta_usd": int(_num(acc, "q3_arr_delta_usd")),
                    "present_in_reconciled_join": any(
                        r.get("cik") == o.get("cik") and r.get("leaf_sic_code") for r in state.edgar_sic_joined_rows
                    ),
                }
            )
        reconciled = [o for o in orphans if o["reconciled_leaf_sic_code"] and o["present_in_reconciled_join"]]
        state.metrics["taxonomy"] = {
            "orphans": orphans,
            "reconciled_count": len(reconciled),
            "restored_revenue_usd": sum(o["restored_fy2025_revenue_usd"] for o in reconciled),
            "restored_q3_arr_usd": sum(o["restored_q3_arr_usd"] for o in reconciled),
            "leaf_codes_recovered": sorted({str(r["sic_code"]) for r in state.sic_hierarchy_rows})
            or sorted({c for rule in state.reconciliation_map.values() for c in rule.get("candidates", [rule["leaf_code"]])}),
        }
        if not orphans:
            state.metrics["h2_outcome"] = "verified"
        elif len(reconciled) == len(orphans):
            state.metrics["h2_outcome"] = "reconciled_after_dq_fix"
        else:
            state.metrics["h2_outcome"] = "inconclusive"

        listing = "; ".join(
            f"{o['ticker']} SIC {o['edgar_sic_code']}->{o['reconciled_leaf_sic_code']} "
            f"({fmt_usd(o['restored_fy2025_revenue_usd'])} revenue, {fmt_usd(o['restored_q3_arr_usd'])} Q3 ARR)"
            for o in orphans
        )
        text = f"Stage taxonomy_reconciliation: {len(orphans)} fk_sic_check orphan(s); {listing or 'none'}."
        checkpoint = (
            f"COMPLETED: Taxonomy check; H2 outcome={state.metrics['h2_outcome']}. "
            f"REMAINING: operational correlation, synthesis. "
            f"DECISIONS: Reconciled {len(reconciled)}/{len(orphans)} orphan(s). "
            f"CONTEXT: restored {fmt_usd(state.metrics['taxonomy']['restored_revenue_usd'])} FY2025 revenue."
        )
        return text, checkpoint

    def _stage_correlation(self, state: InvestigationState, accounts: List[Dict[str, Any]]) -> Tuple[str, str]:
        rows = [a for a in accounts if "api_p99_latency_ms_q3" in a and "q3_net_retention_pct" in a]
        loss = [100.0 - _num(r, "q3_net_retention_pct") for r in rows]
        correlations = {name: pearson_r([_num(r, col) for r in rows], loss) for name, col in CORRELATION_FEATURES.items()}
        primary_r = correlations["timeout_rate_vs_retention_loss_r"]
        ci = pearson_ci95(primary_r, len(rows))

        cohort = [a for a in rows if a["cik"] in state.focus["ciks"]]
        q2_p1 = int(sum(_num(a, "q2_p1_escalations") for a in cohort))
        q3_p1 = int(sum(_num(a, "q3_p1_escalations") for a in cohort))
        peak = max(cohort, key=lambda a: _num(a, "api_p99_latency_ms_q3"), default={})
        causes = Counter()
        for a in cohort:
            cause = str(a.get("dominant_root_cause") or "none")
            if cause != "none":
                causes[cause] += int(_num(a, "q3_p1_escalations"))
        baseline_p99 = sorted(_num(a, "api_p99_latency_ms_q2") for a in cohort) or [0.0]

        if len(rows) >= 5 and abs(primary_r) >= 0.7:
            outcome = "verified"
        elif len(rows) >= 5 and abs(primary_r) >= 0.4:
            outcome = "inconclusive"
        else:
            outcome = "refuted" if len(rows) >= 5 else "inconclusive"

        state.metrics["operational"] = {
            "n": len(rows),
            "correlations": {**correlations, "timeout_vs_retention_loss_r": primary_r, "latency_vs_retention_loss_r": correlations["p99_latency_vs_retention_loss_r"]},
            "primary_r": primary_r,
            "primary_r_ci95": ci,
            "cohort_q2_p1": q2_p1,
            "cohort_q3_p1": q3_p1,
            "p1_multiplier": round(q3_p1 / q2_p1, 2) if q2_p1 else None,
            "peak_p99_ms": int(_num(peak, "api_p99_latency_ms_q3")),
            "peak_p99_account": peak.get("account_name", ""),
            "baseline_q2_p99_median_ms": int(baseline_p99[len(baseline_p99) // 2]),
            "root_causes_by_q3_p1": dict(causes.most_common()),
            "worst_latency_accounts": [
                {"ticker": a.get("ticker", ""), "account_name": a.get("account_name", ""), "p99_ms": int(_num(a, "api_p99_latency_ms_q3")), "root_cause": a.get("dominant_root_cause")}
                for a in sorted(cohort, key=lambda a: -_num(a, "api_p99_latency_ms_q3"))[:5]
            ],
        }
        state.metrics["h3_outcome"] = outcome
        text = (
            f"Stage operational_correlation: across n={len(rows)} accounts, retention loss vs timeout rate r={primary_r} "
            f"(95% CI {ci['low']}..{ci['high']}), vs p99 latency r={correlations['p99_latency_vs_retention_loss_r']}, "
            f"vs P1 escalations r={correlations['p1_escalations_vs_retention_loss_r']}. Focus cohort P1 tickets {q2_p1}->{q3_p1}."
        )
        checkpoint = (
            f"COMPLETED: Correlation; H3 outcome={outcome}. REMAINING: candidate synthesis. "
            f"DECISIONS: Correlation is observational only. "
            f"CONTEXT: top cause {next(iter(causes), 'none')}; peak p99 {state.metrics['operational']['peak_p99_ms']}ms."
        )
        return text, checkpoint

    def _stage_synthesis(self, state: InvestigationState, accounts: List[Dict[str, Any]]) -> Tuple[str, str]:
        if "taxonomy" not in state.metrics:  # warm path skipped the stage; the numbers are still reported
            self._stage_taxonomy(state, accounts)
        cohort = [a for a in accounts if a["cik"] in state.focus["ciks"]]
        exposure = sorted(cohort, key=lambda a: -_num(a, "weather_sla_exposure_usd"))
        regions = Counter()
        for a in cohort:
            regions[str(a.get("primary_datacenter_region") or "unknown")] += int(_num(a, "weather_sla_exposure_usd"))
        state.metrics["forward_risk"] = {
            "cohort_weather_sla_exposure_usd": int(sum(_num(a, "weather_sla_exposure_usd") for a in cohort)),
            "top_accounts": [
                {"ticker": a.get("ticker", ""), "account_name": a.get("account_name", ""), "region": a.get("primary_datacenter_region"), "exposure_usd": int(_num(a, "weather_sla_exposure_usd"))}
                for a in exposure[:3]
            ],
            "by_region": dict(regions.most_common()),
        }

        cohort_m = state.metrics["cohort"]
        op = state.metrics["operational"]
        tax = state.metrics["taxonomy"]
        label = state.focus["label"]
        claims = [
            {
                "claim_id": "C_H1", "hypothesis_id": "H1", "requires": state.hypothesis_requirements.get("H1", []),
                "statement": f"{label} net retention contracted to {cohort_m['avg_q3_net_retention_pct']}% ({fmt_usd(cohort_m['arr_delta_usd'])} ARR).",
            },
            {
                "claim_id": "C_H2", "hypothesis_id": "H2", "requires": state.hypothesis_requirements.get("H2", []),
                "statement": f"{len(tax['orphans'])} summary-SIC filing(s) were dropped by the naive join and reconciled to leaf codes.",
            },
            {
                "claim_id": "C_H3", "hypothesis_id": "H3", "requires": state.hypothesis_requirements.get("H3", []),
                "statement": f"Operational degradation co-moves with retention loss (r={op['primary_r']}).",
                "statistic": "pearson_r", "value": op["primary_r"], "n": op["n"],
            },
            {
                # Drafted for every plan: pricing is a standard churn explanation, so the analyst proposes it and
                # leaves the evaluator to decide whether the data can support it.
                "claim_id": "C_H4", "hypothesis_id": "H4", "requires": state.hypothesis_requirements.get("H4", []),
                "statement": f"Q3 pricing-tier increases contributed to contraction in {label}.",
                "forbidden_terms": ["pricing", "price increase", "rate-card", "tier change"],
            },
            {
                "claim_id": "C_FORWARD_RISK", "hypothesis_id": None, "requires": ["billing_cohorts"],
                "statement": f"{label} carries {fmt_usd(state.metrics['forward_risk']['cohort_weather_sla_exposure_usd'])} Q4 weather SLA exposure.",
            },
        ]
        for probe in CONFOUNDER_PROBES:
            claims.append(
                {
                    "claim_id": f"C_{probe['factor_id']}", "hypothesis_id": None, "factor_id": probe["factor_id"],
                    "requires": [probe["purpose"]], "title": probe["title"],
                    "required_data_to_test": probe["required_data_to_test"],
                    "statement": f"{probe['title']} contributed to contraction in {label}.",
                    "forbidden_terms": ["competitor", "displacement", "bundling"],
                }
            )
        state.candidate_claims = claims
        text = f"Stage candidate_synthesis: drafted {len(claims)} candidate claims ({', '.join(c['claim_id'] for c in claims)}) for evaluator review."
        return text, ""

    # ------------------------------------------------------------------ orchestration entry points
    def execute_initial_analysis(self, state: InvestigationState) -> InvestigationState:
        accounts = self._accounts(state)
        state.focus = resolve_focus(state.user_query, accounts)

        # Warm path: H-MEM supplied every reconciliation, so the taxonomy-discovery stage has no work to do.
        resolved_by_memory = bool(state.reconciliation_map) and all(
            r.get("source") == "hmem" for r in state.reconciliation_map.values()
        )
        stages = [s for s in COLD_BASELINE_STAGES if not (s == "taxonomy_reconciliation" and (resolved_by_memory or not state.dq_invalid_rows))]
        stage_fns: Dict[str, Callable[[InvestigationState, List[Dict[str, Any]]], Tuple[str, str]]] = {
            "cohort_scan": self._stage_cohort_scan,
            "taxonomy_reconciliation": self._stage_taxonomy,
            "operational_correlation": self._stage_correlation,
            "candidate_synthesis": self._stage_synthesis,
        }

        def chunk_step(idx0: int, chunk_input: str, is_final: bool) -> str:
            text, checkpoint = stage_fns[stages[idx0]](state, accounts)
            out = self.llm.generate(
                task="analyze",
                system_prompt=(
                    "You are MarkovianAnalystAgent. Using only the computed facts, write 2 sentences. "
                    "Never introduce numbers that are not in the facts."
                ),
                user_prompt=f"{chunk_input}\n\nComputed facts: {text}",
                fallback_text=text,
            )["content"]
            if is_final:
                return f"{out}\n[TASK_COMPLETE]"
            return f"{out}\n[STATE_CHECKPOINT]\n{checkpoint}"

        telemetry = self.markovian.execute_reasoning_chain(
            prompt=state.user_query,
            memory_context=state.memory_context_str,
            chunk_Step_generator=chunk_step,
            warm_mode=state.warm_mode,
            run_id=state.run_id,
            planned_chunks=len(stages),
            baseline_chunks=len(COLD_BASELINE_STAGES),
            step_labels=stages,
        )
        state.markovian_telemetry = telemetry

        op = state.metrics["operational"]
        state.analyst_draft = {
            **state.analyst_draft,
            "focus": state.focus,
            "multivariate_correlations": op["correlations"],
            "enterprise_360_matrix": [
                {
                    "cik": a["cik"],
                    "ticker": a.get("ticker", ""),
                    "account_name": a.get("account_name", ""),
                    "in_focus": a["cik"] in state.focus["ciks"],
                    "sic_code_edgar": a["edgar_sic_code"],
                    "reconciled_sic_leaf": a["reconciled_sic_leaf"],
                    "dq_status": a["dq_status"],
                    "industry_segment": a.get("industry_segment", ""),
                    "revenue_2025_usd": int(_num(a, "revenue_2025")),
                    "q2_arr_usd": int(_num(a, "q2_arr_usd")),
                    "q3_arr_usd": int(_num(a, "q3_arr_usd")),
                    "q3_net_retention_pct": _num(a, "q3_net_retention_pct"),
                    "q3_arr_delta_usd": int(_num(a, "q3_arr_delta_usd")),
                    "q3_p1_escalations": int(_num(a, "q3_p1_escalations")),
                    "api_p99_latency_ms_q3": int(_num(a, "api_p99_latency_ms_q3")),
                    "batch_sync_timeout_rate_pct": _num(a, "batch_sync_timeout_rate_pct"),
                    "sla_credits_issued_usd": int(_num(a, "sla_credits_issued_usd")),
                    "weather_sla_exposure_usd": int(_num(a, "weather_sla_exposure_usd")),
                    "dominant_root_cause": a.get("dominant_root_cause", ""),
                    "churn_risk_tier": a.get("churn_risk_tier", ""),
                }
                for a in accounts
            ],
            "world_model_entities": self.world_model.context.build_context()["total_entities"],
            "summary": telemetry["final_output"],
        }

        self.trace.log(
            agent_name=self.name,
            action="run_markovian_bounded_analysis",
            input_summary=(
                f"Ran {telemetry['chunks_executed']} bounded chunk(s) [{', '.join(stages)}] over "
                f"{len(accounts)} World Model accounts (focus: {state.focus['label']}; memory seam={bool(state.memory_context_str)})"
            ),
            output_summary=(
                f"H1={state.metrics['h1_outcome']}, H2={state.metrics['h2_outcome']}, H3={state.metrics['h3_outcome']} "
                f"(timeout r={op['primary_r']}); drafted {len(state.candidate_claims)} candidate claims for review; "
                f"measured token saving {telemetry['efficiencyPercent']}% vs naive full-history chain."
            ),
            target_agent="EvidenceEvaluatorAgent",
            confidence=0.9,
            retry_count=0,
            status="success",
            sponsor_tech="meterless_markovian",
        )
        return state

    def revise_after_claim_block(self, state: InvestigationState) -> InvestigationState:
        """Apply the evaluator's verdicts: finalize hypotheses, keep supported findings, demote blocked claims."""
        verdicts = state.claim_verdicts
        m = state.metrics
        cohort, comp, op, tax, risk = m["cohort"], m["comparison"], m["operational"], m["taxonomy"], m["forward_risk"]

        def verdict(claim_id: str) -> str:
            return (verdicts.get(claim_id) or {}).get("verdict", "blocked")

        # ---- hypotheses
        h_conf = {
            "H1": round(min(0.97, 0.55 + 0.06 * cohort["accounts"]), 2),
            "H2": 0.99 if m["h2_outcome"] == "reconciled_after_dq_fix" else (0.95 if m["h2_outcome"] == "verified" else 0.6),
            # Conservative: the lower bound of the 95% CI on Pearson r.
            "H3": round(max(0.0, op["primary_r_ci95"]["low"]), 2),
        }
        orphan_text = "; ".join(
            f"{o['ticker']} (CIK {o['cik']}) reports SEC SIC {o['edgar_sic_code']}, a summary header absent from the 4-digit "
            f"leaf table; reconciled to {o['reconciled_leaf_sic_code']} ({o['reconciled_leaf_description']}), restoring "
            f"{fmt_usd(o['restored_fy2025_revenue_usd'])} FY2025 revenue and {fmt_usd(o['restored_q3_arr_usd'])} Q3 ARR"
            for o in tax["orphans"]
        )
        findings_text = {
            "H1": (
                f"{cohort['label']}: {cohort['accounts']} accounts averaged {cohort['avg_q3_net_retention_pct']}% Q3 net retention "
                f"({fmt_usd(cohort['arr_delta_usd'])} ARR delta, {fmt_usd(cohort['sla_credits_usd'])} SLA credits) vs "
                f"{comp['avg_q3_net_retention_pct']}% across {comp['accounts']} comparison accounts."
            ),
            "H2": orphan_text or "fk_sic_check found no orphans: every EDGAR SIC code joins to a 4-digit leaf.",
            "H3": (
                f"Across n={op['n']} accounts, retention loss correlates with batch-sync timeout rate (r={op['primary_r']}, "
                f"95% CI {op['primary_r_ci95']['low']}..{op['primary_r_ci95']['high']}), p99 latency "
                f"(r={op['correlations']['p99_latency_vs_retention_loss_r']}) and P1 escalations "
                f"(r={op['correlations']['p1_escalations_vs_retention_loss_r']}). Focus-cohort P1 tickets rose "
                f"{op['cohort_q2_p1']}->{op['cohort_q3_p1']}" + (f" ({op['p1_multiplier']}x)" if op["p1_multiplier"] else "") + "."
            ),
        }
        outcomes = {"H1": m["h1_outcome"], "H2": m["h2_outcome"], "H3": m["h3_outcome"]}
        for hyp in state.hypotheses:
            hid = hyp["hypothesis_id"]
            v = verdict(f"C_{hid}")
            if v == "blocked":
                hyp.update(
                    status="blocked_untested",
                    confidence=0.0,
                    evidence_tables=[],
                    finding_summary=f"BLOCKED AS UNTESTED: {verdicts.get(f'C_{hid}', {}).get('reason', 'no evidence')} "
                    "EvidenceEvaluatorAgent prohibited causal attribution.",
                )
                continue
            status = outcomes.get(hid, "inconclusive")
            if v == "inconclusive" and status == "verified":
                status = "inconclusive"
            conf = h_conf.get(hid, 0.5)
            if hid == "H3" and status == "refuted":
                conf = round(1 - abs(op["primary_r"]), 2)  # confidence that there is no meaningful co-movement
            hyp.update(status=status, confidence=conf, finding_summary=findings_text.get(hid, hyp["finding_summary"]))
            if hid == "H1":
                hyp["description"] = f"{hyp['description']} Focus cohort: {cohort['label']} ({cohort['focus_mode']})."

        # ---- verified findings (only claims that passed the gate)
        findings: List[Dict[str, Any]] = []
        if verdict("C_H3").startswith("supported") and m["h3_outcome"] == "verified":
            top_cause = next(iter(op["root_causes_by_q3_p1"]), "unknown")
            findings.append(
                {
                    "finding_id": "F1_OPERATIONAL_LATENCY_DRIVER",
                    "title": f"Operational degradation co-moves with retention loss (top cause: {top_cause})",
                    "confidence": h_conf["H3"],
                    "evidence_type": "correlational",
                    "evidence_tables": ["ops.support_tickets_usage", "erp.customer_churn_billing"],
                    "metrics": {
                        "cohort_q2_p1_tickets": op["cohort_q2_p1"],
                        "cohort_q3_p1_tickets": op["cohort_q3_p1"],
                        "p1_escalation_multiplier": f"{op['p1_multiplier']}x" if op["p1_multiplier"] else None,
                        "peak_p99_latency_ms": op["peak_p99_ms"],
                        "peak_p99_account": op["peak_p99_account"],
                        "correlation_with_retention_drop_r": op["primary_r"],
                        "correlation_ci95": op["primary_r_ci95"],
                        "multivariate_correlations": op["correlations"],
                        "root_causes_by_q3_p1_tickets": op["root_causes_by_q3_p1"],
                        "sla_credits_issued_usd": cohort["sla_credits_usd"],
                    },
                    "explanation": (
                        f"In {cohort['label']}, Q3 P1 escalations went {op['cohort_q2_p1']}->{op['cohort_q3_p1']}, led by "
                        f"{', '.join(op['root_causes_by_q3_p1']) or 'no dominant cause'}; p99 latency peaked at {op['peak_p99_ms']:,}ms "
                        f"({op['peak_p99_account']}). Across all {op['n']} accounts, timeout rate correlates with retention loss at "
                        f"r={op['primary_r']}. {fmt_usd(cohort['sla_credits_usd'])} in SLA credits were issued; cohort NRR "
                        f"{cohort['avg_q3_net_retention_pct']}% ({fmt_usd(cohort['arr_delta_usd'])} ARR)."
                    ),
                }
            )
        for o in tax["orphans"]:
            if verdict("C_H2") == "supported" and o["reconciled_leaf_sic_code"]:
                findings.append(
                    {
                        "finding_id": f"F2_TAXONOMIC_BLINDSPOT_{o['ticker']}_{o['edgar_sic_code']}",
                        "title": (
                            f"SEC EDGAR SIC {o['edgar_sic_code']} vs. US SIC V4 leaf orphan "
                            f"({fmt_usd(o['restored_fy2025_revenue_usd'])} revenue blindspot)"
                        ),
                        "confidence": h_conf["H2"],
                        "evidence_type": "structural",
                        "evidence_tables": [
                            "lightning.datasource.rest.sec_edgar",
                            "lightning.datasource.rest.company_dns",
                            "lightning.metastore.usl_demo.sic_edgar_usl",
                        ],
                        "metrics": {
                            "orphan_cik": o["cik"],
                            "orphan_company": o["company_name"],
                            "edgar_reported_sic": o["edgar_sic_code"],
                            "reconciled_leaf_sic": o["reconciled_leaf_sic_code"],
                            "resolution_source": o["resolution_source"],
                            "restored_fy2025_revenue_usd": o["restored_fy2025_revenue_usd"],
                            "restored_q3_arr_usd": o["restored_q3_arr_usd"],
                            "q3_arr_delta_usd": o["restored_q3_arr_delta_usd"],
                        },
                        "explanation": (
                            f"Zetaris DQ rule fk_sic_check shows {o['company_name']} (CIK {o['cik']}) files under SEC SIC "
                            f"{o['edgar_sic_code']}, which is not a 4-digit leaf in company_dns.sic_codes. A naive inner join "
                            f"silently drops its {fmt_usd(o['restored_fy2025_revenue_usd'])} FY2025 revenue and "
                            f"{fmt_usd(o['restored_q3_arr_delta_usd'])} Q3 ARR movement. Reconciled under {o['reconciled_hierarchy']}."
                        ),
                    }
                )
        if verdict("C_FORWARD_RISK") == "supported" and risk["cohort_weather_sla_exposure_usd"] > 0:
            leaders = ", ".join(f"{t['ticker'] or t['account_name']} {fmt_usd(t['exposure_usd'])} in {t['region']}" for t in risk["top_accounts"])
            findings.append(
                {
                    "finding_id": "F3_Q4_REGIONAL_WEATHER_SLA_EXPOSURE",
                    "title": f"Q4 regional weather SLA exposure ({fmt_usd(risk['cohort_weather_sla_exposure_usd'])} in {cohort['label']})",
                    "confidence": h_conf["H1"],
                    "evidence_type": "direct",
                    "evidence_tables": ["erp.customer_churn_billing"],
                    "metrics": {
                        "cohort_weather_sla_exposure_usd": risk["cohort_weather_sla_exposure_usd"],
                        "top_accounts": risk["top_accounts"],
                        "by_region": risk["by_region"],
                    },
                    "explanation": (
                        f"{cohort['label']} carries {fmt_usd(risk['cohort_weather_sla_exposure_usd'])} in Q4 weather SLA "
                        f"penalty exposure, led by {leaders}."
                    ),
                }
            )
        state.verified_findings = findings

        # ---- untested factors (every blocked claim, with what would be needed to test it)
        untested: List[Dict[str, Any]] = []
        for claim in state.candidate_claims:
            if verdict(claim["claim_id"]) != "blocked":
                continue
            hyp = next((h for h in state.hypotheses if h["hypothesis_id"] == claim.get("hypothesis_id")), None)
            plan_sql = next((p["sql"] for p in state.retrieval_plan if p["purpose"] in claim["requires"]), "")
            missing_tables = [
                t
                for p in claim["requires"]
                for t in ((state.datasets.get(p) or {}).get("missing_tables") or [])
            ] or [p for p in claim["requires"] if not state.dataset_has_evidence(p)]
            untested.append(
                {
                    "factor_id": claim.get("factor_id") or f"UF_{claim['hypothesis_id']}",
                    "hypothesis_id": claim.get("hypothesis_id") or "H_EXT",
                    "title": hyp["title"] if hyp else claim.get("title", claim["statement"]),
                    "reason_untested": f"Blocked by EvidenceEvaluatorAgent: `{plan_sql}` -> {verdicts[claim['claim_id']]['reason']}",
                    "required_data_to_test": claim.get("required_data_to_test")
                    or f"{', '.join(missing_tables)} with 2026-Q3 rows joined on account CIK / billing_cohort_id",
                    "forbidden_terms": claim.get("forbidden_terms", []),
                }
            )
        state.untested_factors = untested

        self.trace.log(
            agent_name=self.name,
            action="strip_unverified_claim_and_finalize_findings",
            input_summary=f"Applied EvidenceEvaluatorAgent verdicts to {len(state.candidate_claims)} candidate claims",
            output_summary=(
                f"Locked {len(findings)} provenance-backed finding(s); moved {len(untested)} blocked claim(s) "
                f"({', '.join(u['factor_id'] for u in untested) or 'none'}) to untested_factors. Hypotheses: "
                + ", ".join(f"{h['hypothesis_id']}={h['status']}" for h in state.hypotheses)
                + "."
            ),
            target_agent="ExplanationWriterAgent",
            confidence=0.95,
            retry_count=1 if untested else 0,
            status="success",
            sponsor_tech="meterless_markovian",
        )
        return state
