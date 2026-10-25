"""Regression tests for the security fixes: read-only SQL, id sanitisation, admin gating, PII, XSS, TLS."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.sample_data import SampleZetarisEngine
from app.config import LOGS_DIR, UI_DIR, safe_identifier
from app.engines.scout_intent import ScoutIntentEngine
from app.tracing import TraceLogger
from run import app

client = TestClient(app)


def test_warehouse_refuses_writes_at_engine_level(tmp_path: Path) -> None:
    engine = SampleZetarisEngine()
    target = (tmp_path / "pwned.db").as_posix()
    for sql in (
        f"ATTACH DATABASE '{target}' AS x",
        "UPDATE edgar_company_facts SET revenue_2025 = 0",
        "INSERT INTO company_dns_sic_codes (sic_code) VALUES ('3570')",
        "CREATE TABLE evil (x)",
        "PRAGMA writable_schema = 1",
    ):
        assert engine.execute_sql(sql)["status"] == "ERROR", sql
    assert not (tmp_path / "pwned.db").exists()
    assert engine.execute_sql("SELECT revenue_2025 FROM edgar_company_facts WHERE ticker = 'IBM'")["rows"][0]["revenue_2025"] == 62753000000


def test_workbench_is_read_only(tmp_path: Path) -> None:
    target = (tmp_path / "pwned2.db").as_posix()
    resp = client.post("/api/sql", json={"sql": f"ATTACH DATABASE '{target}' AS x"}).json()
    assert resp["status"] == "REJECTED_READ_ONLY"
    assert not (tmp_path / "pwned2.db").exists()
    assert client.post("/api/sql", json={"sql": "SHOW TABLES"}).json()["status"] == "SUCCESS"


def test_run_id_cannot_escape_logs_dir(tmp_path: Path) -> None:
    assert safe_identifier("../../etc/passwd") == "etc-passwd"
    assert safe_identifier("") == "run"
    logger = TraceLogger(run_id="../../../evil", logs_dir=tmp_path)
    assert logger.run_trace_path.resolve().parent == tmp_path.resolve()

    body = client.post("/run", json={"run_id": "../../TRAVERSAL", "query": "why did q3 retention drop"}).json()
    assert body["run_id"] == "TRAVERSAL"
    assert not (LOGS_DIR.parent / "TRAVERSAL.jsonl").exists()


def test_credentials_endpoint_requires_loopback_or_token(monkeypatch) -> None:
    remote = TestClient(app, client=("203.0.113.9", 50000))
    assert remote.post("/api/credentials", json={"zetaris_rest_url": "http://attacker.example"}).status_code == 403

    monkeypatch.setenv("AUDITWHY_ADMIN_TOKEN", "s3cret-token")
    ok = remote.post(
        "/api/credentials",
        json={"sample_mode": True},
        headers={"Authorization": "Bearer s3cret-token"},
    )
    assert ok.status_code == 200


def test_endpoint_move_without_new_key_drops_the_old_key(monkeypatch) -> None:
    monkeypatch.setenv("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0")
    monkeypatch.setenv("ZETARIS_API_KEY", "real-zetaris-key")
    resp = client.post("/api/credentials", json={"zetaris_rest_url": "http://other-host:8888/api/v1.0"}).json()
    import os

    assert os.environ["ZETARIS_API_KEY"] == ""
    assert any("ZETARIS_API_KEY cleared" in n for n in resp["notes"])


def test_credential_values_cannot_inject_env_lines() -> None:
    resp = client.post("/api/credentials", json={"nvidia_api_key": "nvapi-x\nSAMPLE_MODE=false"})
    assert resp.status_code == 400


def test_pii_redaction_uses_luhn_so_identifiers_survive(tmp_path: Path) -> None:
    scout = ScoutIntentEngine(contracts_log_path=tmp_path / "c.jsonl")
    kept = scout.evaluate("Why is CIK 0000051143 3570 missing from joins?")
    assert "0000051143" in kept["sanitized_query"]
    assert "credit_card" not in kept["risk"]["layers"]["layer4_pii_redaction"]["redacted_types"]
    card = scout.evaluate("Retention for customer card 4111 1111 1111 1111 dropped")
    assert "[REDACTED_CREDIT_CARD]" in card["sanitized_query"]


def test_scout_rate_limit_is_shared_across_engine_instances(tmp_path: Path) -> None:
    a = ScoutIntentEngine(contracts_log_path=tmp_path / "a.jsonl", rate_limit_per_minute=10**6)
    b = ScoutIntentEngine(contracts_log_path=tmp_path / "b.jsonl", rate_limit_per_minute=10**6)
    first = a.evaluate("why did retention drop")["risk"]["layers"]["layer5_rate_limit"]["requests_in_window"]
    second = b.evaluate("why did retention drop")["risk"]["layers"]["layer5_rate_limit"]["requests_in_window"]
    assert second == first + 1


def test_default_signing_key_is_not_a_public_constant(monkeypatch) -> None:
    monkeypatch.delenv("SCOUT_HMAC_KEY", raising=False)
    from app.config import settings

    assert settings.scout_hmac_key != "auditwhy-scout-contract-v1"
    assert len(settings.scout_hmac_key) == 64


def test_dashboard_escapes_server_strings() -> None:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert "function esc(" in html
    for raw in ("${ev.output_summary}", "${h.finding_summary}", "${r.account_name}"):
        assert raw not in html


def test_nvidia_thinking_flag_is_sent_as_top_level_field(monkeypatch) -> None:
    import httpx

    from app import llm as llm_module
    from app.adapters.meterless_provider import plan_route

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    real_client = httpx.Client
    monkeypatch.setattr(llm_module.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    router = llm_module.LLMRouter(sample_mode=False)
    out = router._call_nvidia_nim([{"role": "user", "content": "x"}], plan_route("evaluate"), 0.1, 16)
    assert out["content"] == "ok"
    assert seen["chat_template_kwargs"] == {"enable_thinking": True}
    assert "extra_body" not in seen
