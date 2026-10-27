"""Zetaris Networked Data Platform REST SQL Client with USL & DQ support and automatic Sample fallback."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.adapters.sample_data import SampleZetarisEngine, get_shared_sample_engine
from app.config import DATA_DIR, settings
from app.engines.scout_intent import ExecutionContract, ScoutIntentEngine
from app.tracing import TraceLogger

# Statement types an analyst may run interactively. Zetaris USL/DQ DDL is reserved for the setup pipeline.
READ_ONLY_STATEMENT = re.compile(r"^\s*(?:SELECT|WITH|SHOW|EXPLAIN|DESCRIBE)\b", re.IGNORECASE)


class ZetarisSQLValidationError(ValueError):
    """Raised when a query violates Zetaris Lightning SQL single-statement rules."""


_CATALOG_MISS = re.compile(
    r"(TABLE_OR_VIEW_NOT_FOUND|table or view not found|cannot be found|no such table|does not exist|cannot resolve|"
    r"not found in database)",
    re.I,
)

# Zetaris REST returns every cell as a string ({"headers": [...], "data": [["10"]]}). Codes and identifiers stay
# strings (CIK 0000051143, SIC 3570); everything else that parses as a number or boolean is coerced back.
_IDENTIFIER_COLUMN = re.compile(r"(cik|code|sic|_id$|^id$|group|division|fiscal_year|ticker|zone|region|name)", re.I)
_INT_TEXT = re.compile(r"^-?(0|[1-9]\d*)$")
_FLOAT_TEXT = re.compile(r"^-?\d+\.\d+(?:[eE][-+]?\d+)?$|^-?\d+[eE][-+]?\d+$")


def _coerce_cell(column: str, value: Any) -> Any:
    if not isinstance(value, str) or _IDENTIFIER_COLUMN.search(column or ""):
        return value
    v = value.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if _INT_TEXT.match(v):
        return int(v)
    if _FLOAT_TEXT.match(v):
        return float(v)
    return value


def _is_transient(exc: BaseException) -> bool:
    """Retry transport failures and 5xx/429; a 4xx is the server's final answer and retrying cannot change it."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


def normalize_zetaris_rows(data: Any) -> List[Dict[str, Any]]:
    """Coerce the result shapes Zetaris REST can return into a list of row dicts.

    Handles `{"rows": [{...}]}`, `{"data": [{...}]}`, columnar `{"columns"|"headers": [...], "data"|"rows": [[...]]}`
    (with columns as names or `{"name": ...}` objects), the same nested under `result`, and a bare list. The local
    2.4.3 bundle answers `{"headers": [...], "data": [[...]], "total": n}` with string cells, which are coerced.
    """
    if isinstance(data, dict) and isinstance(data.get("result"), (dict, list)):
        data = data["result"]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    rows = data.get("rows", data.get("data", data.get("records", [])))
    if not isinstance(rows, list):
        return []
    columns = data.get("columns") or data.get("headers") or data.get("schema") or []
    names = [c.get("name") or c.get("columnName") if isinstance(c, dict) else str(c) for c in columns]
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: _coerce_cell(k, v) for k, v in row.items()})
        elif isinstance(row, (list, tuple)) and names:
            out.append({k: _coerce_cell(k, v) for k, v in zip(names, row)})
    return out


def load_live_table_map() -> Dict[str, str]:
    """Canonical -> live Zetaris table names: ZETARIS_TABLE_MAP (inline JSON or path) over data/zetaris_table_map.json."""
    raw = os.getenv("ZETARIS_TABLE_MAP", "").strip()
    source: Any = None
    try:
        if raw.startswith("{"):
            source = json.loads(raw)
        elif raw:
            source = json.loads(Path(raw).expanduser().read_text(encoding="utf-8"))
        else:
            source = json.loads((DATA_DIR / "zetaris_table_map.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source = {}
    return {k: str(v) for k, v in (source or {}).items() if not k.startswith("_") and isinstance(v, str)}


def apply_table_map(sql: str, table_map: Dict[str, str]) -> str:
    """Rewrite canonical names to live names, longest first, never touching a name that is already more qualified."""
    out = sql
    for canonical in sorted(table_map, key=len, reverse=True):
        pattern = rf"(?<![\w.]){re.escape(canonical)}(?![\w.])"
        out = re.sub(pattern, table_map[canonical], out, flags=re.IGNORECASE)
    return out


def assert_read_only_sql(sql: str) -> None:
    """Reject anything other than a read statement (defence in depth on top of the warehouse authorizer)."""
    if not READ_ONLY_STATEMENT.match(sql or ""):
        raise ZetarisSQLValidationError(
            "Only read-only statements (SELECT, WITH, SHOW, EXPLAIN, DESCRIBE) are permitted here."
        )


def sanitize_zetaris_sql(sql: str) -> str:
    """Enforce Zetaris Lightning SQL rules:
    - Single statement per execution (unless inside a COMPILE USL ... block)
    - Strip trailing semicolons (unless inside a COMPILE USL ... block)
    """
    raw = (sql or "").strip()
    if not raw:
        raise ZetarisSQLValidationError("SQL query cannot be empty.")

    upper = raw.upper()
    is_compile_usl = upper.startswith("COMPILE USL")

    if not is_compile_usl:
        while raw.endswith(";"):
            raw = raw[:-1].strip()
        # Reject multiple statements separated by semicolons outside string literals
        stripped_strings = ""
        in_quote = False
        for ch in raw:
            if ch == "'":
                in_quote = not in_quote
            elif not in_quote:
                stripped_strings += ch
        if ";" in stripped_strings:
            raise ZetarisSQLValidationError(
                "Zetaris Lightning SQL requires a single statement per execution; found multiple ';' statements."
            )
    return raw


_LOGIN_CACHE: Dict[str, Any] = {}
_LOGIN_TTL_SECONDS = 45 * 60


def resolve_zetaris_token(*, force_refresh: bool = False) -> str:
    """Bearer token for the REST API: ZETARIS_API_KEY if set, else a session token from /api/auth/login.

    The login route is what zetaris/getting-started's own scripts use when no API key exists. The token is cached per
    process and refreshed before it ages out. Returns "" when neither credential is configured or login fails.
    """
    if settings.zetaris_api_key:
        return settings.zetaris_api_key
    user, password = settings.zetaris_username, settings.zetaris_password
    if not (user and password):
        return ""
    key = f"{settings.zetaris_base_url}|{user}"
    cached = _LOGIN_CACHE.get(key)
    if cached and not force_refresh and time.monotonic() - cached[1] < _LOGIN_TTL_SECONDS:
        return cached[0]
    try:
        with httpx.Client(timeout=min(30.0, settings.zetaris_timeout_seconds)) as client:
            r = client.post(f"{settings.zetaris_base_url}/api/auth/login",
                            json={"username": user, "password": password},
                            headers={"X-Request-ID": str(uuid.uuid4())})
        body = r.json() if r.status_code < 300 else {}
    except (httpx.HTTPError, ValueError):
        return ""
    token = ""
    if isinstance(body, dict):
        token = body.get("idToken") or body.get("token") or body.get("accessToken") or ""
    if token:
        _LOGIN_CACHE[key] = (token, time.monotonic())
    return token


class ZetarisClient:
    """Live Zetaris REST SQL Client (default port 8888, never 8889) with tenacity retry and SampleZetarisEngine fallback."""

    def __init__(
        self,
        *,
        rest_url: Optional[str] = None,
        api_key: Optional[str] = None,
        org_id: Optional[int] = None,
        sample_mode: Optional[bool] = None,
        scout_engine: Optional[ScoutIntentEngine] = None,
        trace_logger: Optional[TraceLogger] = None,
        sample_dir: Optional[Path] = None,
        sample_engine: Optional[SampleZetarisEngine] = None,
    ) -> None:
        base = (rest_url or settings.zetaris_rest_url).strip()
        if ":8889" in base:
            base = base.replace(":8889", ":8888")
        self.rest_url = base.rstrip("/")
        self.sample_mode = sample_mode if sample_mode is not None else settings.sample_mode
        if api_key is not None:
            self.api_key = api_key.strip()
        else:
            # Only sign in when the run will actually go live; sample mode never touches the network.
            self.api_key = "" if self.sample_mode else resolve_zetaris_token()
        self.org_id = org_id if org_id is not None else settings.zetaris_org_id
        self.scout_engine = scout_engine
        self.trace_logger = trace_logger
        self.sample_engine = sample_engine or get_shared_sample_engine(sample_dir)
        self.executed_queries: List[Dict[str, Any]] = []
        # Circuit breaker: once live Zetaris is unreachable, the rest of the run goes straight to the mirror
        # instead of paying 3 retries with backoff on every remaining query.
        self.live_unavailable_reason: Optional[str] = None
        self.table_map = load_live_table_map()
        self.timeout = settings.zetaris_timeout_seconds

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
    def _post_live_sql(self, cleaned_sql: str, limit: int) -> Dict[str, Any]:
        endpoint = f"{self.rest_url}/sql-editor/sqls/run"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Org-ID": str(self.org_id),
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        payload = {
            "queryId": str(uuid.uuid4()),
            "sql": cleaned_sql,
            "source": "SqlEditor",
            "limit": int(limit),
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if 400 <= resp.status_code < 500 and _CATALOG_MISS.search(resp.text):
                missing = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", cleaned_sql, re.I)
                return {
                    "status": "TABLE_NOT_FOUND",
                    "execution_engine": "ZetarisLightningREST",
                    "sql": cleaned_sql,
                    "missing_tables": missing,
                    "columns": [],
                    "row_count": 0,
                    "rows": [],
                    "note": resp.text[:300],
                }
            resp.raise_for_status()
            data = resp.json()
            rows = normalize_zetaris_rows(data)
            return {
                "status": "SUCCESS",
                "execution_engine": "ZetarisLightningREST",
                "sql": cleaned_sql,
                "columns": list(rows[0].keys()) if rows else [],
                "row_count": len(rows),
                "rows": rows,
            }

    def run_sql(
        self,
        sql: str,
        limit: int = 100,
        *,
        contract: Optional[ExecutionContract | Dict[str, Any]] = None,
        tool_name: str = "query_zetaris",
        agent_name: str = "DataRetrieverAgent",
        read_only: bool = False,
    ) -> Dict[str, Any]:
        """Validate contract (if scout_engine configured), sanitize SQL, and execute against Zetaris or SampleZetarisEngine."""
        if self.scout_engine is not None:
            self.scout_engine.verify_contract_for_tool(contract, tool_name)

        cleaned_sql = sanitize_zetaris_sql(sql)
        if read_only:
            assert_read_only_sql(cleaned_sql)

        if not self.sample_mode and self.api_key and self.live_unavailable_reason is None:
            live_sql = apply_table_map(cleaned_sql, self.table_map)
            try:
                res = self._post_live_sql(live_sql, limit)
                res["canonical_sql"] = cleaned_sql
                self.executed_queries.append(
                    {"sql": live_sql, "mode": "live_zetaris", "status": res["status"], "row_count": res["row_count"]}
                )
                return res
            except (httpx.HTTPError, ValueError) as exc:  # transport, HTTP status, or non-JSON body
                self.live_unavailable_reason = type(exc).__name__
                if self.trace_logger is not None:
                    self.trace_logger.log(
                        agent_name=agent_name,
                        action="fallback_to_sample_data",
                        input_summary=f"Live Zetaris failed ({type(exc).__name__}) on SQL: {cleaned_sql[:90]}",
                        output_summary=(
                            "Circuit opened: this and all remaining queries in the run execute on the deterministic "
                            "SampleZetarisEngine over data/sample/."
                        ),
                        target_agent=agent_name,
                        confidence=0.95,
                        retry_count=1,
                        status="fallback",
                        sponsor_tech="zetaris",
                    )

        res = self.sample_engine.execute_sql(cleaned_sql, limit=limit)
        self.executed_queries.append(
            {"sql": cleaned_sql, "mode": "sample_zetaris", "status": res["status"], "row_count": res["row_count"]}
        )
        return res

    def check_status(self) -> str:
        if self.sample_mode:
            return "sample_mode_active"
        if not self.api_key:
            return "unconfigured_fallback_sample"
        # The documented connectivity check is SELECT 1 over /sql-editor/sqls/run (there is no /health route).
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Org-ID": str(self.org_id),
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        payload = {"queryId": str(uuid.uuid4()), "sql": "SELECT 1 AS one", "source": "SqlEditor", "limit": 1}
        try:
            with httpx.Client(timeout=min(5.0, self.timeout)) as client:
                r = client.post(f"{self.rest_url}/sql-editor/sqls/run", headers=headers, json=payload)
        except httpx.HTTPError:
            return "offline_fallback_sample"
        if r.status_code == 401:
            return "unauthorized_create_new_api_key"
        if r.status_code == 400:
            return "rejected_check_org_id"
        return "online" if r.status_code < 300 else "degraded_fallback_sample"
