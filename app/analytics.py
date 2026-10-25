"""Small, dependency-free statistics and query-focus helpers used by the analyst agents."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Words too generic to identify a company on their own ("Western" alone should not match Western Digital).
_GENERIC_NAME_WORDS = {
    "INTERNATIONAL", "WESTERN", "EASTERN", "GENERAL", "AMERICAN", "UNITED", "NATIONAL", "GLOBAL", "FIRST",
    "SYSTEMS", "BUSINESS", "MACHINES", "CORP", "CORPORATION", "INC", "CO", "COMPANY", "HOLDINGS", "GROUP",
}
_NAME_SUFFIXES = re.compile(r"\b(?:CORP(?:ORATION)?|INC|CO|LTD|PLC|LLC|HOLDINGS|GROUP|SYSTEMS|THE)\b\.?", re.I)


def pearson_r(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x <= 1e-12 or den_y <= 1e-12:
        return 0.0
    return round(num / (den_x * den_y), 3)


def pearson_ci95(r: float, n: int) -> Dict[str, float]:
    """95% confidence interval for Pearson r via the Fisher z-transform (requires n >= 4)."""
    if n < 4 or abs(r) >= 1.0:
        return {"low": round(r, 3), "high": round(r, 3)}
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    return {"low": round(math.tanh(z - 1.96 * se), 3), "high": round(math.tanh(z + 1.96 * se), 3)}


def mean(values: Iterable[float]) -> float:
    vals = [float(v) for v in values]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def short_company_name(company_name: str) -> str:
    """'INTERNATIONAL BUSINESS MACHINES CORP' -> 'INTERNATIONAL BUSINESS MACHINES'."""
    return re.sub(r"\s+", " ", _NAME_SUFFIXES.sub("", company_name or "")).strip(" ,.")


def account_aliases(row: Dict[str, Any]) -> List[str]:
    """Phrases that identify an account in free text: CIK, ticker, short name, distinctive first word."""
    aliases: List[str] = []
    cik = str(row.get("cik") or "")
    if cik:
        aliases.extend([cik, cik.lstrip("0")])
    ticker = str(row.get("ticker") or "").upper()
    if len(ticker) >= 2:
        aliases.append(ticker)
    short = short_company_name(str(row.get("company_name") or row.get("account_name") or "")).upper()
    if short:
        aliases.append(short)
        first = short.split()[0]
        if len(first) >= 4 and first not in _GENERIC_NAME_WORDS:
            aliases.append(first)
        if len(short) <= 3:  # e.g. "HP"
            aliases.append(short)
    return [a for a in dict.fromkeys(aliases) if a]


def _mentions(text_upper: str, phrase: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(phrase.upper())}(?![A-Z0-9])", text_upper) is not None


def _normalize_segment(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").upper().replace("&", "AND")).strip()


def resolve_focus(query: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Work out which accounts the question is about.

    Order of precedence: named accounts (CIK / ticker / name) -> named industry segments -> SIC codes or
    industry groups -> otherwise the segment with the lowest average net retention (an explicit auto-selection).
    `accounts` rows need cik, account_name/company_name, industry_segment, industry_group, q3_net_retention_pct.
    """
    q_upper = (query or "").upper()
    q_segment = _normalize_segment(query)

    named = [a for a in accounts if any(_mentions(q_upper, alias) for alias in account_aliases(a))]
    if named:
        return {
            "mode": "named_accounts",
            "label": ", ".join(str(a.get("account_name") or a.get("company_name")) for a in named),
            "ciks": [a["cik"] for a in named],
            "segments": sorted({str(a.get("industry_segment")) for a in named}),
        }

    segments = sorted({str(a.get("industry_segment")) for a in accounts if a.get("industry_segment")})
    named_segments = [s for s in segments if _normalize_segment(s) in q_segment]
    if named_segments:
        ciks = [a["cik"] for a in accounts if a.get("industry_segment") in named_segments]
        return {"mode": "named_segment", "label": " + ".join(named_segments), "ciks": ciks, "segments": named_segments}

    codes = set(re.findall(r"\b(\d{3,4})\b", q_upper))
    coded = [
        a for a in accounts
        if codes & {str(a.get("industry_group")), str(a.get("sic_code_edgar")), str(a.get("sic_code"))}
    ]
    if coded:
        segs = sorted({str(a.get("industry_segment")) for a in coded})
        return {"mode": "named_sic", "label": " + ".join(segs), "ciks": [a["cik"] for a in coded], "segments": segs}

    by_segment: Dict[str, List[float]] = {}
    for a in accounts:
        by_segment.setdefault(str(a.get("industry_segment")), []).append(float(a.get("q3_net_retention_pct") or 0.0))
    if not by_segment:
        return {"mode": "none", "label": "all accounts", "ciks": [], "segments": []}
    worst = min(by_segment, key=lambda s: mean(by_segment[s]))
    return {
        "mode": "auto_lowest_retention_segment",
        "label": worst,
        "ciks": [a["cik"] for a in accounts if a.get("industry_segment") == worst],
        "segments": [worst],
    }


def fmt_usd(value: Optional[float]) -> str:
    v = float(value or 0)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:,.3f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:,.2f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:,.0f}K"
    return f"{sign}${v:,.0f}"
