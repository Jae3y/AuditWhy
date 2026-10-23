"""Zetaris Networked Data Platform REST SQL Client with USL & DQ support and automatic Sample fallback."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.adapters.sample_data import SampleZetarisEngine
from app.config import settings
from app.engines.scout_intent import ExecutionContract, ScoutIntentEngine
from app.tracing import TraceLogger


class ZetarisSQLValidationError(ValueError):
    """Raised when a query violates Zetaris Lightning SQL single-statement rules."""


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
    ) -> None:
        base = (rest_url or settings.zetaris_rest_url).strip()
        if ":8889" in base:
            base = base.replace(":8889", ":8888")
        self.rest_url = base.rstrip("/")
        self.api_key = (api_key if api_key is not None else settings.zetaris_api_key).strip()
        self.org_id = org_id if org_id is not None else settings.zetaris_org_id
        self.sample_mode = sample_mode if sample_mode is not None else settings.sample_mode
        self.scout_engine = scout_engine
        self.trace_logger = trace_logger
        self.sample_engine = SampleZetarisEngine()
        self.executed_queries: List[Dict[str, Any]] = []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
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
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "SUCCESS",
                "execution_engine": "ZetarisLightningREST",
                "sql": cleaned_sql,
                "row_count": len(data.get("rows", data.get("data", []))),
                "rows": data.get("rows", data.get("data", [])),
                "raw": data,
            }

    def run_sql(
        self,
        sql: str,
        limit: int = 100,
        *,
        contract: Optional[ExecutionContract | Dict[str, Any]] = None,
        tool_name: str = "query_zetaris",
        agent_name: str = "DataRetrieverAgent",
    ) -> Dict[str, Any]:
        """Validate contract (if scout_engine configured), sanitize SQL, and execute against Zetaris or SampleZetarisEngine."""
        if self.scout_engine is not None:
            self.scout_engine.verify_contract_for_tool(contract, tool_name)

        cleaned_sql = sanitize_zetaris_sql(sql)

        if not self.sample_mode and self.api_key:
            try:
                res = self._post_live_sql(cleaned_sql, limit)
                self.executed_queries.append(
                    {"sql": cleaned_sql, "mode": "live_zetaris", "row_count": res["row_count"]}
                )
                return res
            except Exception as exc:
                if self.trace_logger is not None:
                    self.trace_logger.log(
                        agent_name=agent_name,
                        action="fallback_to_sample_data",
                        input_summary=f"Live Zetaris unreachable ({type(exc).__name__}); falling back to SampleZetarisEngine for SQL: {cleaned_sql[:90]}",
                        output_summary="Switched execution to deterministic SampleZetarisEngine over data/sample/",
                        target_agent=agent_name,
                        confidence=0.95,
                        retry_count=1,
                        status="fallback",
                        sponsor_tech="zetaris",
                    )

        res = self.sample_engine.execute_sql(cleaned_sql, limit=limit)
        self.executed_queries.append(
            {"sql": cleaned_sql, "mode": "sample_zetaris", "row_count": res["row_count"]}
        )
        return res

    def check_status(self) -> str:
        if self.sample_mode:
            return "sample_mode_active"
        if not self.api_key:
            return "unconfigured_fallback_sample"
        try:
            with httpx.Client(timeout=3.0) as client:
                r = client.get(f"{self.rest_url}/health")
                return "online" if r.status_code < 500 else "degraded_fallback_sample"
        except Exception:
            return "offline_fallback_sample"
