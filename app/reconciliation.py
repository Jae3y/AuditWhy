"""Taxonomy reconciliation: mapping SEC EDGAR summary SIC codes onto US SIC V4 4-digit leaves.

EDGAR filers sometimes self-report a summary header (e.g. IBM files under 3570, the Industry Group 357 header)
that does not exist in the 4-digit leaf table. A naive equi-join drops those filers. These helpers turn the
hierarchy carried in `company_dns.sic_codes.parent_summary_code` into an explicit summary -> leaf mapping and the
hierarchy-aware SQL that applies it, with no company- or code-specific special cases.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

_SIC_CODE = re.compile(r"^\d{3,4}$")

LEAF_POLICIES = ("primary_leaf", "peer_majority")


def active_leaf_policy() -> str:
    """Reconciliation policy, overridable with AUDITWHY_SIC_LEAF_POLICY (primary_leaf | peer_majority)."""
    policy = os.getenv("AUDITWHY_SIC_LEAF_POLICY", "primary_leaf").strip().lower()
    return policy if policy in LEAF_POLICIES else "primary_leaf"


def is_valid_sic_code(code: Any) -> bool:
    return bool(_SIC_CODE.match(str(code or "")))


def leaf_children(summary_code: str, sic_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """4-digit leaves under `summary_code`, preferring the explicit hierarchy column over prefix matching."""
    # Live Zetaris REST returns every value as a string, so "false"/"0" must count as False too.
    rows = [r for r in sic_rows if str(r.get("is_leaf_4digit", 1)).strip().lower() not in ("0", "false")]
    explicit = [r for r in rows if r.get("parent_summary_code") == summary_code]
    if explicit:
        return sorted(explicit, key=lambda r: str(r["sic_code"]))
    prefix = str(summary_code).rstrip("0") or str(summary_code)
    return sorted(
        (r for r in rows if str(r["sic_code"]).startswith(prefix) and r["sic_code"] != summary_code),
        key=lambda r: str(r["sic_code"]),
    )


def choose_reconciled_leaf(
    summary_code: str,
    children: List[Dict[str, Any]],
    peer_leaf_codes: List[str],
    policy: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Pick the leaf a summary-coded filer is reconciled to.

    Policies:
      primary_leaf (default): the lowest-coded leaf. By SIC convention the "...1" leaf is the industry group's
        primary line (3571 Electronic Computers under 3570), and the choice never depends on who else is in the
        dataset, so the same filing reconciles identically in every run.
      peer_majority: the leaf most used by the filer's industry-group peers, ties broken by code. Useful when a
        cohort is dominated by one line of business, but it lets unrelated filers influence the mapping.

    Either way the filer stays inside its industry group, so group-level revenue and ARR totals are identical;
    only the leaf-level attribution differs. Callers record the policy and all candidates for audit.
    """
    if not children:
        return None
    policy = policy or active_leaf_policy()
    if policy == "peer_majority":
        child_codes = {str(c["sic_code"]) for c in children}
        counts = Counter(code for code in peer_leaf_codes if code in child_codes)
        if counts:
            best = max(counts.values())
            winner = min(code for code, n in counts.items() if n == best)
            return next(c for c in children if str(c["sic_code"]) == winner)
    return children[0]


def build_leaf_mapping(
    orphan_rows: Iterable[Dict[str, Any]],
    sic_rows: List[Dict[str, Any]],
    peer_rows: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Dict[str, Any]]:
    """Return {summary_code: {leaf_code, leaf_description, candidates, ciks}} for every reconcilable orphan."""
    peers = list(peer_rows)
    mapping: Dict[str, Dict[str, Any]] = {}
    for orphan in orphan_rows:
        code = str(orphan.get("invalid_sic_code") or orphan.get("edgar_sic_code") or orphan.get("sic_code") or "")
        if not is_valid_sic_code(code):
            continue
        entry = mapping.get(code)
        if entry is None:
            children = leaf_children(code, sic_rows)
            group = str(orphan.get("industry_group") or code[:3])
            peer_codes = [
                str(p.get("leaf_sic_code") or p.get("sic_code"))
                for p in peers
                if str(p.get("industry_group")) == group and p.get("leaf_sic_code")
            ]
            policy = active_leaf_policy()
            chosen = choose_reconciled_leaf(code, children, peer_codes, policy=policy)
            if chosen is None:
                continue
            entry = {
                "leaf_code": str(chosen["sic_code"]),
                "leaf_description": chosen.get("description", ""),
                "industry_group": chosen.get("industry_group", group),
                "hierarchy": " -> ".join(
                    part
                    for part in (
                        f"Division {chosen.get('division')} ({chosen.get('division_name')})" if chosen.get("division") else "",
                        f"Major Group {chosen.get('major_group')}" if chosen.get("major_group") else "",
                        f"Industry Group {chosen.get('industry_group')}" if chosen.get("industry_group") else "",
                        f"Leaf {chosen['sic_code']} ({chosen.get('description', '')})",
                    )
                    if part
                ),
                "candidates": [str(c["sic_code"]) for c in children],
                "leaf_policy": policy,
                "ciks": [],
            }
            mapping[code] = entry
        cik = orphan.get("cik")
        if cik and cik not in entry["ciks"]:
            entry["ciks"].append(cik)
    return mapping


def edgar_sic_join_sql(mapping: Dict[str, str], limit: int = 1000) -> str:
    """EDGAR x SIC join. With an empty mapping this is the naive equi-join that silently drops summary codes."""
    select = (
        "SELECT e.cik, e.ticker, e.company_name, e.sic_code AS edgar_sic_code, e.industry_group, "
        "e.revenue_2024, e.revenue_2025, s.sic_code AS leaf_sic_code, s.description AS leaf_sic_description "
        "FROM edgar.company_facts e "
    )
    safe = {k: v for k, v in sorted(mapping.items()) if is_valid_sic_code(k) and is_valid_sic_code(v)}
    if not safe:
        return f"{select}JOIN company_dns.sic_codes s ON e.sic_code = s.sic_code LIMIT {int(limit)}"
    hierarchy_terms = " OR ".join(f"(e.sic_code = '{k}' AND s.sic_code = '{v}')" for k, v in safe.items())
    return f"{select}LEFT JOIN company_dns.sic_codes s ON e.sic_code = s.sic_code OR {hierarchy_terms} LIMIT {int(limit)}"
