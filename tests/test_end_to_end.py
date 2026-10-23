"""End-to-end API and multi-agent orchestration tests across all 3 canonical examples."""

from __future__ import annotations

import json
from pathlib import Path
from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from run import app

client = TestClient(app)


def test_health_endpoint() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "auditwhy"
    assert isinstance(data["sample_mode"], bool)
    assert "provider_status" in data
    assert "zetaris_status" in data


def test_example_1_cold_investigation_loop() -> None:
    payload = json.loads((ROOT_DIR / "input_examples" / "example_1.json").read_text(encoding="utf-8"))
    resp = client.post("/run", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "success"
    assert body["taxonomic_anomaly_resolution"]["orphan_entity"]["cik"] == "0000051143"
    assert body["taxonomic_anomaly_resolution"]["orphan_entity"]["edgar_sic_code"] == "3570"
    assert body["taxonomic_anomaly_resolution"]["orphan_entity"]["reconciled_leaf_sic_code"] == "3571"
    assert body["zetaris_lineage"]["dq_revision_triggered"] is True
    assert body["zetaris_lineage"]["retriever_passes_executed"] == 2

    # Verify H4 pricing hypothesis was blocked and moved to untested_factors
    h4 = next(h for h in body["hypotheses"] if h["hypothesis_id"] == "H4")
    assert h4["status"] == "blocked_untested"
    assert len(body["untested_factors"]) >= 1
    assert "CORRELATION VS. CAUSATION" in body["correlation_vs_causation_caveat"]

    # Verify 10-Account Enterprise 360 Matrix & Multivariate Correlations
    assert len(body["enterprise_360_matrix"]) == 10
    assert body["multivariate_correlations"]["timeout_vs_retention_loss_r"] > 0.85
    assert body["multivariate_correlations"]["latency_vs_retention_loss_r"] > 0.85

    # Verify 12..18 multi-agent trace events
    events = body["trace_events"]
    assert 12 <= len(events) <= 18
    statuses = {e["status"] for e in events}
    assert "needs_revision" in statuses
    assert "partial_success" in statuses


def test_example_2_warm_memory_compounding() -> None:
    payload = json.loads((ROOT_DIR / "input_examples" / "example_2.json").read_text(encoding="utf-8"))
    resp = client.post("/run", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "success"
    assert body["meterless_engines"]["hmem"]["warm_mode_activated"] is True
    assert body["meterless_engines"]["markovian"]["warm_mode"] is True
    assert body["meterless_engines"]["markovian"]["chunks_executed"] == 2
    assert body["meterless_engines"]["markovian"]["cold_vs_warm_chunk_delta"] == 2
    assert body["zetaris_lineage"]["retriever_passes_executed"] == 1


def test_example_3_adversarial_injection_blocked() -> None:
    payload = json.loads((ROOT_DIR / "input_examples" / "example_3.json").read_text(encoding="utf-8"))
    resp = client.post("/run", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "blocked"
    assert body["meterless_engines"]["scout_intent"]["blocked"] is True
    assert body["zetaris_lineage"]["queries_executed_count"] == 0
    assert body["meterless_engines"]["hmem"]["conflict_resolution_test"]["conflicts_found"] >= 1


def test_vibe_coding_payload_contract() -> None:
    vibe_payload = {
        "request_id": "vibe-req-99",
        "user_query": "Explain why Q3 retention dropped in Computer & Office Equipment and check SEC EDGAR SIC codes.",
        "context": {"track": "Track 02 — The Agent That Can Explain Why", "sample_mode": True},
    }
    resp = client.post("/run", json=vibe_payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "vibe-req-99"
    assert body["status"] == "success"


def test_arbitrary_judge_query_produces_dynamic_input_dependent_output() -> None:
    arb_payload = {
        "run_id": "judge-arbitrary-query-01",
        "input": {
            "query": "Compare IBM and Western Digital Q3 SLA latency against Microsoft and Dominion Energy."
        },
        "options": {"sample_mode": True},
    }
    resp = client.post("/run", json=arb_payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "INTERNATIONAL BUSINESS MACHINES CORP" in body["executive_summary"]
    assert "WESTERN DIGITAL CORP" in body["executive_summary"]
    assert "MICROSOFT CORP" in body["executive_summary"]
    assert "DOMINION ENERGY INC" in body["executive_summary"]


def test_sql_workbench_and_412_billing_events_warehouse() -> None:
    # 1. Verify all 412 granular billing event rows are queryable via real SQL
    r1 = client.post(
        "/api/sql",
        json={
            "sql": "SELECT COUNT(*) AS total_events, ROUND(SUM(arr_delta_usd), 2) AS total_delta FROM erp.billing_events"
        },
    )
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["status"] == "SUCCESS"
    assert d1["rows"][0]["total_events"] == 412

    # 2. Verify Scout Intent blocks DROP TABLE in the SQL workbench
    r2 = client.post(
        "/api/sql",
        json={"sql": "DROP TABLE company_dns.sic_codes"},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["status"] == "BLOCKED_BY_SCOUT_GUARD"


def test_engines_telemetry_and_credentials_endpoints() -> None:
    tel = client.get("/api/engines/telemetry")
    assert tel.status_code == 200
    tel_body = tel.json()
    assert tel_body["warehouse_schema"]["erp_billing_events"]["row_count"] == 412

    cred = client.get("/api/credentials/status")
    assert cred.status_code == 200
    assert "zetaris" in cred.json()
    assert "meterless_nightshift" in cred.json()
    assert "nvidia_nim" in cred.json()

    usl = client.post("/api/usl/setup")
    assert usl.status_code == 200
    usl_body = usl.json()
    assert usl_body["status"] == "USL_PIPELINE_COMPILED_AND_ACTIVATED"
    assert usl_body["statements_executed"] == 7

    view_q = client.post(
        "/api/sql",
        json={"sql": "SELECT cik, account_name, edgar_sic, reconciled_leaf_sic, dq_status FROM enterprise_360_reconciled_view ORDER BY cik"},
    )
    assert view_q.status_code == 200
    view_body = view_q.json()
    assert view_body["row_count"] == 10
    ibm_row = next(r for r in view_body["rows"] if r["cik"] == "0000051143")
    assert ibm_row["edgar_sic"] == "3570"
    assert ibm_row["reconciled_leaf_sic"] == "3571"



