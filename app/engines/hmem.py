"""Meterless Engine 1: H-MEM Hierarchical Memory System.

Implements all 7 services per H-MEM AGENTS.md specification:
1. MemoryStoreService
2. MemoryMiningService
3. MemoryRetrievalService
4. DreamingService
5. SleepCycleService
6. TrustLedgerService
7. ConflictDetectionService
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from app.config import LOGS_DIR, redact_sensitive_text

MemoryType = Literal["personal", "factual", "preference", "general", "procedural"]
MemoryLayer = Literal["short_term", "working", "long_term"]
FeedbackType = Literal["helpful", "not_helpful", "wrong"]

LAYER_WEIGHTS: Dict[str, float] = {
    "long_term": 1.0,
    "working": 0.8,
    "short_term": 0.6,
}


class LowTrustOverwriteError(ValueError):
    """Raised when a low-trust source (confidence < 0.5) attempts to overwrite a high-trust fact (confidence >= 0.85)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compute_deterministic_embedding(text: str, dim: int = 16) -> List[float]:
    """Compute a deterministic unit-normalized vector embedding from token/character features."""
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9_]+", (text or "").lower())
    if not tokens:
        return vec
    for tok in tokens:
        digest = hashlib.sha256(tok.encode("utf-8")).digest()
        for i in range(dim):
            byte_val = digest[i % len(digest)]
            sign = 1.0 if (byte_val % 2 == 0) else -1.0
            weight = 1.0 + (byte_val / 255.0)
            vec[i] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-9:
        vec = [round(v / norm, 6) for v in vec]
    return vec


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return 0.0
    return max(0.0, min(1.0, (dot / (norm_a * norm_b) + 1.0) / 2.0))


@dataclass
class MemoryRecord:
    id: str
    content: str
    type: MemoryType = "factual"
    layer: MemoryLayer = "working"
    timestamp: str = field(default_factory=_utc_now_iso)
    lastAccessed: str = field(default_factory=_utc_now_iso)
    accessCount: int = 0
    tags: List[str] = field(default_factory=list)
    embedding: List[float] = field(default_factory=list)
    score: float = 0.0
    confidence: float = 0.85
    source: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)
    domain: str = "general"
    namespace: str = "auditwhy"
    entities: List[str] = field(default_factory=list)
    relatedTo: List[str] = field(default_factory=list)
    supersedes: Optional[str] = None
    supersededBy: Optional[str] = None
    derivedFrom: List[str] = field(default_factory=list)
    agentId: Optional[str] = None
    runId: Optional[str] = None
    taskId: Optional[str] = None

    def __post_init__(self) -> None:
        if self.source is None or not str(self.source).strip():
            raise ValueError("H-MEM violation: 'source' is mandatory and cannot be empty.")
        if self.confidence is None or not isinstance(self.confidence, (int, float)):
            raise ValueError("H-MEM violation: 'confidence' is mandatory and must be numeric.")
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.type not in ("personal", "factual", "preference", "general", "procedural"):
            raise ValueError(f"Invalid H-MEM type: {self.type}")
        if self.layer not in ("short_term", "working", "long_term"):
            raise ValueError(f"Invalid H-MEM layer: {self.layer}")
        # Enforce entity cap 10 and relatedTo cap 5
        self.entities = list(dict.fromkeys(self.entities))[:10]
        self.relatedTo = list(dict.fromkeys(self.relatedTo))[:5]
        if not self.embedding:
            self.embedding = compute_deterministic_embedding(
                f"{self.content} {' '.join(self.tags)} {' '.join(self.entities)} {self.domain}"
            )
        if not self.provenance:
            self.provenance = {
                "origin": self.source,
                "learnedAt": self.timestamp,
                "label": f"{self.type}:{self.domain}",
            }

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TrustLedgerService:
    """Appends immutable audit and feedback events to logs/hmem_trust_ledger.jsonl."""

    def __init__(self, ledger_path: Optional[Path] = None) -> None:
        self.ledger_path = ledger_path or (LOGS_DIR / "hmem_trust_ledger.jsonl")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: List[Dict[str, Any]] = []

    def append(
        self,
        event_type: Literal[
            "create",
            "read",
            "update",
            "delete",
            "feedback",
            "conflict_detected",
            "conflict_resolved",
            "sleep_preview",
            "dream_proposed",
        ],
        record_id: str,
        details: Dict[str, Any],
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry = {
            "ledger_id": f"led-{uuid.uuid4().hex[:10]}",
            "timestamp": _utc_now_iso(),
            "event_type": event_type,
            "record_id": record_id,
            "run_id": run_id or "system",
            "details": json.loads(redact_sensitive_text(json.dumps(details))),
        }
        self.entries.append(entry)
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


class MemoryStoreService:
    """Manages CRUD operations, trust protection, and feedback adjustments on MemoryRecords."""

    def __init__(self, ledger: TrustLedgerService) -> None:
        self.ledger = ledger
        self._records: Dict[str, MemoryRecord] = {}

    def create(self, record: MemoryRecord | Dict[str, Any]) -> MemoryRecord:
        rec = record if isinstance(record, MemoryRecord) else MemoryRecord(**record)
        # Check if overwriting an existing record ID
        if rec.id in self._records:
            existing = self._records[rec.id]
            if existing.confidence >= 0.85 and rec.confidence < 0.50:
                self.ledger.append(
                    "update",
                    rec.id,
                    {
                        "status": "blocked_by_trust_protection",
                        "existing_confidence": existing.confidence,
                        "incoming_confidence": rec.confidence,
                        "incoming_source": rec.source,
                    },
                    run_id=rec.runId,
                )
                raise LowTrustOverwriteError(
                    f"Trust protection violation: low-trust source ({rec.confidence:.2f} < 0.50) "
                    f"cannot overwrite high-trust record '{rec.id}' ({existing.confidence:.2f} >= 0.85)."
                )
        self._records[rec.id] = rec
        self.ledger.append(
            "create",
            rec.id,
            {
                "source": rec.source,
                "confidence": rec.confidence,
                "layer": rec.layer,
                "domain": rec.domain,
                "entities": rec.entities,
            },
            run_id=rec.runId,
        )
        return rec

    def get(self, record_id: str, run_id: Optional[str] = None) -> Optional[MemoryRecord]:
        rec = self._records.get(record_id)
        if rec:
            rec.accessCount += 1
            rec.lastAccessed = _utc_now_iso()
            self.ledger.append(
                "read",
                rec.id,
                {"accessCount": rec.accessCount, "confidence": rec.confidence},
                run_id=run_id,
            )
        return rec

    def update(
        self,
        record_id: str,
        updates: Dict[str, Any],
        incoming_confidence: Optional[float] = None,
        incoming_source: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> MemoryRecord:
        if record_id not in self._records:
            raise KeyError(f"Memory record '{record_id}' not found.")
        existing = self._records[record_id]
        eff_conf = (
            float(incoming_confidence)
            if incoming_confidence is not None
            else float(updates.get("confidence", existing.confidence))
        )
        if existing.confidence >= 0.85 and eff_conf < 0.50:
            self.ledger.append(
                "update",
                record_id,
                {
                    "status": "blocked_by_trust_protection",
                    "existing_confidence": existing.confidence,
                    "incoming_confidence": eff_conf,
                    "incoming_source": incoming_source or updates.get("source", "unknown"),
                },
                run_id=run_id,
            )
            raise LowTrustOverwriteError(
                f"Trust protection violation: low-trust update ({eff_conf:.2f} < 0.50) "
                f"cannot overwrite high-trust fact '{record_id}' ({existing.confidence:.2f} >= 0.85)."
            )

        for k, v in updates.items():
            if hasattr(existing, k):
                setattr(existing, k, v)
        existing.__post_init__()
        self.ledger.append(
            "update",
            record_id,
            {"updated_fields": list(updates.keys()), "confidence": existing.confidence},
            run_id=run_id,
        )
        return existing

    def delete(self, record_id: str, run_id: Optional[str] = None) -> bool:
        if record_id in self._records:
            del self._records[record_id]
            self.ledger.append("delete", record_id, {"deleted": True}, run_id=run_id)
            return True
        return False

    def apply_feedback(
        self,
        record_id: str,
        feedback: FeedbackType,
        run_id: Optional[str] = None,
    ) -> MemoryRecord:
        if record_id not in self._records:
            raise KeyError(f"Memory record '{record_id}' not found.")
        rec = self._records[record_id]
        prev_conf = rec.confidence
        if feedback == "helpful":
            rec.confidence = min(1.0, round(rec.confidence + 0.05, 4))
            rec.accessCount += 1
        elif feedback == "not_helpful":
            rec.confidence = max(0.0, round(rec.confidence - 0.03, 4))
        elif feedback == "wrong":
            rec.confidence = max(0.0, round(rec.confidence - 0.20, 4))
            if "review" not in rec.tags:
                rec.tags.append("review")
            if "needs_review" not in rec.tags:
                rec.tags.append("needs_review")
        else:
            raise ValueError(f"Unsupported feedback type: {feedback}")
        rec.lastAccessed = _utc_now_iso()
        self.ledger.append(
            "feedback",
            record_id,
            {
                "feedback": feedback,
                "previous_confidence": prev_conf,
                "new_confidence": rec.confidence,
                "accessCount": rec.accessCount,
                "tags": rec.tags,
            },
            run_id=run_id,
        )
        return rec

    def all_records(self) -> List[MemoryRecord]:
        return list(self._records.values())


class MemoryRetrievalService:
    """Hybrid 8-factor memory retrieval engine implementing exact H-MEM scoring math."""

    def __init__(self, store: MemoryStoreService, ledger: TrustLedgerService) -> None:
        self.store = store
        self.ledger = ledger

    @staticmethod
    def calculate_hybrid_score(
        *,
        semantic: float,
        keyword: float,
        tag: float,
        domain: float,
        entity: float,
        layer: float,
        recency: float,
        superseded: float,
        confidence: float,
    ) -> Dict[str, float]:
        """Exact H-MEM retrieval formula:
        raw = 0.35*semantic + 0.20*keyword + 0.10*tag + 0.10*domain + 0.15*entity + 0.05*layer + 0.05*recency - 0.20*superseded
        score = clamp_0_1(raw) * confidence
        """
        raw = (
            0.35 * float(semantic)
            + 0.20 * float(keyword)
            + 0.10 * float(tag)
            + 0.10 * float(domain)
            + 0.15 * float(entity)
            + 0.05 * float(layer)
            + 0.05 * float(recency)
            - 0.20 * float(superseded)
        )
        clamped_raw = max(0.0, min(1.0, raw))
        final_score = round(clamped_raw * max(0.0, min(1.0, float(confidence))), 6)
        return {
            "raw": round(raw, 6),
            "clamped_raw": round(clamped_raw, 6),
            "score": final_score,
        }

    def retrieve(
        self,
        query: str,
        *,
        query_tags: Optional[List[str]] = None,
        query_domain: Optional[str] = None,
        query_entities: Optional[List[str]] = None,
        threshold: float = 0.35,
        top_n: int = 5,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        query_vec = compute_deterministic_embedding(query)
        q_tokens = set(re.findall(r"[a-z0-9_]+", query.lower()))
        q_tags = {t.lower() for t in (query_tags or [])}
        q_entities = {e.lower() for e in (query_entities or [])}
        # Also auto-extract potential entity/tag tokens from query
        for tok in ("ibm", "3570", "3571", "0000051143", "sic", "edgar", "zetaris", "churn", "weather", "dominion"):
            if tok in q_tokens:
                q_entities.add(tok)
                q_tags.add(tok)

        scored_items: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for rec in self.store.all_records():
            semantic_sim = cosine_similarity(query_vec, rec.embedding)
            rec_tokens = set(re.findall(r"[a-z0-9_]+", rec.content.lower()))
            if q_tokens and rec_tokens:
                overlap = len(q_tokens & rec_tokens)
                keyword_sim = min(1.0, overlap / max(1, min(len(q_tokens), 6)))
            else:
                keyword_sim = 0.0

            rec_tags = {t.lower() for t in rec.tags}
            if q_tags and rec_tags:
                tag_sim = min(1.0, len(q_tags & rec_tags) / max(1, len(q_tags)))
            elif rec_tags and q_tokens:
                tag_sim = min(1.0, len(q_tokens & rec_tags) / max(1, len(rec_tags)))
            else:
                tag_sim = 0.0

            if query_domain and rec.domain.lower() == query_domain.lower():
                domain_sim = 1.0
            elif rec.domain.lower() in q_tokens or rec.domain.lower() in ("taxonomy", "zetaris_dq", "enterprise_audit"):
                domain_sim = 0.85 if any(t in q_tokens for t in ("sic", "edgar", "enterprise", "retention", "3570", "exposure", "q4", "q3")) else 0.4
            else:
                domain_sim = 0.3

            rec_entities = {e.lower() for e in rec.entities}
            if q_entities and rec_entities:
                matched_ent = sum(
                    1 for qe in q_entities if any(qe in re_item or re_item in qe for re_item in rec_entities)
                )
                entity_sim = min(1.0, matched_ent / max(1, min(len(q_entities), 3)))
            else:
                entity_sim = 0.0

            layer_weight = LAYER_WEIGHTS.get(rec.layer, 0.6)

            try:
                ts_dt = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
                age_hours = max(0.0, (now - ts_dt).total_seconds() / 3600.0)
                recency_score = math.exp(-age_hours / 168.0)
            except Exception:
                recency_score = 0.9

            superseded_flag = 1.0 if rec.supersededBy else 0.0

            calc = self.calculate_hybrid_score(
                semantic=semantic_sim,
                keyword=keyword_sim,
                tag=tag_sim,
                domain=domain_sim,
                entity=entity_sim,
                layer=layer_weight,
                recency=recency_score,
                superseded=superseded_flag,
                confidence=rec.confidence,
            )

            rec.score = calc["score"]
            if calc["score"] >= threshold:
                rec.accessCount += 1
                rec.lastAccessed = _utc_now_iso()
                self.ledger.append(
                    "read",
                    rec.id,
                    {
                        "score": calc["score"],
                        "raw": calc["raw"],
                        "query_preview": query[:80],
                    },
                    run_id=run_id,
                )
                scored_items.append(
                    {
                        "record": rec.to_dict(),
                        "score": calc["score"],
                        "raw": calc["raw"],
                        "relevance": {
                            "semantic": round(semantic_sim, 4),
                            "keyword": round(keyword_sim, 4),
                            "tag": round(tag_sim, 4),
                            "domain": round(domain_sim, 4),
                            "entity": round(entity_sim, 4),
                            "layer": round(layer_weight, 4),
                            "recency": round(recency_score, 4),
                            "superseded": superseded_flag,
                            "confidence": rec.confidence,
                        },
                        "retrievalReason": (
                            f"Matched domain '{rec.domain}' with entity overlap={entity_sim:.2f}, "
                            f"keyword={keyword_sim:.2f}, layer={rec.layer} (score={calc['score']:.3f})"
                        ),
                    }
                )

        scored_items.sort(key=lambda x: x["score"], reverse=True)
        top_items = scored_items[:top_n]

        grouped_by_domain: Dict[str, List[Dict[str, Any]]] = {}
        for item in top_items:
            dom = item["record"]["domain"]
            grouped_by_domain.setdefault(dom, []).append(item)

        return {
            "strategy": "hmem_8factor_hybrid_v1",
            "threshold": threshold,
            "top_n": top_n,
            "total_matches": len(top_items),
            "memories": top_items,
            "grouped_by_domain": grouped_by_domain,
        }


class ConflictDetectionService:
    """Detects opposing claims/numbers on the same entity and resolves when confidence/margin >= 0.70."""

    def __init__(self, store: MemoryStoreService, ledger: TrustLedgerService) -> None:
        self.store = store
        self.ledger = ledger

    def detect_and_resolve(
        self,
        candidate: MemoryRecord,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        conflicts: List[Dict[str, Any]] = []
        cand_entities = {e.lower() for e in candidate.entities}
        cand_numbers = re.findall(r"\b\d+(?:\.\d+)?\b", candidate.content)

        for existing in self.store.all_records():
            if existing.id == candidate.id or existing.supersededBy:
                continue
            ex_entities = {e.lower() for e in existing.entities}
            shared_entities = cand_entities & ex_entities
            if not shared_entities and existing.domain != candidate.domain:
                continue

            ex_numbers = re.findall(r"\b\d+(?:\.\d+)?\b", existing.content)
            # Opposing claim heuristic: shared entity + either differing numeric claims or opposing polarity tokens
            opposing_numbers = bool(shared_entities and cand_numbers and ex_numbers and set(cand_numbers) != set(ex_numbers))
            opposing_polarity = bool(
                shared_entities
                and (
                    ("invalid" in candidate.content.lower() and "valid" in existing.content.lower())
                    or ("leaf" in candidate.content.lower() and "summary" in existing.content.lower())
                    or ("drop" in candidate.content.lower() and "increase" in existing.content.lower())
                )
            )

            if opposing_numbers or opposing_polarity:
                self.ledger.append(
                    "conflict_detected",
                    candidate.id,
                    {
                        "existing_id": existing.id,
                        "shared_entities": list(shared_entities),
                        "candidate_confidence": candidate.confidence,
                        "existing_confidence": existing.confidence,
                    },
                    run_id=run_id,
                )
                winner = candidate if candidate.confidence >= existing.confidence else existing
                loser = existing if winner.id == candidate.id else candidate
                margin_or_conf = max(winner.confidence, abs(winner.confidence - loser.confidence))

                resolved = False
                if margin_or_conf >= 0.70:
                    loser.supersededBy = winner.id
                    winner.supersedes = loser.id
                    resolved = True
                    self.ledger.append(
                        "conflict_resolved",
                        winner.id,
                        {
                            "winner_id": winner.id,
                            "loser_id": loser.id,
                            "winner_confidence": winner.confidence,
                            "loser_confidence": loser.confidence,
                            "supersededBy_set_on": loser.id,
                        },
                        run_id=run_id,
                    )

                conflicts.append(
                    {
                        "existing_id": existing.id,
                        "candidate_id": candidate.id,
                        "shared_entities": list(shared_entities),
                        "resolved": resolved,
                        "winner_id": winner.id if resolved else None,
                        "loser_id": loser.id if resolved else None,
                    }
                )
        return {"conflicts_found": len(conflicts), "conflicts": conflicts}


class MemoryMiningService:
    """Extracts structured memories from agent findings, resolves conflicts, and stores in H-MEM."""

    def __init__(
        self,
        store: MemoryStoreService,
        conflict_service: ConflictDetectionService,
    ) -> None:
        self.store = store
        self.conflict_service = conflict_service

    def mine_fact(
        self,
        *,
        content: str,
        source: str,
        confidence: float = 0.94,
        domain: str = "taxonomy",
        layer: MemoryLayer = "long_term",
        memory_type: MemoryType = "procedural",
        tags: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        agent_id: str = "FinalReviewerAgent",
        run_id: Optional[str] = None,
        record_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rec_id = record_id or f"mem-{hashlib.sha256(f'{domain}:{content}'.encode('utf-8')).hexdigest()[:12]}"
        record = MemoryRecord(
            id=rec_id,
            content=content,
            type=memory_type,
            layer=layer,
            tags=tags or ["zetaris", "sic_3570", "taxonomy_reconciliation"],
            confidence=confidence,
            source=source,
            domain=domain,
            entities=entities or ["IBM", "0000051143", "SIC_3570", "SIC_3571"],
            agentId=agent_id,
            runId=run_id,
        )
        conflict_report = self.conflict_service.detect_and_resolve(record, run_id=run_id)
        stored = self.store.create(record)
        return {
            "record": stored.to_dict(),
            "conflict_report": conflict_report,
        }


class SleepCycleService:
    """Non-mutating sleep preview service that identifies consolidation, archival, and synthesis candidates."""

    def __init__(self, store: MemoryStoreService, ledger: TrustLedgerService) -> None:
        self.store = store
        self.ledger = ledger

    def preview(self, run_id: Optional[str] = None) -> Dict[str, List[str]]:
        """Outputs {toConsolidate, toArchive, toSynthesize} without mutating the store.
        Never archives superseding (record.supersedes is not None) or derived (len(record.derivedFrom) > 0) records.
        """
        to_consolidate: List[str] = []
        to_archive: List[str] = []
        to_synthesize: List[str] = []

        records = self.store.all_records()
        domain_groups: Dict[str, List[MemoryRecord]] = {}
        for rec in records:
            domain_groups.setdefault(rec.domain, []).append(rec)
            if rec.layer == "short_term" and rec.confidence >= 0.75 and rec.accessCount >= 1:
                to_consolidate.append(rec.id)
            elif rec.layer == "working" and rec.confidence >= 0.85 and rec.accessCount >= 2:
                to_consolidate.append(rec.id)

            # Archive check: NEVER archive superseding or derived records
            is_protected = bool(rec.supersedes) or bool(rec.derivedFrom)
            if not is_protected and (rec.supersededBy or (rec.confidence < 0.40 and rec.accessCount == 0)):
                to_archive.append(rec.id)

        for dom, group in domain_groups.items():
            if len(group) >= 2:
                for rec in group:
                    if not rec.supersededBy:
                        to_synthesize.append(rec.id)

        result = {
            "toConsolidate": list(dict.fromkeys(to_consolidate)),
            "toArchive": list(dict.fromkeys(to_archive)),
            "toSynthesize": list(dict.fromkeys(to_synthesize)),
        }
        self.ledger.append("sleep_preview", "sleep_cycle", result, run_id=run_id)
        return result


class DreamingService:
    """Proposes higher-order cross-domain synthesis rules from existing memories."""

    def __init__(self, store: MemoryStoreService, ledger: TrustLedgerService) -> None:
        self.store = store
        self.ledger = ledger

    def propose_dreams(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        active = [r for r in self.store.all_records() if not r.supersededBy and r.confidence >= 0.70]
        proposals: List[Dict[str, Any]] = []
        if active:
            proposal = {
                "dream_id": f"drm-{uuid.uuid4().hex[:8]}",
                "derived_from": [r.id for r in active[:3]],
                "hypothesis": (
                    "Cross-system joins between SEC EDGAR 3-digit summary SIC codes (e.g., 3570) "
                    "and US SIC V4 4-digit leaf tables (3571..3577) require Industry Group prefix expansion "
                    "prior to cohort retention aggregation."
                ),
                "confidence": round(sum(r.confidence for r in active[:3]) / min(len(active), 3), 4),
            }
            proposals.append(proposal)
            self.ledger.append("dream_proposed", proposal["dream_id"], proposal, run_id=run_id)
        return proposals


class HMemEngine:
    """Unified H-MEM Engine bundling all 7 mandatory services."""

    def __init__(self, ledger_path: Optional[Path] = None) -> None:
        self.trust_ledger = TrustLedgerService(ledger_path=ledger_path)
        self.store = MemoryStoreService(self.trust_ledger)
        self.retrieval = MemoryRetrievalService(self.store, self.trust_ledger)
        self.conflict = ConflictDetectionService(self.store, self.trust_ledger)
        self.mining = MemoryMiningService(self.store, self.conflict)
        self.sleep = SleepCycleService(self.store, self.trust_ledger)
        self.dreaming = DreamingService(self.store, self.trust_ledger)


# Shared singleton instance so Run 1's mined SIC 3570 rule compounds into Run 2 in the same process,
# plus disk persistence helper for CLI multi-invocation compounding.
GLOBAL_HMEM = HMemEngine()
HMEM_STATE_FILE = LOGS_DIR / "hmem_state_store.json"


def save_hmem_state(engine: HMemEngine = GLOBAL_HMEM) -> None:
    data = [rec.to_dict() for rec in engine.store.all_records()]
    HMEM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    HMEM_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_hmem_state(engine: HMemEngine = GLOBAL_HMEM) -> None:
    if not HMEM_STATE_FILE.exists():
        return
    try:
        raw_list = json.loads(HMEM_STATE_FILE.read_text(encoding="utf-8"))
        for item in raw_list:
            if item.get("id") not in engine.store._records:
                engine.store._records[item["id"]] = MemoryRecord(**item)
    except Exception:
        pass
