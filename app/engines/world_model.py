"""Meterless Engine 3: World Model Shared Grounded State.

Implements:
- WorldStoreService
- IngestPipelineService (6-stage idempotent pipeline: extract -> normalize -> resolve -> validate -> append -> project)
- ContextBuilderService
- Deterministic stable IDs:
  * Name-keyed: 'ent_' + sha256(f'{entity_type.lower()}:{normalized_name.lower()}').hexdigest()[:16]
  * Externally keyed: '{type}:{system}:{externalId}'
- Append-only event log at logs/world_model_events.jsonl
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import LOGS_DIR, redact_sensitive_text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_entity_name(name: str) -> str:
    """Normalize whitespace and punctuation for deterministic entity resolution."""
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    return cleaned.lower()


def compute_entity_id(
    entity_type: str,
    name: str = "",
    *,
    system: Optional[str] = None,
    external_id: Optional[str] = None,
) -> str:
    """Compute deterministic stable entity ID per World Model specification."""
    etype = (entity_type or "entity").strip().lower()
    if system and external_id:
        return f"{etype}:{system.strip().lower()}:{str(external_id).strip()}"
    norm_name = normalize_entity_name(name)
    digest = hashlib.sha256(f"{etype}:{norm_name}".encode("utf-8")).hexdigest()[:16]
    return f"ent_{digest}"


def _canonical_hash(payload: Dict[str, Any]) -> str:
    stable = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


class WorldStoreService:
    """Stores projected entities, relationships, observations, and append-only event log."""

    def __init__(self, events_path: Optional[Path] = None) -> None:
        self.events_path = events_path or (LOGS_DIR / "world_model_events.jsonl")
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.observations_by_hash: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []

    def append_event(self, event: Dict[str, Any]) -> None:
        self.events.append(event)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(redact_sensitive_text(json.dumps(event, ensure_ascii=False)) + "\n")

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        return self.entities.get(entity_id)

    def all_entities(self) -> List[Dict[str, Any]]:
        return list(self.entities.values())


class IngestPipelineService:
    """Executes the 6-stage idempotent pipeline:
    extract -> normalize -> resolve -> validate -> append -> project.
    Ingesting the exact same observations twice produces diff_count == 0.
    """

    def __init__(self, store: WorldStoreService) -> None:
        self.store = store

    def ingest(
        self,
        observations: List[Dict[str, Any]],
        *,
        source: str = "zetaris",
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Stage 1: extract
        extracted = [dict(obs) for obs in (observations or []) if isinstance(obs, dict)]

        # Stage 2: normalize
        normalized: List[Dict[str, Any]] = []
        for item in extracted:
            etype = str(item.get("entity_type") or item.get("type") or "enterprise_account").strip().lower()
            name = str(
                item.get("name")
                or item.get("company_name")
                or item.get("account_name")
                or item.get("description")
                or item.get("id")
                or "unknown"
            ).strip()
            system = item.get("system")
            ext_id = item.get("external_id")
            attrs = {
                k: v
                for k, v in item.items()
                if k not in ("entity_type", "type", "system", "external_id", "timestamp")
            }
            normalized.append(
                {
                    "entity_type": etype,
                    "name": name,
                    "normalized_name": normalize_entity_name(name),
                    "system": system,
                    "external_id": ext_id,
                    "attributes": attrs,
                    "source": source,
                }
            )

        # Stage 3: resolve (assign deterministic IDs)
        resolved: List[Dict[str, Any]] = []
        for norm in normalized:
            eid = compute_entity_id(
                norm["entity_type"],
                norm["name"],
                system=norm["system"],
                external_id=norm["external_id"],
            )
            resolved.append({**norm, "entity_id": eid})

        # Stage 4: validate
        validated: List[Dict[str, Any]] = []
        for res in resolved:
            if not res["entity_id"] or not res["normalized_name"]:
                continue
            obs_fingerprint = _canonical_hash(
                {
                    "entity_id": res["entity_id"],
                    "entity_type": res["entity_type"],
                    "normalized_name": res["normalized_name"],
                    "attributes": res["attributes"],
                    "source": res["source"],
                }
            )
            validated.append({**res, "observation_hash": obs_fingerprint})

        # Stage 5 & 6: append & project (idempotent diff check)
        diffs: List[Dict[str, Any]] = []
        for val in validated:
            obs_hash = val["observation_hash"]
            eid = val["entity_id"]
            if obs_hash in self.store.observations_by_hash:
                # Exact duplicate observation -> idempotent no-op
                continue

            existing_entity = self.store.entities.get(eid)
            prev_attrs = dict(existing_entity["attributes"]) if existing_entity else {}
            merged_attrs = {**prev_attrs, **val["attributes"]}

            if existing_entity and merged_attrs == prev_attrs:
                self.store.observations_by_hash[obs_hash] = val
                continue

            event = {
                "event_id": f"wme-{uuid.uuid4().hex[:10]}",
                "timestamp": _utc_now_iso(),
                "run_id": run_id or "system",
                "stage": "append_and_project",
                "entity_id": eid,
                "entity_type": val["entity_type"],
                "name": val["name"],
                "source": val["source"],
                "observation_hash": obs_hash,
                "attribute_delta": {
                    k: v for k, v in merged_attrs.items() if prev_attrs.get(k) != v
                },
            }
            self.store.observations_by_hash[obs_hash] = val
            self.store.entities[eid] = {
                "entity_id": eid,
                "entity_type": val["entity_type"],
                "name": val["name"],
                "normalized_name": val["normalized_name"],
                "attributes": merged_attrs,
                "sources": sorted(
                    list(set((existing_entity.get("sources", []) if existing_entity else []) + [val["source"]]))
                ),
                "updated_at": event["timestamp"],
            }
            self.store.append_event(event)
            diffs.append(event)

        return {
            "pipeline_stages": ["extract", "normalize", "resolve", "validate", "append", "project"],
            "input_count": len(observations or []),
            "validated_count": len(validated),
            "diff_count": len(diffs),
            "diffs": diffs,
            "total_entities": len(self.store.entities),
        }


class ContextBuilderService:
    """Projects grounded World Model state into structured agent context snapshots."""

    def __init__(self, store: WorldStoreService) -> None:
        self.store = store

    def build_context(self, entity_type: Optional[str] = None) -> Dict[str, Any]:
        entities = self.store.all_entities()
        if entity_type:
            entities = [e for e in entities if e["entity_type"] == entity_type.lower()]
        by_type: Dict[str, int] = {}
        for e in self.store.all_entities():
            by_type[e["entity_type"]] = by_type.get(e["entity_type"], 0) + 1
        return {
            "total_entities": len(self.store.all_entities()),
            "counts_by_type": by_type,
            "entities": entities,
        }


class WorldModelEngine:
    """Unified World Model Engine bundling Store, IngestPipeline, and ContextBuilder."""

    def __init__(self, events_path: Optional[Path] = None) -> None:
        self.store = WorldStoreService(events_path=events_path)
        self.pipeline = IngestPipelineService(self.store)
        self.context = ContextBuilderService(self.store)
