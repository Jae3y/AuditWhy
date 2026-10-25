# AuditWhy Data Layer — Zetaris Networked Data Catalog & Sample Mirror

This directory contains the local deterministic mirror of the Zetaris Unified Semantic Layer (USL) tables used when `SAMPLE_MODE=true` or when a live Zetaris Lightning cluster (`http://localhost:8888/api/v1.0`) is unreachable.

## Datasets Included

1. **`sample/edgar_company_facts.json`** (`lightning.datasource.rest.sec_edgar` / `edgar.company_facts`)
   - 10 enterprise public company filings from SEC EDGAR (`data.sec.gov/api/xbrl/companyfacts`).
   - Includes **IBM (`CIK 0000051143`)** which self-reports SEC SIC code **`3570`** (*Computer and Office Equipment*, $62.753B FY2025 revenue), alongside Apple (`3571`), Western Digital (`3572`), Diebold Nixdorf (`3575`), Cisco (`3577`), HP Inc (`3571`), NetApp (`3572`), Microsoft (`7372`), Oracle (`7372`), and Dominion Energy (`4911`).

2. **`sample/company_dns_sic_codes.json`** (`lightning.datasource.rest.company_dns` / `company_dns.sic_codes`)
   - Official US SIC V4 4-digit leaf taxonomy records from `company-dns.mediumroast.io/V4.0/sic.json`.
   - **Real-World Taxonomic Anomaly:** In the official US SIC V4 specification, `3570` is a 3-digit Industry Group summary header (`357` — *Computer and Office Equipment* under Division D, Major Group 35), **not** a 4-digit leaf code (`3571` Electronic Computers, `3572` Computer Storage Devices, `3575` Computer Terminals, `3577` Computer Peripheral Equipment, NEC).
   - Naive inner joins between `edgar.company_facts` and `company_dns.sic_codes` on `sic_code` silently drop IBM (`$62.753B` revenue), triggering the Zetaris Data Quality foreign-key rule `fk_sic_check`.

3. **`sample/erp_customer_churn_billing.json`** (`erp.customer_churn_billing`)
   - Aggregated enterprise B2B contract cohorts (412 underlying billing events across 10 enterprise accounts and 4 industry segments) tracking Q1–Q4 ARR, net retention rate, churn flags, SLA credit payouts, and Q4 regional weather disruption exposure.
   - **Intentional Provenance Gap:** Contains **no pricing-tier change table or price-increase history**, ensuring that any agent hypothesis attributing Q3 churn to pricing changes is intercepted and blocked by `EvidenceEvaluatorAgent` as an `untested_factor`.

4. **`sample/support_tickets_usage.json`** (`ops.support_tickets_usage`)
   - Quarterly P1/P2 escalation counts, API gateway p99 latency (`ms`), timeout error rates (`%`), and telemetry across enterprise accounts. Across all 10 accounts, batch-sync timeout rate correlates with Q3 retention loss at $r \approx 0.96$. The agents compute this at run time; nothing downstream hardcodes it.

## Editing the Data

Every verdict is derived from these files, so they are safe to edit for experiments. Point a run at a copy with `AuditWhyOrchestrator(sample_dir=...)`. For example, set HP's `sic_code` to `3570`, or Oracle's to `7370`, and the evaluator will catch and reconcile each new orphan. `auditwhy_warehouse.db` is a read-only snapshot for inspection and is rebuilt automatically when these files or the schema code change.
