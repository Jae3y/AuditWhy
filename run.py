"""Programmatic FastAPI Server & CLI Entrypoint for AuditWhy.

Usage:
  1. Server Mode (default):
     python run.py
     -> Starts FastAPI + Uvicorn bound to 0.0.0.0 on int(os.getenv("PORT", "8000"))
  2. CLI Mode:
     python run.py --input input_examples/example_1.json --output output_examples/example_1.json
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app import __version__
from app.adapters.live_ingestor import fetch_live_ibm_edgar_facts, fetch_live_sic_v4_codes
from app.adapters.meterless_provider import MeterlessProviderClient, parse_connect_link
from app.adapters.zetaris import ZetarisClient, ZetarisSQLValidationError
from app.config import LOGS_DIR, ROOT_DIR, UI_DIR, mask_secret, settings
from app.engines.hmem import GLOBAL_HMEM
from app.engines.scout_intent import ScoutIntentEngine
from app.llm import LLMRouter
from app.orchestration.graph import AuditWhyOrchestrator
from app.schemas import RunRequest, RunResponse

MAX_QUERY_CHARS = 4000
MAX_SQL_LIMIT = 1000
_LOOPBACK_PEERS = {"127.0.0.1", "::1", "localhost", "testclient"}

app = FastAPI(
    title="AuditWhy — Autonomous Multi-Agent Discrepancy Investigator",
    version=__version__,
    description="Open Agent Hackathon 2026 — Track 02 (Primary), Track 01 & Track 03",
)

orchestrator = AuditWhyOrchestrator()


def require_admin(request: Request) -> None:
    """Gate state-changing admin endpoints: loopback peers, or `Authorization: Bearer $AUDITWHY_ADMIN_TOKEN`.

    The server binds 0.0.0.0 for container use, so without this anyone on the network could repoint a provider
    URL at their own host and receive the configured API key on the next call.
    """
    token = settings.admin_token
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token and supplied and hmac.compare_digest(supplied, token):
        return
    peer = request.client.host if request.client else ""
    if peer in _LOOPBACK_PEERS:
        return
    raise HTTPException(
        status_code=403,
        detail="Admin endpoint: call from localhost or send 'Authorization: Bearer <AUDITWHY_ADMIN_TOKEN>'.",
    )


@app.get("/health")
def health_check() -> Dict[str, Any]:
    """Programmatic health check endpoint required by hackathon evaluation harness."""
    llm_router = LLMRouter()
    zetaris_client = ZetarisClient()
    prov = llm_router.get_provider_status()
    return {
        "status": "ok",
        "service": "auditwhy",
        "version": __version__,
        "sample_mode": settings.sample_mode,
        "provider_status": prov["active_provider"],
        "zetaris_status": zetaris_client.check_status(),
    }


@app.post("/run", response_model=RunResponse)
def run_investigation(payload: Dict[str, Any]) -> RunResponse:
    """Execute the 6-agent investigation loop accepting both Judge and Vibe Coding payloads."""
    try:
        req = RunRequest(**payload)
    except (ValidationError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid request payload: {exc}") from exc

    query = req.effective_query()
    if not query:
        raise HTTPException(
            status_code=400,
            detail="Missing query: provide 'input.query', 'user_query', or 'query'.",
        )
    if len(query) > MAX_QUERY_CHARS:
        raise HTTPException(status_code=413, detail=f"Query exceeds {MAX_QUERY_CHARS} characters.")
    return orchestrator.run(req)


@app.get("/api/credentials/status")
def credentials_status() -> Dict[str, Any]:
    """Inspect masked credential configuration and live provider connectivity."""
    llm_router = LLMRouter(sample_mode=settings.sample_mode)
    z_client = ZetarisClient(sample_mode=settings.sample_mode)
    m_client = MeterlessProviderClient()
    m_probe = m_client.probe_status(timeout=1.5)
    return {
        "sample_mode": settings.sample_mode,
        "zetaris": {
            "rest_url": settings.zetaris_rest_url,
            "org_id": settings.zetaris_org_id,
            "api_key_configured": bool(settings.zetaris_api_key),
            "api_key_masked": mask_secret(settings.zetaris_api_key),
            "status": z_client.check_status(),
        },
        "meterless_nightshift": {
            "base_url": m_client.base_url,
            "app_name": m_client.app_name,
            "api_key_configured": bool(m_client.api_key),
            "api_key_masked": mask_secret(m_client.api_key),
            "probe": m_probe,
        },
        "nvidia_nim": {
            "base_url": settings.nvidia_base_url,
            "api_key_configured": bool(settings.nvidia_api_key),
            "api_key_masked": mask_secret(settings.nvidia_api_key),
            "models": {
                "fast": settings.nvidia_fast_model,
                "deep": settings.nvidia_deep_model,
                "synth": settings.nvidia_synth_model,
            },
        },
        "active_provider_routing": llm_router.get_provider_status(),
    }


def _clean_env_value(raw: Any) -> str:
    """Single-line value only: a newline in a submitted value must never inject extra .env assignments."""
    value = str(raw or "").strip()
    if any(ch in value for ch in "\r\n\0"):
        raise HTTPException(status_code=400, detail="Credential values must be single-line.")
    return value


def _set_endpoint(url_var: str, key_var: str, new_url: str, new_key: str, notes: list) -> None:
    """Update a provider endpoint; moving to a different host without a new key drops the old key."""
    if new_url:
        old_host = urlparse(os.getenv(url_var, "")).netloc
        if urlparse(new_url).netloc != old_host and not new_key and os.getenv(key_var):
            os.environ[key_var] = ""
            notes.append(f"{key_var} cleared: endpoint host changed and no new key was supplied.")
        os.environ[url_var] = new_url
    if new_key:
        os.environ[key_var] = new_key


@app.post("/api/credentials")
def update_credentials(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """Hot-reload credentials in process environment (and optionally persist to .env). Admin only."""
    require_admin(request)
    notes: list = []
    if "sample_mode" in payload and payload["sample_mode"] is not None:
        os.environ["SAMPLE_MODE"] = "true" if payload["sample_mode"] else "false"

    if payload.get("meterless_connect_link"):
        try:
            parsed = parse_connect_link(_clean_env_value(payload["meterless_connect_link"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        os.environ["METERLESS_BASE_URL"] = parsed["base_url"]
        os.environ["METERLESS_API_KEY"] = parsed["api_key"]
        os.environ["METERLESS_APP_NAME"] = parsed["app"]
    else:
        _set_endpoint(
            "METERLESS_BASE_URL",
            "METERLESS_API_KEY",
            _clean_env_value(payload.get("meterless_base_url")),
            _clean_env_value(payload.get("meterless_api_key")),
            notes,
        )

    if payload.get("nvidia_api_key"):
        os.environ["NVIDIA_API_KEY"] = _clean_env_value(payload["nvidia_api_key"])

    _set_endpoint(
        "ZETARIS_REST_URL",
        "ZETARIS_API_KEY",
        _clean_env_value(payload.get("zetaris_rest_url")),
        _clean_env_value(payload.get("zetaris_api_key")),
        notes,
    )

    if payload.get("persist_to_dotenv"):
        env_path = ROOT_DIR / ".env"
        lines = [
            f"PORT={settings.port}",
            f"SAMPLE_MODE={'true' if settings.sample_mode else 'false'}",
            f"ZETARIS_REST_URL={settings.zetaris_rest_url}",
            f"ZETARIS_API_KEY={settings.zetaris_api_key}",
            f"ZETARIS_ORG_ID={settings.zetaris_org_id}",
            f"METERLESS_BASE_URL={settings.meterless_base_url}",
            f"METERLESS_API_KEY={settings.meterless_api_key}",
            f"METERLESS_APP_NAME={settings.meterless_app_name}",
            f"NVIDIA_BASE_URL={settings.nvidia_base_url}",
            f"NVIDIA_API_KEY={settings.nvidia_api_key}",
        ]
        if os.getenv("SCOUT_HMAC_KEY"):
            lines.append(f"SCOUT_HMAC_KEY={os.getenv('SCOUT_HMAC_KEY')}")
        if settings.admin_token:
            lines.append(f"AUDITWHY_ADMIN_TOKEN={settings.admin_token}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {**credentials_status(), "notes": notes}


@app.post("/api/sql")
def execute_workbench_sql(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a read-only SQL statement through Scout Intent Guard + Zetaris / Relational SQLite Warehouse."""
    sql = str(payload.get("sql", "")).strip()
    try:
        limit = max(1, min(MAX_SQL_LIMIT, int(payload.get("limit", 100))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'limit' must be an integer.")
    if not sql:
        raise HTTPException(status_code=400, detail="SQL query cannot be empty.")

    scout = ScoutIntentEngine()
    eval_res = scout.evaluate(sql, run_id="sql-workbench")
    if eval_res["blocked"]:
        return {
            "status": "BLOCKED_BY_SCOUT_GUARD",
            "sql": sql,
            "risk": eval_res["risk"],
            "contract": eval_res["contract"].to_dict(),
            "rows": [],
            "row_count": 0,
        }

    z_client = ZetarisClient(scout_engine=scout)
    try:
        res = z_client.run_sql(sql, limit=limit, contract=eval_res["contract"], tool_name="query_zetaris", read_only=True)
    except ZetarisSQLValidationError as exc:
        return {"status": "REJECTED_READ_ONLY", "sql": sql, "error": str(exc), "rows": [], "row_count": 0}
    return {
        **res,
        "scout_contract_id": eval_res["contract"].contract_id,
        "scout_signature_prefix": eval_res["contract"].signature[:16],
    }


@app.get("/api/engines/telemetry")
def get_engines_telemetry() -> Dict[str, Any]:
    """Return live state of H-MEM, Markovian, World Model, Scout Contracts, and SQLite Warehouse Schema."""
    z_client = ZetarisClient()
    catalog = z_client.sample_engine.get_schema_catalog()

    def _tail_jsonl(filename: str, max_lines: int = 12) -> list:
        """Stream the file keeping only the last `max_lines`: these logs are append-only and grow without bound."""
        p = LOGS_DIR / filename
        if not p.exists():
            return []
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            tail = deque((ln for ln in fh if ln.strip()), maxlen=max_lines)
        out = []
        for ln in tail:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    return {
        "warehouse_schema": catalog,
        "hmem_records": [r.to_dict() for r in GLOBAL_HMEM.store.all_records()],
        "hmem_sleep_preview": GLOBAL_HMEM.sleep.preview(),
        "hmem_trust_ledger_tail": _tail_jsonl("hmem_trust_ledger.jsonl"),
        "markovian_telemetry_tail": _tail_jsonl("markovian_telemetry.jsonl", max_lines=5),
        "world_model_events_tail": _tail_jsonl("world_model_events.jsonl"),
        "scout_contracts_tail": _tail_jsonl("scout_contracts.jsonl"),
    }


@app.post("/api/live-ingest")
def trigger_live_public_ingest() -> Dict[str, Any]:
    """Probe live SEC EDGAR XBRL API (data.sec.gov) and US SIC V4 API (company-dns.mediumroast.io)."""
    sic_res = fetch_live_sic_v4_codes()
    edgar_res = fetch_live_ibm_edgar_facts()
    return {
        "company_dns_sic_v4": sic_res,
        "sec_edgar_ibm_0000051143": edgar_res,
    }


@app.post("/api/usl/setup")
def execute_usl_setup() -> Dict[str, Any]:
    """Execute the full scripts/zetaris_usl_setup.sql pipeline and return step-by-step results."""
    z_client = ZetarisClient()
    steps = z_client.sample_engine.execute_usl_setup_script()
    return {
        "status": "USL_PIPELINE_COMPILED_AND_ACTIVATED",
        "steps_executed": len(steps),
        "statements_executed": len(steps),
        "steps": steps,
    }


@app.get("/", response_class=HTMLResponse)
def serve_dashboard() -> HTMLResponse:
    """Serve the interactive single-page Trace, SQL Workbench & Lineage Inspector UI."""
    index_path = UI_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>AuditWhy API Active</h1>")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


def run_cli(input_path: str, output_path: str) -> RunResponse:
    in_file = Path(input_path)
    if not in_file.exists():
        raise FileNotFoundError(f"Input file not found: {in_file}")
    raw_payload = json.loads(in_file.read_text(encoding="utf-8"))
    req = RunRequest(**raw_payload)
    if not req.effective_query():
        raise ValueError(f"{in_file} has no query (expected 'input.query', 'user_query', or 'query').")
    result = orchestrator.run(req)

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[CLI] Wrote output artifact to {out_file} (status={result.status}, events={len(result.trace_events)})")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="AuditWhy Server & CLI Runner")
    parser.add_argument("--input", "-i", type=str, help="Path to input JSON payload")
    parser.add_argument("--output", "-o", type=str, help="Path to output JSON artifact")
    args = parser.parse_args()

    if args.input and args.output:
        run_cli(args.input, args.output)
        return

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=settings.port)


if __name__ == "__main__":
    main()
