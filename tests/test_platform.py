"""Reconciliation policy, live Zetaris resilience, telemetry tailing and log-level behaviour."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import httpx
import pytest

from app.adapters import zetaris as zetaris_module
from app.adapters.zetaris import ZetarisClient, _is_transient, normalize_zetaris_rows
from app.engines.hmem import HMemEngine
from app.orchestration.graph import AuditWhyOrchestrator
from app.reconciliation import active_leaf_policy, choose_reconciled_leaf
from app.schemas import RunRequest
from app.tracing import TraceLogger

CHILDREN = [{"sic_code": c, "description": c} for c in ("3571", "3572", "3575", "3577")]


# ---------------------------------------------------------------- reconciliation policy
def test_primary_leaf_policy_ignores_peers() -> None:
    assert choose_reconciled_leaf("3570", CHILDREN, ["3572", "3572", "3577"], policy="primary_leaf")["sic_code"] == "3571"


def test_peer_majority_policy_follows_the_cohort_with_deterministic_ties() -> None:
    assert choose_reconciled_leaf("3570", CHILDREN, ["3572", "3572", "3577"], policy="peer_majority")["sic_code"] == "3572"
    assert choose_reconciled_leaf("3570", CHILDREN, ["3577", "3572"], policy="peer_majority")["sic_code"] == "3572"
    # Peers outside the candidate set, or no peers at all, fall back to the primary leaf
    assert choose_reconciled_leaf("3570", CHILDREN, ["7372"], policy="peer_majority")["sic_code"] == "3571"
    assert choose_reconciled_leaf("3570", [], ["3572"]) is None


def test_policy_env_override_and_audit_trail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUDITWHY_SIC_LEAF_POLICY", "nonsense")
    assert active_leaf_policy() == "primary_leaf"
    monkeypatch.setenv("AUDITWHY_SIC_LEAF_POLICY", "peer_majority")
    orch = AuditWhyOrchestrator(hmem_engine=HMemEngine(ledger_path=tmp_path / "l.jsonl"), persist_memory=False)
    body = orch.run(RunRequest(query="Why did Q3 retention drop in Computer & Office Equipment?")).model_dump()
    orphan = body["taxonomic_anomaly_resolution"]["orphans"][0]
    assert orphan["leaf_policy"] == "peer_majority"
    assert orphan["leaf_candidates"] == ["3571", "3572", "3575", "3577", "3578", "3579"]
    # Peers use 3571 (AAPL, HPQ) and 3572 (WDC, NTAP) twice each: the tie breaks to the lower code
    assert orphan["reconciled_leaf_sic_code"] == "3571"
    f2 = next(f for f in body["verified_findings"] if f["finding_id"].startswith("F2_"))
    assert "peer_majority" in f2["explanation"] and f2["metrics"]["leaf_candidates"]


# ---------------------------------------------------------------- live Zetaris resilience
@pytest.mark.parametrize(
    "payload",
    [
        {"rows": [{"cik": "1", "x": 2}]},
        {"data": [{"cik": "1", "x": 2}]},
        {"columns": ["cik", "x"], "data": [["1", 2]]},
        {"columns": [{"name": "cik"}, {"name": "x"}], "rows": [["1", 2]]},
        {"result": {"columns": ["cik", "x"], "rows": [["1", 2]]}},
        [{"cik": "1", "x": 2}],
    ],
)
def test_zetaris_payload_shapes_normalize_to_row_dicts(payload) -> None:
    assert normalize_zetaris_rows(payload) == [{"cik": "1", "x": 2}]


def test_only_transport_5xx_and_429_are_retried() -> None:
    req = httpx.Request("POST", "http://z")
    assert _is_transient(httpx.ConnectError("down", request=req))
    assert _is_transient(httpx.HTTPStatusError("x", request=req, response=httpx.Response(503, request=req)))
    assert _is_transient(httpx.HTTPStatusError("x", request=req, response=httpx.Response(429, request=req)))
    assert not _is_transient(httpx.HTTPStatusError("x", request=req, response=httpx.Response(403, request=req)))


def _live_client(monkeypatch, handler, tmp_path: Path) -> tuple[ZetarisClient, List[str]]:
    calls: List[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["sql"])
        return handler(request)

    real = httpx.Client
    monkeypatch.setattr(zetaris_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(recording), **kw))
    client = ZetarisClient(
        rest_url="http://zetaris.test:8888/api/v1.0",
        api_key="zk-test",
        sample_mode=False,
        trace_logger=TraceLogger("live-test", logs_dir=tmp_path),
    )
    return client, calls


def test_live_catalog_miss_is_reported_not_answered_from_sample(monkeypatch, tmp_path: Path) -> None:
    client, calls = _live_client(
        monkeypatch, lambda r: httpx.Response(400, text="AnalysisException: Table or view not found: erp.pricing_tier_changes"), tmp_path
    )
    res = client.run_sql("SELECT * FROM erp.pricing_tier_changes LIMIT 5")
    assert res["status"] == "TABLE_NOT_FOUND" and res["execution_engine"] == "ZetarisLightningREST"
    assert res["missing_tables"] == ["erp.pricing_tier_changes"]
    assert len(calls) == 1  # a 4xx is final: no retries
    assert client.live_unavailable_reason is None


def test_live_columnar_rows_flow_through(monkeypatch, tmp_path: Path) -> None:
    client, _ = _live_client(monkeypatch, lambda r: httpx.Response(200, json={"columns": ["cik"], "data": [["0000051143"]]}), tmp_path)
    res = client.run_sql("SELECT cik FROM edgar.company_facts")
    assert res["status"] == "SUCCESS" and res["rows"] == [{"cik": "0000051143"}]


def test_circuit_breaker_stops_hitting_a_failed_cluster(monkeypatch, tmp_path: Path) -> None:
    client, calls = _live_client(monkeypatch, lambda r: httpx.Response(403, text="forbidden"), tmp_path)
    first = client.run_sql("SELECT COUNT(*) AS c FROM erp.billing_events")
    second = client.run_sql("SELECT COUNT(*) AS c FROM erp.billing_events")
    assert client.live_unavailable_reason == "HTTPStatusError"
    assert len(calls) == 1  # the second query never touched the network
    assert first["execution_engine"] == second["execution_engine"] == "SQLiteRelationalWarehouse"
    assert first["rows"][0]["c"] == 412
    fallbacks = [e for e in client.trace_logger.events if e.action == "fallback_to_sample_data"]
    assert len(fallbacks) == 1


# ---------------------------------------------------------------- telemetry & logging
def test_telemetry_returns_only_the_tail_of_large_logs(monkeypatch, tmp_path: Path) -> None:
    import run

    log = tmp_path / "markovian_telemetry.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for i in range(5000):
            fh.write(json.dumps({"i": i}) + "\n")
        fh.write("not json\n")
    monkeypatch.setattr(run, "LOGS_DIR", tmp_path)
    body = run.get_engines_telemetry()
    assert [r["i"] for r in body["markovian_telemetry_tail"]] == [4996, 4997, 4998, 4999]


@pytest.mark.parametrize(
    "level, status, printed",
    [("INFO", "success", True), ("WARNING", "success", False), ("WARNING", "needs_revision", True), ("CRITICAL", "blocked", False)],
)
def test_log_level_controls_stdout_but_not_the_jsonl(monkeypatch, capsys, tmp_path: Path, level, status, printed) -> None:
    monkeypatch.setenv("LOG_LEVEL", level)
    logger = TraceLogger("lvl", logs_dir=tmp_path)
    logger.log(agent_name="A", action="x", input_summary="i", output_summary="o", target_agent="B", confidence=0.5, status=status)
    assert bool(capsys.readouterr().out.strip()) is printed
    assert logger.run_trace_path.read_text(encoding="utf-8").count("\n") == 1
