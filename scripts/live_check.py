"""Live-stack preflight: proves Zetaris and Meterless Nightshift (with Nemotron) are really connected.

Mirrors the Participant Setup Guide's three checks, plus the tables AuditWhy needs. Reads .env, never prints a
secret, and exits non-zero if a required check fails.

  python scripts/live_check.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.adapters.meterless_provider import MeterlessProviderClient, is_valid_meterless_token  # noqa: E402
from app.adapters.zetaris import apply_table_map, load_live_table_map, normalize_zetaris_rows, resolve_zetaris_token  # noqa: E402
from app.config import settings  # noqa: E402

results: list = []


def report(name: str, ok: bool, detail: str, required: bool = True) -> None:
    results.append((ok, required))
    tag = "PASS" if ok else ("FAIL" if required else "WARN")
    print(f"[{tag}] {name}: {detail}")


def zetaris_sql(sql: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {resolve_zetaris_token()}",
        "X-Org-ID": str(settings.zetaris_org_id),
        "X-Request-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": 5}
    with httpx.Client(timeout=settings.zetaris_timeout_seconds) as client:
        return client.post(f"{settings.zetaris_rest_url.rstrip('/')}/sql-editor/sqls/run", headers=headers, json=body)


def check_zetaris() -> None:
    print(f"\n== Zetaris ({settings.zetaris_rest_url}, org {settings.zetaris_org_id})")
    if not resolve_zetaris_token():
        report("API key", False, "No Zetaris credentials: set ZETARIS_API_KEY (UI > Management > API Tokens) or ZETARIS_USERNAME/ZETARIS_PASSWORD")
        return
    try:
        resp = zetaris_sql("SELECT 1 AS one")
    except httpx.HTTPError as exc:
        report("REST SELECT 1", False, f"{type(exc).__name__}: is the platform up? (docker compose ps --all; ./verify.sh)")
        return
    hints = {401: "key rejected; create a new API token", 400: "check ZETARIS_ORG_ID (numeric, locally 1)"}
    report("REST SELECT 1", resp.status_code < 300, f"HTTP {resp.status_code} {hints.get(resp.status_code, '')}".strip())
    if resp.status_code >= 300:
        return
    table_map = load_live_table_map()
    for canonical in ("edgar.company_facts", "company_dns.sic_codes", "erp.customer_churn_billing", "ops.support_tickets_usage"):
        live = apply_table_map(canonical, table_map)
        r = zetaris_sql(f"SELECT COUNT(*) AS n FROM {live}")
        rows = normalize_zetaris_rows(r.json()) if r.status_code < 300 else []
        report(f"table {live}", r.status_code < 300 and bool(rows),
               f"{rows[0].get('n', '?')} rows" if rows else f"HTTP {r.status_code}; run scripts/zetaris_onboard.py --apply")
    dq_target = apply_table_map("lightning.metastore.usl_demo.sic_edgar_usl", table_map)
    r = zetaris_sql(f"SHOW DQ ALL INVALID TABLE {dq_target}")
    rows = normalize_zetaris_rows(r.json()) if r.status_code < 300 else []
    report("official sic_edgar_usl DQ", r.status_code < 300,
           f"{len(rows)} invalid record(s): " + ", ".join(str(x.get('entity_name') or x.get('cik')) for x in rows)
           if r.status_code < 300 else f"HTTP {r.status_code}; build sic_edgar_usl from zetaris/getting-started")


def check_nightshift() -> None:
    client = MeterlessProviderClient()
    print(f"\n== Meterless Nightshift ({client.base_url}, app '{client.app_name}')")
    if not is_valid_meterless_token(client.api_key):
        found = MeterlessProviderClient.auto_detect_runtime(timeout=0.75)
        where = f"runtime answering at {found['selected_base_url']} ({found['status']})" if found["found"] else "no runtime on 8088/8090/8787/11434"
        report("runtime token", False, f"METERLESS_RUNTIME_TOKEN empty ({where}). Nightshift > Console (top right).")
        return
    probe = client.probe_status(timeout=5.0)
    hints = {"unauthorized": "token rejected; copy it again from the Nightshift Console", "offline": "is Nightshift running?"}
    report("GET /v1/models", probe["status"] == "online", f"{probe['status']} {hints.get(probe['status'], '')}".strip())
    if probe["status"] != "online":
        return
    meterless_ids = [m for m in probe["models"] if m.startswith("meterless/")]
    print(f"       routing modes advertised: {', '.join(meterless_ids[:12])}")
    model = client.resolve_model("synthesize")
    try:
        res = client.chat_completion(messages=[{"role": "user", "content": "Reply with the single word: ready"}], task="synthesize", max_tokens=16)
    except (httpx.HTTPError, PermissionError) as exc:
        report("chat completion", False, f"{type(exc).__name__} on {model}")
        return
    served = res.get("served_by") or "not reported"
    report("chat completion", bool(res["content"].strip()), f"{model} -> {res['content'].strip()[:40]!r} (served by {served})")
    nemotron = "nemotron" in served.lower()
    report("Nemotron lane", nemotron,
           "a Nemotron model served the request" if nemotron
           else "add NVIDIA in Nightshift > Providers and pin a Nemotron lane via METERLESS_MODEL (needed for the DGX Spark award)",
           required=False)


def check_direct_nvidia() -> None:
    print(f"\n== Direct Nemotron route, optional fallback ({settings.nvidia_base_url})")
    if not settings.nvidia_api_key:
        report("NVIDIA key", True, "not set; fine - the setup guide puts the NVIDIA key in Nightshift Providers instead", required=False)
        return
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{settings.nvidia_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.nvidia_api_key}"},
                json={"model": settings.nvidia_fast_model, "messages": [{"role": "user", "content": "Reply: ready"}], "max_tokens": 8},
            )
        hints = {401: "key rejected", 403: "key lacks access to this model", 404: "wrong base URL or model id"}
        report("chat completion", r.status_code < 300, f"{settings.nvidia_fast_model}: HTTP {r.status_code} {hints.get(r.status_code, '')}".strip(), required=False)
    except httpx.HTTPError as exc:
        report("chat completion", False, type(exc).__name__, required=False)


def main() -> None:
    print("AuditWhy live-stack preflight (secrets are never printed)")
    check_zetaris()
    check_nightshift()
    check_direct_nvidia()
    failed = [r for r in results if not r[0] and r[1]]
    print(f"\n{'READY: run with SAMPLE_MODE=false' if not failed else f'{len(failed)} required check(s) failing'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
