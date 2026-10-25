"""Append-only JSONL trace logger with secret redaction and stdout telemetry."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from app.config import LOGS_DIR, redact_sensitive_text, safe_identifier, settings

# Statuses that still reach stdout when LOG_LEVEL is WARNING/ERROR (the JSONL files always get every event).
_NOTABLE_STATUSES = {"needs_revision", "partial_success", "fallback", "blocked"}
from app.schemas import SponsorTech, TraceEvent, TraceStatus


class TraceLogger:
    """Writes structured trace events to logs/agent_trace.jsonl, logs/trace-{run_id}.jsonl, and stdout."""

    def __init__(self, run_id: str, logs_dir: Path | None = None) -> None:
        self.run_id = run_id
        self.logs_dir = logs_dir or LOGS_DIR
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.global_trace_path = self.logs_dir / "agent_trace.jsonl"
        self.run_trace_path = self.logs_dir / f"trace-{safe_identifier(run_id)}.jsonl"
        self.events: List[TraceEvent] = []

    def log(
        self,
        *,
        agent_name: str,
        action: str,
        input_summary: str,
        output_summary: str,
        target_agent: str,
        confidence: float,
        retry_count: int = 0,
        status: TraceStatus = "success",
        sponsor_tech: SponsorTech = "meterless_markovian",
    ) -> TraceEvent:
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        clamped_conf = max(0.0, min(1.0, round(float(confidence), 4)))
        safe_input = redact_sensitive_text(input_summary)
        safe_output = redact_sensitive_text(output_summary)

        event = TraceEvent(
            timestamp=now_iso,
            run_id=self.run_id,
            trace_id=f"trc-{uuid.uuid4().hex[:12]}",
            agent_name=agent_name,
            action=action,
            input_summary=safe_input,
            output_summary=safe_output,
            target_agent=target_agent,
            confidence=clamped_conf,
            retry_count=max(0, int(retry_count)),
            status=status,
            sponsor_tech=sponsor_tech,
        )
        self.events.append(event)

        serialized = json.dumps(event.model_dump(), ensure_ascii=False)
        with self.global_trace_path.open("a", encoding="utf-8") as gf:
            gf.write(serialized + "\n")
        with self.run_trace_path.open("a", encoding="utf-8") as rf:
            rf.write(serialized + "\n")

        level = settings.log_level
        notable = status in _NOTABLE_STATUSES
        if level in ("DEBUG", "INFO") or (level == "WARNING" and notable) or (level == "ERROR" and status in ("fallback", "blocked")):
            print(
                f"[{'WARN' if notable else 'INFO'}] run_id={self.run_id} agent={agent_name} action={action} "
                f"status={status} target={target_agent} confidence={clamped_conf:.2f} "
                f"retry={retry_count} sponsor={sponsor_tech}"
            )
        return event
