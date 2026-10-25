"""Onboard AuditWhy's internal sample tables into a live Zetaris instance (REST tables + schemastore views).

Follows the zetaris/getting-started rules: dry-run first, one statement per call, 15 s between statements that call
an endpoint, verify row counts against the source. Credentials come from .env (ZETARIS_REST_URL, ZETARIS_ORG_ID,
ZETARIS_API_KEY) and are never printed.

Prerequisite for the DQ check: build the official sic_edgar_usl from a clone of zetaris/getting-started
(`python3 scripts/onboard.py run sic_edgar_usl --dry-run`, then without --dry-run). That USL's foreign-key rule is
what flags IBM's SIC 3570 live; this script adds the internal ERP/support data the agents join against it.

  python scripts/zetaris_onboard.py --emit          # regenerate data/zetaris/*.json from data/sample/
  python scripts/zetaris_onboard.py                 # dry run: print every statement in order
  python scripts/zetaris_onboard.py --apply         # execute against Zetaris, then verify counts
  python scripts/zetaris_onboard.py --verify        # only verify the auditwhy.* views
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import SAMPLE_DATA_DIR, settings  # noqa: E402

PUBLISH_DIR = ROOT / "data" / "zetaris"
DEFAULT_BASE_URL = "https://raw.githubusercontent.com/Jae3y/AuditWhy/main/data/zetaris"
LIGHTNING_DB = "AUDITWHY_REST"
CONTAINER = "auditwhy"

# sample file -> (raw REST table, schemastore view)
SOURCES: Dict[str, Tuple[str, str]] = {
    "edgar_company_facts.json": ("edgar_company_facts_raw", "edgar_company_facts"),
    "company_dns_sic_codes.json": ("sic_codes_raw", "sic_codes"),
    "erp_customer_churn_billing.json": ("customer_churn_billing_raw", "customer_churn_billing"),
    "support_tickets_usage.json": ("support_tickets_usage_raw", "support_tickets_usage"),
}


def emit() -> None:
    """Wrap each sample file as {"data": [...]}, the flat-array shape Zetaris flattens with explode(data)."""
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        rows = json.loads((SAMPLE_DATA_DIR / name).read_text(encoding="utf-8"))
        payload = {"code": 200, "total": len(rows), "data": rows}
        (PUBLISH_DIR / name).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        print(f"wrote data/zetaris/{name} ({len(rows)} rows)")


def statements(base_url: str) -> List[Tuple[str, bool]]:
    """Ordered (sql, calls_endpoint) pairs."""
    ua = os.getenv("ZETARIS_USER_AGENT", "").strip()  # never invent a contact; only send one if configured
    header = f'user-agent "{ua}"' if ua else ""
    out: List[Tuple[str, bool]] = [
        (f'CREATE LIGHTNING DATABASE {LIGHTNING_DB} DESCRIBE BY "AuditWhy internal sample sources - ERP billing, support telemetry, EDGAR facts, SIC leaves"', False)
    ]
    for name, (raw, _) in SOURCES.items():
        out.append(
            (
                f"CREATE LIGHTNING REST TABLE {raw} FROM {LIGHTNING_DB} REQUEST(\n"
                f'    endpoint "{base_url.rstrip("/")}/{name}",\n'
                '    method "get",\n'
                '    response_type "json",\n'
                '    http_encoding "URLENCODED"\n'
                f") HEADER ({header}) BODY ()",
                True,
            )
        )
        out.append((f"CACHE TABLE {LIGHTNING_DB.lower()}.{raw}", True))
    out.append((f"CREATE SCHEMASTORE CONTAINER {CONTAINER}", False))
    for name, (raw, view) in SOURCES.items():
        columns = list(json.loads((SAMPLE_DATA_DIR / name).read_text(encoding="utf-8"))[0].keys())
        select = ",\n    ".join(f"item.{c} AS {c}" for c in columns)
        out.append(
            (
                f"CREATE OR REPLACE SCHEMASTORE VIEW {view} WITH CONTAINER {CONTAINER} AS\n"
                f"SELECT\n    {select}\nFROM {LIGHTNING_DB.lower()}.{raw}\nLATERAL VIEW explode(data) AS item",
                False,
            )
        )
    return out


def _post(sql: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {settings.zetaris_api_key}",
        "X-Org-ID": str(settings.zetaris_org_id),
        "X-Request-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": 1000}
    with httpx.Client(timeout=max(60.0, settings.zetaris_timeout_seconds)) as client:
        return client.post(f"{settings.zetaris_rest_url.rstrip('/')}/sql-editor/sqls/run", headers=headers, json=body)


def apply(base_url: str, pace: float) -> bool:
    if not settings.zetaris_api_key:
        print("ZETARIS_API_KEY is not set in .env (Zetaris UI: Management > API Tokens > +).")
        return False
    ok = True
    for i, (sql, calls_endpoint) in enumerate(statements(base_url), 1):
        first_line = sql.splitlines()[0][:100]
        resp = _post(sql)
        text = resp.text[:300].replace("\n", " ")
        if resp.status_code < 300:
            print(f"[{i:02d}] OK      {first_line}")
        elif "already exist" in resp.text.lower():
            print(f"[{i:02d}] EXISTS  {first_line} (kept; verify it matches before relying on it)")
        else:
            print(f"[{i:02d}] FAILED  {first_line}\n       HTTP {resp.status_code}: {text}")
            ok = False
            break
        if calls_endpoint:
            time.sleep(pace)
    return ok


def verify() -> bool:
    if not settings.zetaris_api_key:
        print("ZETARIS_API_KEY is not set in .env.")
        return False
    all_ok = True
    for name, (_, view) in SOURCES.items():
        expected = len(json.loads((SAMPLE_DATA_DIR / name).read_text(encoding="utf-8")))
        resp = _post(f"SELECT COUNT(*) AS n FROM {CONTAINER}.{view}")
        try:
            data = resp.json()
            rows = data.get("data") or data.get("rows") or []
            first = rows[0] if rows else {}
            got = int(first.get("n") if isinstance(first, dict) else first[0])
        except (ValueError, IndexError, TypeError, KeyError, AttributeError):
            got = -1
        status = "PASS" if got == expected else "FAIL"
        all_ok &= got == expected
        print(f"{status}  {CONTAINER}.{view}: {got} rows (source has {expected})")
    dq = _post("SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl.company")
    print(("PASS" if dq.status_code < 300 else "FAIL") + "  official sic_edgar_usl DQ check reachable"
          + ("" if dq.status_code < 300 else f" (HTTP {dq.status_code}; build it from zetaris/getting-started first)"))
    return all_ok and dq.status_code < 300


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--emit", action="store_true", help="regenerate data/zetaris/*.json")
    parser.add_argument("--apply", action="store_true", help="execute the statements against Zetaris")
    parser.add_argument("--verify", action="store_true", help="only verify row counts and the DQ target")
    parser.add_argument("--base-url", default=os.getenv("AUDITWHY_DATA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--pace", type=float, default=15.0, help="seconds between endpoint-calling statements")
    args = parser.parse_args()

    if args.emit:
        emit()
        return
    if args.verify:
        sys.exit(0 if verify() else 1)
    if args.apply:
        sys.exit(0 if apply(args.base_url, args.pace) and verify() else 1)
    print(f"-- DRY RUN against {settings.zetaris_rest_url} (nothing executed). Re-run with --apply.\n")
    for sql, calls_endpoint in statements(args.base_url):
        print(sql + (";   -- calls endpoint, then wait 15 s" if calls_endpoint else ";") + "\n")


if __name__ == "__main__":
    main()
