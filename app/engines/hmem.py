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
import sys
import threading
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
        # FastAPI runs sync endpoints on a thread pool, so concurrent runs share this store.
        self.lock = threading.RLock()

    def _guard_trust(self, existing: MemoryRecord, incoming_conf: float, incoming_source: str, run_id: Optional[str]) -> None:
        if existing.confidence >= 0.85 and incoming_conf < 0.50:
            self.ledger.append(
                "update",
                existing.id,
                {
                    "status": "blocked_by_trust_protection",
                    "existing_confidence": existing.confidence,
                    "incoming_confidence": incoming_conf,
                    "incoming_source": incoming_source,
                },
                run_id=run_id,
            )
            raise LowTrustOverwriteError(
                f"Trust protection violation: low-trust source ({incoming_conf:.2f} < 0.50) "
                f"cannot overwrite high-trust record '{existing.id}' ({existing.confidence:.2f} >= 0.85)."
            )

    def create(self, record: MemoryRecord | Dict[str, Any]) -> MemoryRecord:
        rec = record if isinstance(record, MemoryRecord) else MemoryRecord(**record)
        with self.lock:
            if rec.id in self._records:
                self._guard_trust(self._records[rec.id], rec.confidence, rec.source, rec.runId)
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

    def reinforce(self, record: MemoryRecord) -> MemoryRecord:
        """Re-observing a known fact strengthens the stored record instead of resetting it.

        Keeps the higher confidence (so earned `helpful` feedback is not wiped), preserves access history,
        refreshes content/provenance from the new observation, and counts the re-confirmation.
        """
        with self.lock:
            existing = self._records.get(record.id)
            if existing is None:
                return self.create(record)
            self._guard_trust(existing, record.confidence, record.source, record.runId)
            prev_conf = existing.confidence
            existing.content = record.content
            existing.tags = list(dict.fromkeys(existing.tags + record.tags))
            existing.entities = list(dict.fromkeys(existing.entities + record.entities))[:10]
            existing.confidence = max(existing.confidence, record.confidence)
            existing.lastAccessed = _utc_now_iso()
            existing.runId = record.runId
            reinforcements = int(existing.provenance.get("reinforcements", 0)) + 1
            existing.provenance = {**existing.provenance, **record.provenance, "reinforcements": reinforcements}
            existing.embedding = compute_deterministic_embedding(
                f"{existing.content} {' '.join(existing.tags)} {' '.join(existing.entities)} {existing.domain}"
            )
        self.ledger.append(
            "update",
            record.id,
            {
                "status": "reinforced",
                "source": record.source,
                "previous_confidence": prev_conf,
                "confidence": existing.confidence,
                "reinforcements": reinforcements,
            },
            run_id=record.runId,
        )
        return existing

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
        with self.lock:
            return list(self._records.values())

    def active_rules(self, rule_type: str, min_confidence: float = 0.85) -> List[MemoryRecord]:
        """High-trust, non-superseded, non-flagged records carrying a structured rule of `rule_type`."""
        return [
            r
            for r in self.all_records()
            if (r.provenance.get("rule") or {}).get("rule_type") == rule_type
            and r.confidence >= min_confidence
            and not r.supersededBy
            and "needs_review" not in r.tags
        ]


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
        records = self.store.all_records()
        # Auto-extract: any query token that names an entity or tag already known to the store.
        known_entities = {e.lower() for r in records for e in r.entities}
        known_tags = {t.lower() for r in records for t in r.tags}
        q_entities |= q_tokens & known_entities
        q_tags |= q_tokens & known_tags

        scored_items: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for rec in records:
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
            elif rec.domain.lower() in q_tokens:
                domain_sim = 0.85
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
        rule: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store a mined fact; re-mining an existing id reinforces it rather than replacing it.

        `rule` is an optional machine-readable payload (stored under provenance.rule) so later runs can apply
        the learned behaviour directly instead of re-parsing prose.
        """
        rec_id = record_id or f"mem-{hashlib.sha256(f'{domain}:{content}'.encode('utf-8')).hexdigest()[:12]}"
        record = MemoryRecord(
            id=rec_id,
            content=content,
            type=memory_type,
            layer=layer,
            tags=tags or [],
            confidence=confidence,
            source=source,
            domain=domain,
            entities=entities or [],
            agentId=agent_id,
            runId=run_id,
        )
        if rule:
            record.provenance = {**record.provenance, "rule": rule}
        conflict_report = self.conflict_service.detect_and_resolve(record, run_id=run_id)
        existed = any(r.id == rec_id for r in self.store.all_records())
        stored = self.store.reinforce(record) if existed else self.store.create(record)
        return {
            "record": stored.to_dict(),
            "reinforced_existing": existed,
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
        """Propose generalisations across active memories that share a structured rule type or a domain.

        Proposals are never written back automatically; they are surfaced for review (non-mutating).
        """
        active = [r for r in self.store.all_records() if not r.supersededBy and r.confidence >= 0.70]
        groups: Dict[str, List[MemoryRecord]] = {}
        for rec in active:
            rule_type = (rec.provenance.get("rule") or {}).get("rule_type")
            groups.setdefault(rule_type or f"domain:{rec.domain}", []).append(rec)

        proposals: List[Dict[str, Any]] = []
        for key, members in sorted(groups.items()):
            if key.startswith("domain:") and len(members) < 2:
                continue
            rules = [m.provenance.get("rule") or {} for m in members]
            if key == "sic_summary_to_leaf":
                pairs = ", ".join(f"{r.get('summary_code')}->{r.get('leaf_code')}" for r in rules)
                hypothesis = (
                    f"Learned summary-to-leaf mappings ({pairs}) suggest a general USL rule: any EDGAR SIC that is a "
                    "summary header should be expanded through company_dns.sic_codes.parent_summary_code before "
                    "cohort aggregation, rather than waiting for fk_sic_check to fail."
                )
            else:
                shared = set.intersection(*(set(t.lower() for t in m.tags) for m in members)) if members else set()
                hypothesis = (
                    f"{len(members)} memories in '{key.split(':', 1)[-1]}' share tags {sorted(shared) or '[]'}; "
                    "candidate for consolidation into a single higher-order rule."
                )
            top = sorted(members, key=lambda m: m.confidence, reverse=True)[:3]
            proposal = {
                "dream_id": f"drm-{hashlib.sha256((key + ''.join(m.id for m in top)).encode()).hexdigest()[:8]}",
                "derived_from": [m.id for m in top],
                "hypothesis": hypothesis,
                "confidence": round(sum(m.confidence for m in top) / len(top), 4),
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


def save_hmem_state(engine: HMemEngine = GLOBAL_HMEM, path: Optional[Path] = None) -> None:
    target = path or HMEM_STATE_FILE
    with engine.store.lock:
        data = [rec.to_dict() for rec in engine.store.all_records()]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(target)  # atomic: a crash mid-write never leaves a truncated store


def load_hmem_state(engine: HMemEngine = GLOBAL_HMEM, path: Optional[Path] = None) -> int:
    """Merge persisted records into `engine`; returns how many were loaded. Corrupt entries are skipped, not fatal."""
    target = path or HMEM_STATE_FILE
    if not target.exists():
        return 0
    try:
        raw_list = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] H-MEM state at {target} unreadable ({type(exc).__name__}); starting empty.", file=sys.stderr)
        return 0
    loaded = 0
    with engine.store.lock:
        for item in raw_list if isinstance(raw_list, list) else []:
            try:
                if item.get("id") not in engine.store._records:
                    engine.store._records[item["id"]] = MemoryRecord(**item)
                    loaded += 1
            except (TypeError, ValueError, KeyError) as exc:
                print(f"[WARN] Skipping malformed H-MEM record: {type(exc).__name__}: {exc}", file=sys.stderr)
    return loaded
