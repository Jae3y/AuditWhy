"""Proves the investigation is driven by the data it retrieves, not by a script.

Each test either perturbs the sample warehouse or changes the question, and checks that verdicts, numbers and memory
behaviour change accordingly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest

from app.agents.evaluator import EvidenceEvaluatorAgent
from app.agents.reviewer import SIC_RULE_TYPE, sic_rule_record_id
from app.config import SAMPLE_DATA_DIR
from app.engines.hmem import HMemEngine
from app.orchestration.graph import AuditWhyOrchestrator
from app.orchestration.state import InvestigationState
from app.reconciliation import build_leaf_mapping, edgar_sic_join_sql
from app.schemas import RunRequest
from app.tracing import TraceLogger

COLD_QUERY = "Why did Q3 retention drop in Computer & Office Equipment?"


def _dataset(tmp_path: Path, mutate: Optional[Callable[[Dict[str, Any]], None]] = None) -> Path:
    target = tmp_path / "sample"
    shutil.copytree(SAMPLE_DATA_DIR, target)
    if mutate:
        path = target / "edgar_company_facts.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            mutate(row)
        path.write_text(json.dumps(rows), encoding="utf-8")
    return target


def _run(tmp_path: Path, query: str, *, sample_dir: Optional[Path] = None, hmem: Optional[HMemEngine] = None, options=None):
    engine = hmem or HMemEngine(ledger_path=tmp_path / "ledger.jsonl")
    orch = AuditWhyOrchestrator(hmem_engine=engine, sample_dir=sample_dir, persist_memory=False)
    return orch.run(RunRequest(query=query, options=options or {})).model_dump()


def test_every_summary_code_orphan_is_discovered_and_reconciled(tmp_path: Path) -> None:
    def mutate(row: Dict[str, Any]) -> None:
        if row["ticker"] == "HPQ":
            row["sic_code"] = "3570"
        if row["ticker"] == "ORCL":
            row["sic_code"] = "7370"  # a summary code no code path has ever special-cased

    body = _run(tmp_path, COLD_QUERY, sample_dir=_dataset(tmp_path, mutate))
    tax = body["taxonomic_anomaly_resolution"]
    mapped = {(o["ticker"], o["edgar_sic_code"], o["reconciled_leaf_sic_code"]) for o in tax["orphans"]}
    assert mapped == {("IBM", "3570", "3571"), ("HPQ", "3570", "3571"), ("ORCL", "7370", "7372")}
    assert tax["status"] == "reconciled"
    expected_revenue = 62_753_000_000 + 53_559_000_000 + 55_840_000_000
    assert tax["restored_revenue_usd"] == expected_revenue
    assert {f["finding_id"] for f in body["verified_findings"]} >= {
        "F2_TAXONOMIC_BLINDSPOT_IBM_3570",
        "F2_TAXONOMIC_BLINDSPOT_HPQ_3570",
        "F2_TAXONOMIC_BLINDSPOT_ORCL_7370",
    }


def test_clean_taxonomy_triggers_no_revision(tmp_path: Path) -> None:
    def mutate(row: Dict[str, Any]) -> None:
        if row["ticker"] == "IBM":
            row["sic_code"] = "3571"

    body = _run(tmp_path, COLD_QUERY, sample_dir=_dataset(tmp_path, mutate))
    assert body["zetaris_lineage"]["dq_revision_triggered"] is False
    assert body["zetaris_lineage"]["retriever_passes_executed"] == 1
    assert body["taxonomic_anomaly_resolution"]["status"] == "clean"
    assert next(h for h in body["hypotheses"] if h["hypothesis_id"] == "H2")["status"] == "verified"
    assert "needs_revision" not in {e["status"] for e in body["trace_events"] if e["agent_name"] == "EvidenceEvaluatorAgent"}


def test_numbers_track_the_underlying_rows(tmp_path: Path) -> None:
    base = _run(tmp_path, COLD_QUERY)

    def mutate(row: Dict[str, Any]) -> None:
        if row["ticker"] == "IBM":
            row["revenue_2025"] = 70_000_000_000

    changed = _run(tmp_path, COLD_QUERY, sample_dir=_dataset(tmp_path, mutate))
    assert base["taxonomic_anomaly_resolution"]["orphan_entity"]["restored_fy2025_revenue_usd"] == 62_753_000_000
    assert changed["taxonomic_anomaly_resolution"]["orphan_entity"]["restored_fy2025_revenue_usd"] == 70_000_000_000
    assert "$70.000B" in changed["executive_summary"]


def test_question_focus_changes_the_verdicts(tmp_path: Path) -> None:
    body = _run(tmp_path, "Why did Oracle's support MTTR change?")
    assert body["why_explanation"]["focus"]["mode"] == "named_accounts"
    assert body["why_explanation"]["focus"]["ciks"] == ["0001341439"]
    h1 = next(h for h in body["hypotheses"] if h["hypothesis_id"] == "H1")
    assert h1["status"] == "refuted"  # Oracle grew (102% NRR); a retention-drop story would be false here
    assert "did not contract" in body["executive_summary"]


def test_compounding_happens_without_an_explicit_warm_flag(tmp_path: Path) -> None:
    hmem = HMemEngine(ledger_path=tmp_path / "ledger.jsonl")
    cold = _run(tmp_path, COLD_QUERY, hmem=hmem)
    assert cold["zetaris_lineage"]["retriever_passes_executed"] == 2
    rule = next(r for r in hmem.store.all_records() if r.id == sic_rule_record_id("3570"))
    conf_after_cold = rule.confidence

    follow_up = _run(tmp_path, "Which Prepackaged Software accounts are at risk next quarter?", hmem=hmem)
    assert follow_up["meterless_engines"]["hmem"]["memory_mode"] == "auto"
    assert follow_up["meterless_engines"]["hmem"]["warm_mode_activated"] is True
    assert follow_up["zetaris_lineage"]["retriever_passes_executed"] == 1
    assert rule.confidence > conf_after_cold  # recalled rule verified again -> "helpful" feedback


def test_cold_mode_recalls_but_never_applies_memory(tmp_path: Path) -> None:
    hmem = HMemEngine(ledger_path=tmp_path / "ledger.jsonl")
    _run(tmp_path, COLD_QUERY, hmem=hmem)
    again = _run(tmp_path, COLD_QUERY, hmem=hmem, options={"warm_memory_seed": False})
    assert again["meterless_engines"]["hmem"]["retrieved_count"] >= 1
    assert again["meterless_engines"]["hmem"]["warm_mode_activated"] is False
    assert again["zetaris_lineage"]["dq_revision_triggered"] is True


def test_a_wrong_recalled_rule_is_penalised_and_superseded(tmp_path: Path) -> None:
    hmem = HMemEngine(ledger_path=tmp_path / "ledger.jsonl")
    hmem.mining.mine_fact(
        content="Reconciliation Rule [SIC-3570-LEAF]: map IBM 3570 -> 3579 (wrong leaf).",
        source="zetaris_usl_dq:fk_sic_check",
        confidence=0.95,
        tags=["zetaris", "taxonomy", "sic"],
        entities=["IBM", "0000051143", "3570", "3579", "sic"],
        record_id=sic_rule_record_id("3570"),
        rule={"rule_type": SIC_RULE_TYPE, "summary_code": "3570", "leaf_code": "9999", "ciks": ["0000051143"]},
    )
    body = _run(tmp_path, COLD_QUERY, hmem=hmem)

    # The bad rule was applied, the evaluator caught IBM still missing, discovery replaced it
    assert body["meterless_engines"]["hmem"]["warm_mode_activated"] is True
    assert body["zetaris_lineage"]["dq_revision_triggered"] is True
    assert body["taxonomic_anomaly_resolution"]["orphan_entity"]["reconciled_leaf_sic_code"] == "3571"
    bad = next(r for r in hmem.store.all_records() if r.id == sic_rule_record_id("3570"))
    assert "needs_review" in bad.tags and bad.confidence < 0.85
    assert bad.supersededBy == f"{sic_rule_record_id('3570')}-r2"
    assert [r.id for r in hmem.store.active_rules(SIC_RULE_TYPE)] == [f"{sic_rule_record_id('3570')}-r2"]


def _evaluator_state(datasets: Dict[str, Any], claims) -> InvestigationState:
    state = InvestigationState(run_id="unit", request_id="unit", user_query="q")
    state.datasets = datasets
    state.candidate_claims = claims
    return state


@pytest.mark.parametrize(
    "dataset, statistic, expected",
    [
        ({"status": "SUCCESS", "row_count": 4}, None, "supported"),
        ({"status": "SUCCESS", "row_count": 0}, None, "blocked"),
        ({"status": "TABLE_NOT_FOUND", "row_count": 0}, None, "blocked"),
        ({"status": "SUCCESS", "row_count": 10}, {"value": 0.91, "n": 10}, "supported_correlational"),
        ({"status": "SUCCESS", "row_count": 10}, {"value": 0.55, "n": 10}, "inconclusive"),
        ({"status": "SUCCESS", "row_count": 3}, {"value": 0.99, "n": 3}, "inconclusive"),
    ],
)
def test_evaluator_gates_claims_on_provenance_then_statistics(tmp_path: Path, dataset, statistic, expected) -> None:
    claim = {"claim_id": "C1", "hypothesis_id": "HX", "statement": "x", "requires": ["d"]}
    if statistic:
        claim.update(statistic="pearson_r", **statistic)
    state = _evaluator_state({"d": dataset}, [claim])
    agent = EvidenceEvaluatorAgent(zetaris_client=None, llm_router=_NoLLM(), trace_logger=TraceLogger("unit", logs_dir=tmp_path))
    agent.evaluate_analyst_claims(state)
    assert state.claim_verdicts["C1"]["verdict"] == expected


class _NoLLM:
    def generate(self, *, fallback_text: str, **_: Any) -> Dict[str, Any]:
        return {"content": fallback_text, "is_live": False}


def test_hierarchy_join_sql_is_built_from_the_mapping_only() -> None:
    assert "LEFT JOIN" not in edgar_sic_join_sql({})
    sql = edgar_sic_join_sql({"3570": "3571", "7370": "7372"})
    assert "(e.sic_code = '3570' AND s.sic_code = '3571')" in sql and "(e.sic_code = '7370' AND s.sic_code = '7372')" in sql
    # Non-numeric codes can never reach the SQL string
    assert "DROP" not in edgar_sic_join_sql({"3570'; DROP TABLE x; --": "3571"})


def test_leaf_mapping_uses_parent_summary_code() -> None:
    sic = [
        {"sic_code": "3572", "description": "Storage", "parent_summary_code": "3570", "industry_group": "357"},
        {"sic_code": "3571", "description": "Computers", "parent_summary_code": "3570", "industry_group": "357"},
        {"sic_code": "7372", "description": "Software", "parent_summary_code": "7370", "industry_group": "737"},
    ]
    mapping = build_leaf_mapping([{"cik": "1", "invalid_sic_code": "3570", "industry_group": "357"}], sic)
    assert mapping["3570"]["leaf_code"] == "3571"
    assert mapping["3570"]["candidates"] == ["3571", "3572"]
