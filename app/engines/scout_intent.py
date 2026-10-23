"""Meterless Engine 4: Scout Intent Guard & Signed Execution Contracts.

Implements:
- 6-Stage Pipeline: Sense -> Interpret -> Guard -> Route -> Recommend -> ExecutionContract
- 5-Layer Security Guard:
  (1) Prompt Injection Check (ignore previous instructions, drop table, system prompt extraction, exfiltration URLs, base64 payloads)
  (2) RBAC Policy Gate
  (3) Scope Check
  (4) PII/Sensitivity Redaction
  (5) Rate Limit Check
- Exact Intent Scoring Formula:
  0.40 * lexical + 0.35 * semantic + 0.20 * contextual + 0.05 * recency - risk_penalty
- HMAC-SHA256 Signed ExecutionContract (signedBy: 'scout', 5-minute TTL)
- Contract enforcement gate for tool calls (query_zetaris, verify_dq, write_memory)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import LOGS_DIR, redact_sensitive_text, settings


class ScoutContractError(PermissionError):
    """Raised when a tool execution is attempted without a valid Scout HMAC-SHA256 ExecutionContract."""


_INJECTION_PATTERNS = [
    ("ignore_instructions", re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I)),
    ("system_prompt_override", re.compile(r"(?:system\s+prompt|reveal\s+your\s+instructions|developer\s+message)", re.I)),
    ("destructive_sql", re.compile(r"\b(?:drop\s+table|truncate\s+table|delete\s+from|alter\s+table)\b", re.I)),
    ("secret_exfiltration", re.compile(r"(?:exfiltrate|ZETARIS_API_KEY|METERLESS_API_KEY|NVIDIA_API_KEY|evil\.example\.com|https?://[^\s]*evil)", re.I)),
    ("base64_payload", re.compile(r"(?:base64\s*-d|eval\s*\(\s*atob|[A-Za-z0-9+/]{48,}={0,2})", re.I)),
]

_PII_PATTERNS = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

ALLOWED_TOOLS = {"query_zetaris", "verify_dq", "write_memory", "read_memory", "analyze_world_model"}


@dataclass
class ExecutionContract:
    contract_id: str
    run_id: str
    intent: str
    allowed_tools: List[str]
    scope: str
    risk: Dict[str, Any]
    score: float
    issued_at: int
    expires_at: int
    signedBy: str = "scout"
    signature: str = ""

    def canonical_payload(self) -> str:
        payload = {
            "contract_id": self.contract_id,
            "run_id": self.run_id,
            "intent": self.intent,
            "allowed_tools": sorted(self.allowed_tools),
            "scope": self.scope,
            "risk_level": self.risk.get("level", "low"),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "signedBy": self.signedBy,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ScoutIntentEngine:
    """Executes the Scout Intent 6-stage pipeline and verifies HMAC-SHA256 ExecutionContracts."""

    def __init__(
        self,
        signing_key: Optional[str] = None,
        contracts_log_path: Optional[Path] = None,
        rate_limit_per_minute: int = 120,
    ) -> None:
        self.signing_key = (signing_key or settings.scout_hmac_key).encode("utf-8")
        self.contracts_log_path = contracts_log_path or (LOGS_DIR / "scout_contracts.jsonl")
        self.contracts_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.rate_limit_per_minute = rate_limit_per_minute
        self._request_timestamps: List[float] = []

    @staticmethod
    def calculate_intent_score(
        *,
        lexical: float,
        semantic: float,
        contextual: float,
        recency: float,
        risk_penalty: float,
    ) -> float:
        """Exact Scout Intent scoring formula:
        0.40 * lexical + 0.35 * semantic + 0.20 * contextual + 0.05 * recency - risk_penalty
        """
        raw = (
            0.40 * float(lexical)
            + 0.35 * float(semantic)
            + 0.20 * float(contextual)
            + 0.05 * float(recency)
            - float(risk_penalty)
        )
        return round(max(0.0, min(1.0, raw)), 4)

    def sign_contract(self, contract: ExecutionContract) -> str:
        msg = contract.canonical_payload().encode("utf-8")
        return hmac.new(self.signing_key, msg, hashlib.sha256).hexdigest()

    def evaluate(
        self,
        user_query: str,
        *,
        run_id: str = "system",
        role: str = "investigator",
        scope: str = "enterprise_audit",
    ) -> Dict[str, Any]:
        """Run Sense -> Interpret -> Guard -> Route -> Recommend -> ExecutionContract."""
        now_ts = int(time.time())
        clean_query = (user_query or "").strip()

        # 1. Sense
        tokens = re.findall(r"[a-z0-9_]+", clean_query.lower())
        audit_keywords = {
            "why", "retention", "churn", "edgar", "sic", "3570", "revenue",
            "enterprise", "support", "escalation", "billing", "q3", "q4",
            "sla", "weather", "computer", "equipment", "taxonomy", "discrepancy",
        }
        matched_keywords = [t for t in tokens if t in audit_keywords]
        lexical_score = min(1.0, len(set(matched_keywords)) / 4.0) if tokens else 0.0
        semantic_score = 0.88 if matched_keywords else 0.45
        contextual_score = 0.90 if scope == "enterprise_audit" else 0.50
        recency_score = 1.0

        # 2. Interpret
        intent_label = "enterprise_discrepancy_investigation"
        if "q4" in tokens or "weather" in tokens or "re-evaluate" in clean_query.lower():
            intent_label = "warm_sla_and_taxonomy_reevaluation"

        # 3. Guard (5-Layer Security Gate)
        # Layer 1: Prompt Injection Check
        injection_hits: List[str] = []
        for name, pattern in _INJECTION_PATTERNS:
            if pattern.search(clean_query):
                injection_hits.append(name)

        # Layer 2: RBAC Policy Gate
        rbac_allowed = role in ("investigator", "admin", "judge", "system")

        # Layer 3: Scope Check
        scope_allowed = scope in ("enterprise_audit", "taxonomy_review", "security_test")

        # Layer 4: PII/Sensitivity Redaction
        sanitized_query = redact_sensitive_text(clean_query)
        pii_redacted: List[str] = []
        for pii_name, pii_pat in _PII_PATTERNS:
            if pii_pat.search(sanitized_query):
                pii_redacted.append(pii_name)
                sanitized_query = pii_pat.sub(f"[REDACTED_{pii_name.upper()}]", sanitized_query)

        # Layer 5: Rate Limit Check
        now_flt = time.time()
        self._request_timestamps = [t for t in self._request_timestamps if now_flt - t < 60.0]
        self._request_timestamps.append(now_flt)
        rate_limit_ok = len(self._request_timestamps) <= self.rate_limit_per_minute

        is_blocked = bool(injection_hits) or not rbac_allowed or not scope_allowed or not rate_limit_ok
        risk_penalty = 1.0 if is_blocked else (0.15 if pii_redacted else 0.0)
        intent_score = self.calculate_intent_score(
            lexical=lexical_score,
            semantic=semantic_score,
            contextual=contextual_score,
            recency=recency_score,
            risk_penalty=risk_penalty,
        )

        risk_info = {
            "level": "blocked" if is_blocked else ("elevated" if pii_redacted else "low"),
            "penalty": risk_penalty,
            "layers": {
                "layer1_prompt_injection": {
                    "passed": len(injection_hits) == 0,
                    "violations": injection_hits,
                },
                "layer2_rbac_gate": {"passed": rbac_allowed, "role": role},
                "layer3_scope_check": {"passed": scope_allowed, "scope": scope},
                "layer4_pii_redaction": {"passed": True, "redacted_types": pii_redacted},
                "layer5_rate_limit": {
                    "passed": rate_limit_ok,
                    "requests_in_window": len(self._request_timestamps),
                },
            },
        }

        # 4. Route & 5. Recommend
        allowed_tools = [] if is_blocked else ["query_zetaris", "verify_dq", "write_memory", "read_memory", "analyze_world_model"]
        recommended_route = "security_refusal" if is_blocked else "planner_agent"

        # 6. ExecutionContract (5-minute TTL = 300s)
        contract = ExecutionContract(
            contract_id=f"sct-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            intent="blocked_adversarial_input" if is_blocked else intent_label,
            allowed_tools=allowed_tools,
            scope=scope,
            risk=risk_info,
            score=intent_score,
            issued_at=now_ts,
            expires_at=now_ts + 300,
            signedBy="scout",
        )
        contract.signature = self.sign_contract(contract)

        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
            "pipeline_stages": ["Sense", "Interpret", "Guard", "Route", "Recommend", "ExecutionContract"],
            "sanitized_query": sanitized_query[:240],
            "recommended_route": recommended_route,
            "contract": contract.to_dict(),
        }
        with self.contracts_log_path.open("a", encoding="utf-8") as f:
            f.write(redact_sensitive_text(json.dumps(log_entry, ensure_ascii=False)) + "\n")

        return {
            "blocked": is_blocked,
            "sanitized_query": sanitized_query,
            "intent": contract.intent,
            "score": intent_score,
            "risk": risk_info,
            "recommended_route": recommended_route,
            "contract": contract,
        }

    def verify_contract_for_tool(
        self,
        contract: Optional[ExecutionContract | Dict[str, Any]],
        tool_name: str,
    ) -> bool:
        """Verify HMAC-SHA256 signature, TTL, risk status, and tool permission before allowing a tool call."""
        if contract is None:
            self._log_refusal("missing_contract", tool_name, "No ExecutionContract supplied")
            raise ScoutContractError(f"Tool '{tool_name}' refused: missing Scout ExecutionContract.")

        obj = contract if isinstance(contract, ExecutionContract) else ExecutionContract(**contract)

        if obj.signedBy != "scout":
            self._log_refusal(obj.contract_id, tool_name, f"Invalid signer: {obj.signedBy}")
            raise ScoutContractError(f"Tool '{tool_name}' refused: contract not signedBy 'scout'.")

        if obj.risk.get("level") == "blocked":
            self._log_refusal(obj.contract_id, tool_name, "Contract flagged as blocked by 5-layer guard")
            raise ScoutContractError(f"Tool '{tool_name}' refused: contract risk level is 'blocked'.")

        expected_sig = self.sign_contract(obj)
        if not obj.signature or not hmac.compare_digest(obj.signature, expected_sig):
            self._log_refusal(obj.contract_id, tool_name, "HMAC-SHA256 signature mismatch")
            raise ScoutContractError(f"Tool '{tool_name}' refused: invalid HMAC-SHA256 signature.")

        now_ts = int(time.time())
        if now_ts > obj.expires_at:
            self._log_refusal(obj.contract_id, tool_name, "ExecutionContract expired (TTL > 300s)")
            raise ScoutContractError(f"Tool '{tool_name}' refused: ExecutionContract expired.")

        if tool_name not in obj.allowed_tools:
            self._log_refusal(obj.contract_id, tool_name, f"Tool '{tool_name}' not in allowed_tools")
            raise ScoutContractError(f"Tool '{tool_name}' refused: not authorized by contract.")

        return True

    def _log_refusal(self, contract_id: str, tool_name: str, reason: str) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": "tool_execution_refused",
            "contract_id": contract_id,
            "tool_name": tool_name,
            "reason": reason,
        }
        with self.contracts_log_path.open("a", encoding="utf-8") as f:
            f.write(redact_sensitive_text(json.dumps(entry, ensure_ascii=False)) + "\n")
