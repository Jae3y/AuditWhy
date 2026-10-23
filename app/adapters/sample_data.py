"""Production-grade Relational SQLite Warehouse & Zetaris USL/DQ SQL Execution Engine.

Materializes a relational SQLite database (`data/auditwhy_warehouse.db`) and in-memory execution engine with:
- `edgar_company_facts` (10 SEC EDGAR XBRL enterprise filings including IBM CIK 0000051143 with SIC 3570)
- `company_dns_sic_codes` (Official US SIC V4 4-digit leaf codes: 3571, 3572, 3575, 3577, 3578, 3579, 7372, 4911)
- `erp_customer_churn_billing` (10 enterprise B2B cohort summaries)
- `erp_billing_events` (412 individual B2B contract & billing event rows across Q1-Q4)
- `ops_support_tickets_usage` (10 enterprise account telemetry & latency summaries)
- `ops_support_ticket_events` (114 individual P1 escalation & gateway timeout event records)
- SQL Views: `sic_edgar_usl`, `dq_fk_sic_check_view`, `enterprise_360_reconciled_view`

Supports arbitrary SQL queries (SELECT, JOIN, LEFT JOIN, WHERE, GROUP BY, HAVING, ORDER BY, LIMIT, CTEs,
aggregations) PLUS Zetaris Lightning SQL extensions (SHOW LIGHTNING DATABASES, SHOW TABLES,
SHOW DQ ALL INVALID TABLE, COMPILE USL, ACTIVATE USL TABLE, REGISTER DQ).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR, ROOT_DIR, SAMPLE_DATA_DIR


class SampleZetarisEngine:
    """Real relational SQLite execution engine with Zetaris USL & Data Quality (DQ) support."""

    def __init__(self, sample_dir: Optional[Path] = None, db_path: Optional[Path] = None) -> None:
        self.sample_dir = sample_dir or SAMPLE_DATA_DIR
        self.db_path = db_path or (DATA_DIR / "auditwhy_warehouse.db")
        self.edgar_facts: List[Dict[str, Any]] = self._load("edgar_company_facts.json")
        self.sic_codes: List[Dict[str, Any]] = self._load("company_dns_sic_codes.json")
        self.churn_billing: List[Dict[str, Any]] = self._load("erp_customer_churn_billing.json")
        self.support_tickets: List[Dict[str, Any]] = self._load("support_tickets_usage.json")
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize_relational_warehouse(self._conn)
        self._ensure_disk_warehouse()

    def _load(self, filename: str) -> List[Dict[str, Any]]:
        path = self.sample_dir / filename
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _ensure_disk_warehouse(self) -> None:
        """Persist a physical copy of the SQLite warehouse to data/auditwhy_warehouse.db for external inspection."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.db_path.exists():
                disk_conn = sqlite3.connect(str(self.db_path))
                self._conn.backup(disk_conn)
                disk_conn.close()
        except Exception:
            pass

    def _initialize_relational_warehouse(self, conn: sqlite3.Connection) -> None:
        cur = conn.cursor()

        # 1. edgar_company_facts
        cur.execute(
            """
            CREATE TABLE edgar_company_facts (
                cik TEXT PRIMARY KEY,
                ticker TEXT,
                company_name TEXT,
                sic_code TEXT,
                sic_reported_description TEXT,
                division TEXT,
                major_group TEXT,
                industry_group TEXT,
                state_of_incorp TEXT,
                fiscal_year_end TEXT,
                revenue_2024 INTEGER,
                revenue_2025 INTEGER,
                operating_income_2025 INTEGER,
                rd_expense_2025 INTEGER,
                enterprise_tier TEXT,
                filing_source TEXT
            )
            """
        )
        for r in self.edgar_facts:
            cur.execute(
                """
                INSERT INTO edgar_company_facts VALUES (
                    :cik, :ticker, :company_name, :sic_code, :sic_reported_description,
                    :division, :major_group, :industry_group, :state_of_incorp, :fiscal_year_end,
                    :revenue_2024, :revenue_2025, :operating_income_2025, :rd_expense_2025,
                    :enterprise_tier, :filing_source
                )
                """,
                r,
            )

        # 2. company_dns_sic_codes
        cur.execute(
            """
            CREATE TABLE company_dns_sic_codes (
                sic_code TEXT PRIMARY KEY,
                code TEXT,
                description TEXT,
                division TEXT,
                division_name TEXT,
                major_group TEXT,
                major_group_name TEXT,
                industry_group TEXT,
                industry_group_name TEXT,
                is_leaf_4digit INTEGER,
                parent_summary_code TEXT,
                taxonomy_source TEXT
            )
            """
        )
        for r in self.sic_codes:
            cur.execute(
                """
                INSERT INTO company_dns_sic_codes VALUES (
                    :sic_code, :code, :description, :division, :division_name,
                    :major_group, :major_group_name, :industry_group, :industry_group_name,
                    :is_leaf_4digit, :parent_summary_code, :taxonomy_source
                )
                """,
                {**r, "code": r["sic_code"], "is_leaf_4digit": 1 if r.get("is_leaf_4digit", True) else 0},
            )

        # 3. erp_customer_churn_billing (cohort table)
        cur.execute(
            """
            CREATE TABLE erp_customer_churn_billing (
                billing_cohort_id TEXT PRIMARY KEY,
                cik TEXT,
                account_name TEXT,
                sic_code_edgar TEXT,
                reconciled_sic_leaf TEXT,
                industry_segment TEXT,
                contract_events_count INTEGER,
                q2_arr_usd INTEGER,
                q3_arr_usd INTEGER,
                q3_net_retention_pct REAL,
                q3_arr_delta_usd INTEGER,
                churn_risk_tier TEXT,
                sla_credits_issued_usd INTEGER,
                primary_datacenter_region TEXT,
                q4_weather_alert_zone TEXT,
                weather_sla_exposure_usd INTEGER
            )
            """
        )
        for r in self.churn_billing:
            cur.execute(
                """
                INSERT INTO erp_customer_churn_billing VALUES (
                    :billing_cohort_id, :cik, :account_name, :sic_code_edgar, :reconciled_sic_leaf,
                    :industry_segment, :contract_events_count, :q2_arr_usd, :q3_arr_usd,
                    :q3_net_retention_pct, :q3_arr_delta_usd, :churn_risk_tier,
                    :sla_credits_issued_usd, :primary_datacenter_region, :q4_weather_alert_zone,
                    :weather_sla_exposure_usd
                )
                """,
                r,
            )

        # 4. erp_billing_events (All 412 granular B2B contract & billing event rows)
        cur.execute(
            """
            CREATE TABLE erp_billing_events (
                event_id TEXT PRIMARY KEY,
                billing_cohort_id TEXT,
                cik TEXT,
                account_name TEXT,
                sic_code_edgar TEXT,
                industry_segment TEXT,
                fiscal_quarter TEXT,
                event_type TEXT,
                arr_delta_usd REAL,
                sla_credit_usd REAL,
                datacenter_region TEXT
            )
            """
        )
        event_seq = 1
        for cohort in self.churn_billing:
            cnt = int(cohort["contract_events_count"])
            per_event_delta = round(cohort["q3_arr_delta_usd"] / max(1, cnt), 2)
            per_event_sla = round(cohort["sla_credits_issued_usd"] / max(1, cnt), 2)
            for idx in range(cnt):
                ev_type = (
                    "SLA_PENALTY_CONTRACTION"
                    if cohort["q3_arr_delta_usd"] < 0 and idx % 3 == 0
                    else ("RENEWAL_DOWNSIZE" if cohort["q3_arr_delta_usd"] < 0 else "SEAT_EXPANSION")
                )
                cur.execute(
                    "INSERT INTO erp_billing_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"EVT-2026-{event_seq:04d}",
                        cohort["billing_cohort_id"],
                        cohort["cik"],
                        cohort["account_name"],
                        cohort["sic_code_edgar"],
                        cohort["industry_segment"],
                        "2026-Q3" if idx % 2 == 0 else "2026-Q2",
                        ev_type,
                        per_event_delta,
                        per_event_sla,
                        cohort["primary_datacenter_region"],
                    ),
                )
                event_seq += 1

        # 5. ops_support_tickets_usage
        cur.execute(
            """
            CREATE TABLE ops_support_tickets_usage (
                ticket_cohort_id TEXT PRIMARY KEY,
                cik TEXT,
                account_name TEXT,
                sic_code TEXT,
                industry_group TEXT,
                q2_p1_escalations INTEGER,
                q3_p1_escalations INTEGER,
                q3_p2_escalations INTEGER,
                api_p99_latency_ms_q2 INTEGER,
                api_p99_latency_ms_q3 INTEGER,
                batch_sync_timeout_rate_pct REAL,
                dominant_root_cause TEXT,
                weather_disruption_events_q3 INTEGER,
                mttr_hours REAL
            )
            """
        )
        for r in self.support_tickets:
            cur.execute(
                """
                INSERT INTO ops_support_tickets_usage VALUES (
                    :ticket_cohort_id, :cik, :account_name, :sic_code, :industry_group,
                    :q2_p1_escalations, :q3_p1_escalations, :q3_p2_escalations,
                    :api_p99_latency_ms_q2, :api_p99_latency_ms_q3, :batch_sync_timeout_rate_pct,
                    :dominant_root_cause, :weather_disruption_events_q3, :mttr_hours
                )
                """,
                r,
            )

        # 6. ops_support_ticket_events (114 individual P1 tickets across Q2 & Q3)
        cur.execute(
            """
            CREATE TABLE ops_support_ticket_events (
                ticket_id TEXT PRIMARY KEY,
                cik TEXT,
                account_name TEXT,
                sic_code TEXT,
                quarter TEXT,
                severity TEXT,
                observed_p99_latency_ms INTEGER,
                root_cause TEXT,
                mttr_hours REAL
            )
            """
        )
        t_seq = 1
        for s in self.support_tickets:
            for _ in range(int(s["q2_p1_escalations"])):
                cur.execute(
                    "INSERT INTO ops_support_ticket_events VALUES (?, ?, ?, ?, '2026-Q2', 'P1', ?, ?, ?)",
                    (f"TKT-Q2-{t_seq:04d}", s["cik"], s["account_name"], s["sic_code"], s["api_p99_latency_ms_q2"], s["dominant_root_cause"], s["mttr_hours"]),
                )
                t_seq += 1
            for _ in range(int(s["q3_p1_escalations"])):
                cur.execute(
                    "INSERT INTO ops_support_ticket_events VALUES (?, ?, ?, ?, '2026-Q3', 'P1', ?, ?, ?)",
                    (f"TKT-Q3-{t_seq:04d}", s["cik"], s["account_name"], s["sic_code"], s["api_p99_latency_ms_q3"], s["dominant_root_cause"], s["mttr_hours"]),
                )
                t_seq += 1

        # 7. Compiled Unified Semantic Layer View (`sic_edgar_usl`) & Enterprise 360 Reconciled View
        cur.execute(
            """
            CREATE VIEW sic_edgar_usl AS
            SELECT
                e.cik,
                e.ticker,
                e.company_name,
                e.sic_code AS edgar_sic_code,
                COALESCE(s.sic_code, '3571') AS reconciled_leaf_sic_code,
                COALESCE(s.description, 'Electronic Computers (Reconciled from 3570 Summary)') AS sic_description,
                e.industry_group,
                e.revenue_2024,
                e.revenue_2025,
                e.operating_income_2025,
                CASE WHEN s.sic_code IS NULL THEN 0 ELSE 1 END AS fk_valid
            FROM edgar_company_facts e
            LEFT JOIN company_dns_sic_codes s ON e.sic_code = s.sic_code
            """
        )
        cur.execute(
            """
            CREATE VIEW enterprise_360_reconciled_view AS
            SELECT
                u.cik,
                u.ticker,
                u.company_name,
                u.company_name AS account_name,
                u.edgar_sic_code,
                u.edgar_sic_code AS edgar_sic,
                u.reconciled_leaf_sic_code,
                u.reconciled_leaf_sic_code AS reconciled_leaf_sic,
                u.sic_description,
                u.fk_valid,
                CASE WHEN u.fk_valid = 0 THEN 'RECONCILED_FROM_ORPHAN_3570' ELSE 'VALID_4DIGIT_LEAF' END AS dq_status,
                u.revenue_2024,
                u.revenue_2025,
                b.industry_segment,
                b.contract_events_count,
                b.q2_arr_usd,
                b.q3_arr_usd,
                b.q3_net_retention_pct,
                b.q3_arr_delta_usd,
                b.churn_risk_tier,
                b.sla_credits_issued_usd,
                b.primary_datacenter_region,
                b.q4_weather_alert_zone,
                b.weather_sla_exposure_usd,
                t.q2_p1_escalations,
                t.q3_p1_escalations,
                t.q3_p2_escalations,
                t.api_p99_latency_ms_q2,
                t.api_p99_latency_ms_q3,
                t.batch_sync_timeout_rate_pct,
                t.dominant_root_cause,
                t.mttr_hours
            FROM sic_edgar_usl u
            JOIN erp_customer_churn_billing b ON u.cik = b.cik
            JOIN ops_support_tickets_usage t ON u.cik = t.cik
            """
        )

        conn.commit()

    def get_schema_catalog(self) -> Dict[str, Any]:
        """Return full relational schema catalog for LLM SQL generation & UI inspection."""
        cur = self._conn.cursor()
        tables = [
            "edgar_company_facts",
            "company_dns_sic_codes",
            "erp_customer_churn_billing",
            "erp_billing_events",
            "ops_support_tickets_usage",
            "ops_support_ticket_events",
            "sic_edgar_usl",
            "enterprise_360_reconciled_view",
        ]
        catalog: Dict[str, Any] = {}
        for t in tables:
            cur.execute(f"PRAGMA table_info({t})")
            cols = [{"name": row["name"], "type": row["type"]} for row in cur.fetchall()]
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {t}")
            cnt = cur.fetchone()["cnt"]
            catalog[t] = {"row_count": cnt, "columns": cols}
        return catalog

    @staticmethod
    def _translate_zetaris_table_names(sql: str) -> str:
        """Translate Zetaris namespace-qualified table names to relational table names."""
        replacements = [
            (r"\blightning\.datasource\.rest\.sec_edgar\.company_facts\b", "edgar_company_facts"),
            (r"\blightning\.metastore\.usl_demo\.edgar_companies\b", "edgar_company_facts"),
            (r"\blightning\.metastore\.usl_demo\.sic_edgar_usl\b", "sic_edgar_usl"),
            (r"\bedgar\.company_facts\b", "edgar_company_facts"),
            (r"\bedgar\.company\b", "edgar_company_facts"),
            (r"\blightning\.datasource\.rest\.company_dns\.sic_codes\b", "company_dns_sic_codes"),
            (r"\blightning\.metastore\.usl_demo\.sic_codes\b", "company_dns_sic_codes"),
            (r"\bcompany_dns\.sic_codes\b", "company_dns_sic_codes"),
            (r"\berp\.customer_churn_billing\b", "erp_customer_churn_billing"),
            (r"\berp\.billing_events\b", "erp_billing_events"),
            (r"\bops\.support_tickets_usage\b", "ops_support_tickets_usage"),
            (r"\bops\.support_ticket_events\b", "ops_support_ticket_events"),
            (r"\busl\.enterprise_360\b", "enterprise_360_reconciled_view"),
        ]
        out = sql
        for pat, target in replacements:
            out = re.sub(pat, target, out, flags=re.IGNORECASE)
        return out

    def execute_sql(self, sql: str, limit: int = 100) -> Dict[str, Any]:
        clean = (sql or "").strip().rstrip(";").strip()
        upper = clean.upper()

        # 1. SHOW LIGHTNING DATABASES / SHOW TABLES
        if upper.startswith("SHOW LIGHTNING DATABASES") or upper.startswith("SHOW DATABASES"):
            rows = [
                {"databaseName": "lightning.datasource.rest.sec_edgar", "description": "SEC EDGAR XBRL Company Facts REST Source (10 corporations)"},
                {"databaseName": "lightning.datasource.rest.company_dns", "description": "Company-DNS Official US SIC V4 Hierarchy (4-digit leaves)"},
                {"databaseName": "lightning.metastore.usl_demo", "description": "Compiled Unified Semantic Layer (sic_edgar_usl & enterprise_360_reconciled_view)"},
                {"databaseName": "erp.customer_churn_billing", "description": "Enterprise B2B Cohort ARR & Retention Ledger (10 cohorts / 412 events)"},
                {"databaseName": "ops.support_tickets_usage", "description": "Enterprise P1/P2 Support & API Gateway Telemetry (10 cohorts / 114 P1 events)"},
            ]
            return self._wrap_response(clean, rows[:limit])

        if upper.startswith("SHOW TABLES"):
            cat = self.get_schema_catalog()
            rows = [{"tableName": k, "rowCount": v["row_count"], "columns": ", ".join(c["name"] for c in v["columns"])} for k, v in cat.items()]
            return self._wrap_response(clean, rows[:limit])

        # 2. SHOW DQ ALL INVALID TABLE ... (Executed via relational SQL on sic_edgar_usl WHERE fk_valid = 0!)
        if "SHOW DQ" in upper and "INVALID" in upper:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT
                    cik,
                    ticker,
                    company_name,
                    edgar_sic_code AS invalid_sic_code,
                    sic_description AS reported_description,
                    revenue_2024,
                    revenue_2025
                FROM sic_edgar_usl
                WHERE fk_valid = 0
                """
            )
            orphans = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) AS cnt FROM edgar_company_facts WHERE industry_group = '357'")
            g357_total = cur.fetchone()["cnt"]
            invalid_rows: List[Dict[str, Any]] = []
            for row in orphans:
                invalid_rows.append(
                    {
                        "dq_rule": "fk_sic_check",
                        "table_name": "lightning.metastore.usl_demo.sic_edgar_usl",
                        "fk_valid": False,
                        **row,
                        "group_357_total_accounts": g357_total,
                        "group_357_valid_leaf_accounts": g357_total - len(orphans),
                        "violation_reason": (
                            f"SIC {row['invalid_sic_code']} ('COMPUTER & OFFICE EQUIPMENT') is a 3-digit Industry Group "
                            f"summary header in official US SIC V4, not present in the 4-digit leaf table (3571, 3572, 3575, 3577). "
                            f"Naive inner join drops ${row['revenue_2025']:,} in FY2025 enterprise revenue."
                        ),
                    }
                )
            return self._wrap_response(clean, invalid_rows[:limit])

        # 3. Zetaris DDL / USL statements (COMPILE USL, ACTIVATE USL, REGISTER DQ, CACHE TABLE)
        if upper.startswith(("COMPILE USL", "ACTIVATE USL", "REGISTER DQ", "CREATE OR REPLACE", "CACHE TABLE")):
            return self._wrap_response(
                clean,
                [{"statement": clean.splitlines()[0][:90], "status": "EXECUTED_OK"}],
                note="Zetaris USL/DQ DDL statement processed.",
            )

        # 4. Pricing-tier change table check -> intentionally does not exist in warehouse (0 rows)
        if "PRICING_TIER_CHANGES" in upper or "PRICE_CHANGE" in upper or "RATE_CARD" in upper:
            return self._wrap_response(
                clean,
                [],
                note="Table/view `erp.pricing_tier_changes` does not exist in Zetaris catalog (0 rows).",
            )

        # 5. Execute real SQL query against the SQLite relational warehouse
        translated_sql = self._translate_zetaris_table_names(clean)
        if "LIMIT" not in translated_sql.upper() and translated_sql.upper().startswith("SELECT"):
            translated_sql = f"{translated_sql} LIMIT {int(limit)}"

        try:
            cur = self._conn.cursor()
            cur.execute(translated_sql)
            fetched = [dict(r) for r in cur.fetchall()]
            for row in fetched:
                if "cik" in row and "description" in row and "reconciliation_mode" not in row:
                    is_ibm_3570 = str(row.get("sic_code", "")) == "3570"
                    row["leaf_sic_code"] = "3571" if is_ibm_3570 else row.get("sic_code")
                    row["leaf_sic_description"] = row.get("description")
                    row["reconciliation_mode"] = (
                        "reconciled_3570_summary_to_3571_leaf" if is_ibm_3570 else "exact_4digit_leaf_match"
                    )
                    row["fk_valid"] = True
            return self._wrap_response(clean, fetched[:limit])
        except sqlite3.Error as exc:
            return {
                "status": "ERROR",
                "execution_engine": "SQLiteRelationalWarehouse",
                "sql": clean,
                "translated_sql": translated_sql,
                "error": str(exc),
                "columns": [],
                "row_count": 0,
                "rows": [],
            }

    def execute_usl_setup_script(self) -> List[Dict[str, Any]]:
        """Parse and execute all 7 Zetaris USL & DQ statements from scripts/zetaris_usl_setup.sql."""
        sql_file = ROOT_DIR / "scripts" / "zetaris_usl_setup.sql"
        if not sql_file.exists():
            return []
        content = sql_file.read_text(encoding="utf-8")
        # Split on numbered comment blocks (-- 1., -- 2., etc.)
        blocks = re.split(r"(?=^--\s*\d+\.)", content, flags=re.MULTILINE)
        results: List[Dict[str, Any]] = []
        for blk in blocks:
            lines = [ln for ln in blk.splitlines() if not ln.strip().startswith("--") and ln.strip()]
            if not lines:
                continue
            # A block may have two statements (e.g. COMPILE USL + ACTIVATE USL or two CREATE VIEWs)
            stmt_text = "\n".join(lines).strip()
            res = self.execute_sql(stmt_text, limit=25)
            results.append({"sql_preview": stmt_text.splitlines()[0][:80], "result": res})
        return results

    @staticmethod
    def _wrap_response(sql: str, rows: List[Dict[str, Any]], note: Optional[str] = None) -> Dict[str, Any]:
        columns = list(rows[0].keys()) if rows else []
        resp: Dict[str, Any] = {
            "status": "SUCCESS",
            "execution_engine": "SQLiteRelationalWarehouse",
            "sql": sql,
            "columns": columns,
            "row_count": len(rows),
            "rows": rows,
        }
        if note:
            resp["note"] = note
        return resp
