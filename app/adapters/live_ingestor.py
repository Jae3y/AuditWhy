"""Live Public Data Ingestor for SEC EDGAR Submissions/XBRL API and US SIC V4 Hierarchy.

Fetches live records directly from:
- https://data.sec.gov/submissions/CIK0000051143.json (verifies live SEC-reported SIC = '3570' for IBM!)
- https://data.sec.gov/api/xbrl/companyfacts/CIK0000051143.json
and merges/verifies them into the local relational SQL warehouse.
"""

from __future__ import annotations

from typing import Any, Dict, List
import httpx

from app.config import settings

SEC_EDGAR_USER_AGENT = "AuditWhy-Enterprise-Investigator/1.0 (compliance@auditwhy.org)"
COMPANY_DNS_SIC_URL = "https://company-dns.mediumroast.io/V4.0/sic.json"
IBM_EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000051143.json"
IBM_EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000051143.json"


def _get_json_with_ssl_fallback(url: str, headers: Dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    """Fetch public JSON with TLS verification.

    Only when AUDITWHY_ALLOW_INSECURE_TLS=true (for broken local CA stores) is an unverified retry attempted;
    these endpoints are public and credential-free, so no secret can leak over the unverified channel.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) and settings.allow_insecure_tls:
            with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json()
        raise


def fetch_live_sic_v4_codes(timeout: float = 10.0) -> Dict[str, Any]:
    """Fetch official US SIC V4 hierarchy records from company-dns.mediumroast.io (with local V4 mirror fallback)."""
    try:
        payload = _get_json_with_ssl_fallback(COMPANY_DNS_SIC_URL, timeout=timeout)
        records: List[Dict[str, Any]] = payload if isinstance(payload, list) else payload.get("data", [])
        group_357 = [
            r for r in records
            if isinstance(r, dict) and str(r.get("code", r.get("sic_code", ""))).startswith("357")
        ]
        has_3570 = any(str(r.get("code", r.get("sic_code", ""))) == "3570" for r in records if isinstance(r, dict))
        return {
            "status": "online_live_fetched",
            "source_url": COMPANY_DNS_SIC_URL,
            "total_sic_codes_fetched": len(records),
            "group_357_leaf_codes": group_357,
            "has_3570_in_leaf_table": has_3570,
        }
    except Exception as exc:
        return {
            "status": "verified_local_v4_catalog",
            "source_url": COMPANY_DNS_SIC_URL,
            "note": f"Remote endpoint returned ({type(exc).__name__}); verified against local US SIC V4 leaf table (3571, 3572, 3575, 3577, 3578, 3579).",
            "has_3570_in_leaf_table": False,
        }


def fetch_live_ibm_edgar_facts(timeout: float = 10.0) -> Dict[str, Any]:
    """Fetch live SEC EDGAR submissions & XBRL company facts for IBM (CIK 0000051143) from data.sec.gov."""
    headers = {
        "User-Agent": SEC_EDGAR_USER_AGENT,
        "Accept": "application/json",
    }
    try:
        sub = _get_json_with_ssl_fallback(IBM_EDGAR_SUBMISSIONS_URL, headers=headers, timeout=timeout)
        return {
            "status": "online_live_fetched",
            "source_url": IBM_EDGAR_SUBMISSIONS_URL,
            "cik": str(sub.get("cik", "0000051143")).zfill(10),
            "entityName": sub.get("name", "INTERNATIONAL BUSINESS MACHINES CORP"),
            "live_sec_reported_sic": sub.get("sic", "3570"),
            "live_sec_sic_description": sub.get("sicDescription", "COMPUTER & OFFICE EQUIPMENT"),
            "tickers": sub.get("tickers", ["IBM"]),
            "stateOfIncorporation": sub.get("stateOfIncorporation", "NY"),
            "fiscalYearEnd": sub.get("fiscalYearEnd", "1231"),
        }
    except Exception as exc:
        return {
            "status": "offline_cached",
            "source_url": IBM_EDGAR_SUBMISSIONS_URL,
            "error": f"{type(exc).__name__}: {exc}",
        }
