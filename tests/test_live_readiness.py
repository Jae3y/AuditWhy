"""Live-stack readiness: hackathon /run contract, public-demo guard, Nightshift provider contract v4, and the live
Zetaris shapes (official sic_edgar_usl DQ rows, canonical->live table names). Network calls are mocked."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import meterless_provider as mp
from app.adapters.zetaris import apply_table_map, load_live_table_map
from app.agents.evaluator import normalize_dq_rows
from app.config import settings
from app.reconciliation import build_leaf_mapping
from run import app

client = TestClient(app)


# ---------------------------------------------------------------- /run contract
def test_run_returns_the_hackathon_contract_fields() -> None:
    body = client.post("/run", json={"run_id": "contract-1", "query": "Why did Q3 retention drop in Computer & Office Equipment?"}).json()
    assert body["trace_id"] == "contract-1"
    assert body["log_file"] == "logs/trace-contract-1.jsonl" == body["log_path"]
    assert body["execution_time_seconds"] == pytest.approx(body["duration_ms"] / 1000, abs=0.01)
    assert {"summary", "recommendations", "artifacts"} <= set(body["output"])
    assert body["output"]["summary"] == body["executive_summary"]
    assert {a["name"] for a in body["agents"]} >= {"PlannerAgent", "EvidenceEvaluatorAgent", "FinalReviewerAgent"}
    assert all(a["role"] for a in body["agents"])
    assert body["collaboration"]["feedback_loop_completed"] is True
    assert body["collaboration"]["revision_count"] >= 1
    assert body["result"]["confidence"] == body["confidence"]
    assert body["error"] is None


def test_error_responses_carry_a_structured_error(monkeypatch) -> None:
    from app.agents.planner import PlannerAgent

    def boom(self, state, revision_reason=""):
        raise TimeoutError("planner timed out")

    monkeypatch.setattr(PlannerAgent, "execute", boom)
    body = client.post("/run", json={"query": "why did retention drop"}).json()
    assert body["status"] == "error"
    assert body["error"] == {"type": "TimeoutError", "message": "planner timed out", "recoverable": True}
    assert body["trace_events"][-1]["action"] == "recover_from_unhandled_error"


# ---------------------------------------------------------------- public demo
def test_public_demo_forces_sample_mode_and_blocks_credentials(monkeypatch) -> None:
    import app.config as config

    monkeypatch.setattr(config, "PUBLIC_DEMO", True)
    monkeypatch.setenv("SAMPLE_MODE", "false")
    assert settings.sample_mode is True
    body = client.post("/run", json={"query": "why did retention drop", "options": {"sample_mode": False}}).json()
    assert body["sample_mode"] is True
    assert client.post("/api/credentials", json={"sample_mode": False}).status_code == 403


# ---------------------------------------------------------------- Nightshift provider contract v4
def test_runtime_token_env_names(monkeypatch, tmp_path: Path) -> None:
    for name in ("METERLESS_RUNTIME_TOKEN", "METERLESS_API_KEY", "METERLESS_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_API_KEY", "legacy-name")
    assert settings.meterless_api_key == "legacy-name"
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "guide-name")
    assert settings.meterless_api_key == "guide-name"  # the setup-guide name wins
    monkeypatch.delenv("METERLESS_RUNTIME_TOKEN")
    monkeypatch.delenv("METERLESS_API_KEY")
    token_file = tmp_path / "proxy-token"
    token_file.write_text("from-state-dir\n", encoding="utf-8")
    monkeypatch.setenv("METERLESS_TOKEN_FILE", str(token_file))
    assert settings.meterless_api_key == "from-state-dir"


def _mock_runtime(monkeypatch, handler) -> None:
    real = httpx.Client
    monkeypatch.setattr(mp.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_online_requires_meterless_routing_ids(monkeypatch) -> None:
    _mock_runtime(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "llama3"}]}))
    c = mp.MeterlessProviderClient(base_url="http://127.0.0.1:8088", api_key="tok")
    assert c.probe_status()["status"] == "offline"  # a generic OpenAI server is not Nightshift


def test_model_resolution_respects_advertised_modes_and_pins(monkeypatch) -> None:
    monkeypatch.delenv("METERLESS_MODEL", raising=False)
    _mock_runtime(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "meterless/judge.deep"}]}))
    c = mp.MeterlessProviderClient(base_url="http://127.0.0.1:8088", api_key="tok")
    assert c.probe_status()["status"] == "online"
    assert c.resolve_model("evaluate") == "meterless/judge.deep"  # advertised -> used
    assert c.resolve_model("plan") == "meterless/auto"  # text.fast not advertised -> meterless/auto
    monkeypatch.setenv("METERLESS_MODEL", "meterless/text.nemotron")
    assert c.resolve_model("plan") == "meterless/text.nemotron"  # pinned lane wins, unknown ids kept


def test_chat_completion_reports_the_serving_model(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "nvidia/nemotron-3.5-lightning-30b-a3b", "choices": [{"message": {"content": "ready"}}]})

    monkeypatch.delenv("METERLESS_MODEL", raising=False)
    _mock_runtime(monkeypatch, handler)
    c = mp.MeterlessProviderClient(base_url="http://127.0.0.1:8088/v1/chat/completions", api_key="tok")
    res = c.chat_completion(messages=[{"role": "user", "content": "hi"}], task="synthesize")
    assert res["content"] == "ready" and res["served_by"].startswith("nvidia/nemotron")
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["headers"]["meterless-app"] == "auditwhy" and seen["headers"]["meterless-scope"] == "user"
    assert seen["body"]["model"] == "meterless/text.auto"


def test_chat_completion_uses_routed_model_and_strips_leaked_reasoning(monkeypatch) -> None:
    """Shape observed from Nightshift 1.2.7 on Windows: routing in `meterless`, reasoning leaked before </think>."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "meterless/auto",
            "meterless": {"requested_model": "meterless/fast", "routed_model": "nvidia/nemotron-3-ultra-550b-a55b"},
            "choices": [{"message": {"content": 'The user wants a one-word reply: "ready".</think>ready'}}],
        })

    monkeypatch.delenv("METERLESS_MODEL", raising=False)
    _mock_runtime(monkeypatch, handler)
    c = mp.MeterlessProviderClient(base_url="http://127.0.0.1:8088/v1", api_key="tok")
    res = c.chat_completion(messages=[{"role": "user", "content": "hi"}], task="plan", max_tokens=200)
    assert res["served_by"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert res["content"] == "ready"
    assert seen["body"]["max_tokens"] >= 500


def test_autodetect_includes_ipv6_loopback() -> None:
    assert "[::1]" in mp.AUTODETECT_HOSTS


# ---------------------------------------------------------------- live Zetaris shapes
def test_live_table_map_rewrites_only_canonical_names(monkeypatch) -> None:
    monkeypatch.delenv("ZETARIS_TABLE_MAP", raising=False)
    table_map = load_live_table_map()
    dq = apply_table_map("SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl", table_map)
    assert dq.endswith("sic_edgar_usl.company")
    assert apply_table_map(dq, table_map) == dq  # already-qualified names are left alone
    sql = apply_table_map("SELECT * FROM edgar.company_facts e JOIN company_dns.sic_codes s ON e.sic_code = s.sic_code", table_map)
    assert "auditwhy.edgar_company_facts e" in sql and "auditwhy.sic_codes s" in sql
    assert "erp.pricing_tier_changes" in apply_table_map("SELECT * FROM erp.pricing_tier_changes", table_map)
    monkeypatch.setenv("ZETARIS_TABLE_MAP", '{"edgar.company_facts": "team7.edgar"}')
    assert load_live_table_map() == {"edgar.company_facts": "team7.edgar"}


def test_official_usl_dq_rows_are_normalized_and_enriched() -> None:
    # Shape returned live by SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl.company
    live = [{"cik": "51143", "entity_name": "INTERNATIONAL BUSINESS MACHINES CORP", "sic": "3570", "sic_description_edgar": "COMPUTER & OFFICE EQUIPMENT"}]
    filings = [{"cik": "0000051143", "ticker": "IBM", "industry_group": "357", "revenue_2025": "62753000000"}]
    row = normalize_dq_rows(live, filings)[0]
    assert row["cik"] == "0000051143" and row["invalid_sic_code"] == "3570"
    assert row["ticker"] == "IBM" and row["company_name"].startswith("INTERNATIONAL")
    assert row["industry_group"] == "357" and row["revenue_2025"] == "62753000000"


def test_live_sic_table_without_hierarchy_column_still_reconciles() -> None:
    # The official sic_code table has no parent_summary_code and returns strings; prefix matching takes over.
    live_sic = [
        {"sic_code": "3572", "description": "Computer Storage Devices", "industry_group": "357", "division": "D"},
        {"sic_code": "3571", "description": "Electronic Computers", "industry_group": "357", "division": "D"},
        {"sic_code": "3661", "description": "Telephone Apparatus", "industry_group": "366", "division": "D"},
    ]
    mapping = build_leaf_mapping([{"cik": "0000051143", "invalid_sic_code": "3570", "industry_group": "357"}], live_sic)
    assert mapping["3570"]["leaf_code"] == "3571"
    assert mapping["3570"]["candidates"] == ["3571", "3572"]


def test_published_zetaris_files_match_the_sample_data() -> None:
    root = Path(__file__).resolve().parent.parent / "data"
    for name in ("edgar_company_facts.json", "company_dns_sic_codes.json", "erp_customer_churn_billing.json", "support_tickets_usage.json"):
        published = json.loads((root / "zetaris" / name).read_text(encoding="utf-8"))
        sample = json.loads((root / "sample" / name).read_text(encoding="utf-8"))
        assert published["data"] == sample and published["total"] == len(sample), f"run scripts/zetaris_onboard.py --emit ({name})"
