-- =============================================================================
-- AuditWhy — Zetaris Unified Semantic Layer (USL) & Data Quality (DQ) Setup
-- Target: Zetaris Networked Data Platform (http://localhost:8888/api/v1.0)
-- Note: Execute each block as a single statement via POST /sql-editor/sqls/run
-- =============================================================================

-- 1. Register Company-DNS Official US SIC V4 Taxonomy REST Datasource
CREATE OR REPLACE TEMPORARY VIEW company_dns_raw
USING org.apache.spark.sql.execution.datasources.rest.RestDataSource
OPTIONS (
  url "https://company-dns.mediumroast.io/V4.0/sic.json",
  method "GET",
  readTimeout "20000",
  connectionTimeout "10000"
)

-- 2. Cache and flatten SIC V4 4-digit leaf codes into Schemastore
CACHE TABLE company_dns_cached AS
SELECT
  col.code AS sic_code,
  col.description AS description,
  col.division AS division,
  col.major_group AS major_group,
  col.industry_group AS industry_group
FROM (
  SELECT explode(from_json(content, 'ARRAY<STRUCT<code:STRING,description:STRING,division:STRING,major_group:STRING,industry_group:STRING>>')) AS col
  FROM company_dns_raw
)

-- 3. Register SEC EDGAR XBRL Company Facts REST Datasource (IBM CIK 0000051143 + Enterprise Cohort)
CREATE OR REPLACE TEMPORARY VIEW sec_edgar_raw
USING org.apache.spark.sql.execution.datasources.rest.RestDataSource
OPTIONS (
  url "https://data.sec.gov/api/xbrl/companyfacts/CIK0000051143.json",
  method "GET",
  header_User_Agent "AuditWhy-Hackathon/1.0 (compliance@auditwhy.example.org)"
)

-- 4. Create Schemastore Views for Unified Semantic Layer Compilation
CREATE OR REPLACE SCHEMASTORE VIEW lightning.metastore.usl_demo.sic_codes AS
SELECT sic_code, description, division, major_group, industry_group
FROM company_dns_cached

CREATE OR REPLACE SCHEMASTORE VIEW lightning.metastore.usl_demo.edgar_companies AS
SELECT
  cik,
  ticker,
  company_name,
  sic_code,
  sic_reported_description,
  state_of_incorp,
  revenue_2024,
  revenue_2025
FROM lightning.datasource.rest.sec_edgar.company_facts

-- 5. Compile & Activate the Unified Semantic Layer (USL) with Foreign Key Relationship
COMPILE USL sic_edgar_usl
DEPLOY NAMESPACE lightning.metastore.usl_demo AS (
  ENTITY sic_taxonomy FROM lightning.metastore.usl_demo.sic_codes PRIMARY KEY (sic_code);
  ENTITY enterprise_filings FROM lightning.metastore.usl_demo.edgar_companies PRIMARY KEY (cik)
    FOREIGN KEY (sic_code) REFERENCES sic_taxonomy (sic_code);
)

ACTIVATE USL TABLE lightning.metastore.usl_demo.sic_edgar_usl

-- 6. Register Zetaris Data Quality (DQ) Foreign Key Check
-- Detects that IBM (CIK 0000051143) reports SIC '3570' (3-digit Industry Group summary header)
-- which is absent from the 4-digit leaf table (3571, 3572, 3575, 3577), flagging 1 invalid orphan row.
REGISTER DQ fk_sic_check
ON TABLE lightning.metastore.usl_demo.sic_edgar_usl
RULE (
  FOREIGN_KEY_VALID(enterprise_filings.sic_code, sic_taxonomy.sic_code)
)
SEVERITY HIGH

-- 7. Diagnostic Query executed by EvidenceEvaluatorAgent
SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl
