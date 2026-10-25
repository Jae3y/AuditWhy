"""Meterless Engine 2: Markovian Bounded-Context Reasoning Engine.

Implements:
- Strict ChunkConfig validation raising MarkovianConfigError on out-of-bounds parameters
- H-MEM Memory Seam (chunk 0 receives memoryContext + Task; continuation chunks receive ~200-char goal + carryover)
- [STATE_CHECKPOINT] and [TASK_COMPLETE] marker protocol & stripping
- 4-Step Carryover Compression Cascade (Explicit post-marker >=24 chars -> LLM compressor -> Regex heuristic -> Tail truncation)
- Two token ledgers persisted to logs/markovian_telemetry.jsonl:
  * measured: what each chunk actually sent (bounded prompt) vs. what a naive full-transcript chain would have sent
  * spec-projected: AGENTS.md formula that assumes every prior chunk filled `chunkSize`
  The measured ledger is what we report as `efficiencyPercent`; the projection is kept for conformance only.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import LOGS_DIR, redact_sensitive_text


class MarkovianConfigError(ValueError):
    """Typed error raised when ChunkConfig parameters violate AGENTS.md bounds."""


@dataclass
class ChunkConfig:
    chunkSize: int = 8000
    maxChunks: int = 12
    carryoverTokens: int = 256
    overlapTokens: int = 128

    def __post_init__(self) -> None:
        if not isinstance(self.chunkSize, int) or not (1000 <= self.chunkSize <= 128000):
            raise MarkovianConfigError(
                f"Invalid chunkSize={self.chunkSize}; must be in [1000, 128000]."
            )
        if not isinstance(self.maxChunks, int) or not (1 <= self.maxChunks <= 32):
            raise MarkovianConfigError(
                f"Invalid maxChunks={self.maxChunks}; must be in [1, 32]."
            )
        if not isinstance(self.carryoverTokens, int) or not (128 <= self.carryoverTokens <= 32768):
            raise MarkovianConfigError(
                f"Invalid carryoverTokens={self.carryoverTokens}; must be in [128, 32768]."
            )
        if not isinstance(self.overlapTokens, int) or not (0 <= self.overlapTokens <= 4096):
            raise MarkovianConfigError(
                f"Invalid overlapTokens={self.overlapTokens}; must be in [0, 4096]."
            )


def strip_markovian_markers(text: str) -> str:
    """Strip [STATE_CHECKPOINT] and [TASK_COMPLETE] protocol markers and any trailing state block."""
    if not text:
        return ""
    cleaned = text
    if "[STATE_CHECKPOINT]" in cleaned:
        cleaned = cleaned.split("[STATE_CHECKPOINT]", 1)[0]
    cleaned = cleaned.replace("[TASK_COMPLETE]", "")
    return cleaned.strip()


def compress_carryover(
    raw_chunk_output: str,
    carryover_tokens: int = 256,
    llm_compressor: Optional[Callable[[str], str]] = None,
) -> Dict[str, str]:
    """Execute the 4-step carryover compression cascade:
    (1) Explicit state text after [STATE_CHECKPOINT] marker (>= 24 chars)
    (2) Injected LLM compressor callable
    (3) Heuristic regex extraction (Therefore|Key insight|Status|Resolved|COMPLETED|DECISIONS)
    (4) Tail truncation
    """
    max_chars = max(128, carryover_tokens * 4)

    # Step 1: Explicit state text after [STATE_CHECKPOINT]
    if "[STATE_CHECKPOINT]" in raw_chunk_output:
        after_marker = raw_chunk_output.split("[STATE_CHECKPOINT]", 1)[1].strip()
        after_marker = after_marker.replace("[TASK_COMPLETE]", "").strip()
        if len(after_marker) >= 24:
            return {
                "method": "step1_explicit_marker_state",
                "carryover": after_marker[:max_chars],
            }

    # Step 2: Injected LLM compressor
    if llm_compressor is not None:
        try:
            compressed = llm_compressor(raw_chunk_output)
            if compressed and len(compressed.strip()) >= 16:
                return {
                    "method": "step2_llm_compressor",
                    "carryover": compressed.strip()[:max_chars],
                }
        except Exception:
            pass

    # Step 3: Heuristic regex extraction
    clean_body = strip_markovian_markers(raw_chunk_output)
    heuristic_pattern = re.compile(
        r"((?:Therefore|Key insight|Status|Resolved|COMPLETED|REMAINING|DECISIONS|CONTEXT)[^\n.!?]*(?:[.!?]|\n|$))",
        re.IGNORECASE,
    )
    matches = [m.strip() for m in heuristic_pattern.findall(clean_body) if m.strip()]
    if matches:
        joined = " | ".join(matches)
        return {
            "method": "step3_heuristic_regex",
            "carryover": (
                f"COMPLETED: {joined[: max_chars // 2]}\n"
                f"REMAINING: Verify residual hypotheses\n"
                f"DECISIONS: {matches[-1][:160]}\n"
                f"CONTEXT: Bounded carryover active"
            )[:max_chars],
        }

    # Step 4: Tail truncation
    tail = clean_body[-max_chars:] if len(clean_body) > max_chars else clean_body
    return {
        "method": "step4_tail_truncation",
        "carryover": (
            f"COMPLETED: Chunk segment processed\n"
            f"REMAINING: Final synthesis\n"
            f"DECISIONS: Continue bounded chain\n"
            f"CONTEXT: {tail}"
        )[:max_chars],
    }


def compute_chunk_token_math(
    chunk_index_1based: int,
    chunk_tokens: int,
    config: ChunkConfig,
) -> Dict[str, float]:
    """Compute exact token cost and savings for 1-indexed chunk i:
    standardChunkCost = (i - 1) * chunkSize + chunkTokens
    markovianChunkCost = carryoverTokens + chunkTokens
    """
    if chunk_index_1based < 1:
        raise ValueError("chunk_index_1based must be >= 1")
    standard_cost = (chunk_index_1based - 1) * config.chunkSize + chunk_tokens
    markovian_cost = config.carryoverTokens + chunk_tokens
    saved = standard_cost - markovian_cost
    return {
        "chunk_index": chunk_index_1based,
        "chunk_tokens": chunk_tokens,
        "standardChunkCost": standard_cost,
        "markovianChunkCost": markovian_cost,
        "chunkTokensSaved": saved,
    }


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 characters per token)."""
    return max(1, math.ceil(len(text or "") / 4))


class MarkovianEngine:
    """Executes bounded-context multi-chunk reasoning with H-MEM seam and telemetry logging."""

    def __init__(
        self,
        config: Optional[ChunkConfig] = None,
        telemetry_path: Optional[Path] = None,
    ) -> None:
        self.config = config or ChunkConfig()
        self.telemetry_path = telemetry_path or (LOGS_DIR / "markovian_telemetry.jsonl")
        self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def build_chunk_prompt(
        chunk_idx_0based: int,
        prompt: str,
        memory_context: str = "",
        carryover_state: str = "",
    ) -> str:
        """Enforce the H-MEM Memory Seam:
        - Chunk 0 receives memoryContext + "\\n\\nTask: " + prompt
        - Continuation chunks (i > 0) receive ONLY ~200-char goal summary + compressed carryover.
        """
        if chunk_idx_0based == 0:
            if memory_context and memory_context.strip():
                return f"{memory_context.strip()}\n\nTask: {prompt}"
            return f"Task: {prompt}"
        goal_summary = prompt.strip()[:200]
        return f"Goal Summary: {goal_summary}\n\nCarryover State:\n{carryover_state.strip()}"

    def execute_reasoning_chain(
        self,
        *,
        prompt: str,
        memory_context: str = "",
        chunk_Step_generator: Optional[Callable[[int, str, bool], str]] = None,
        llm_compressor: Optional[Callable[[str], str]] = None,
        warm_mode: bool = False,
        run_id: Optional[str] = None,
        planned_chunks: Optional[int] = None,
        baseline_chunks: int = 4,
        step_labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run bounded-context chunk reasoning and persist telemetry.

        `planned_chunks` is normally supplied by the caller from its actual work queue (e.g. the analyst skips the
        taxonomy-reconciliation stage when H-MEM already resolved it). Without it, the legacy default of 4 cold /
        2 warm chunks is used so the engine stays usable standalone.
        """
        if planned_chunks is None:
            planned_chunks = 2 if warm_mode else 4
        planned_chunks = max(1, min(self.config.maxChunks, int(planned_chunks)))

        chunk_records: List[Dict[str, Any]] = []
        cleaned_outputs: List[str] = []
        carryover_text = ""
        # Naive chain: every chunk re-sends the task, the memory, and the full transcript so far.
        naive_transcript = self.build_chunk_prompt(0, prompt=prompt, memory_context=memory_context)

        naive_cumulative = 0
        markovian_cumulative = 0
        spec_standard_cumulative = 0
        spec_markovian_cumulative = 0
        efficiency_curve: List[Dict[str, Any]] = []

        for idx0 in range(planned_chunks):
            idx1 = idx0 + 1
            is_final = idx1 == planned_chunks
            chunk_input = self.build_chunk_prompt(
                idx0,
                prompt=prompt,
                memory_context=memory_context if idx0 == 0 else "",
                carryover_state=carryover_text,
            )

            if chunk_Step_generator is not None:
                raw_out = chunk_Step_generator(idx0, chunk_input, is_final)
            else:
                raw_out = self._default_chunk_step(idx0, is_final, warm_mode)

            # Enforce protocol markers; a missing checkpoint body falls through the compression cascade.
            if not is_final and "[STATE_CHECKPOINT]" not in raw_out and "[TASK_COMPLETE]" not in raw_out:
                raw_out = raw_out.rstrip() + "\n[STATE_CHECKPOINT]"
            elif is_final and "[TASK_COMPLETE]" not in raw_out:
                raw_out = raw_out.rstrip() + "\n[TASK_COMPLETE]"

            cleaned = strip_markovian_markers(raw_out)
            cleaned_outputs.append(cleaned)

            comp_info = (
                compress_carryover(raw_out, self.config.carryoverTokens, llm_compressor)
                if not is_final
                else {"method": "final_chunk_no_carryover", "carryover": ""}
            )
            carryover_text = comp_info["carryover"]

            # Measured ledger
            output_tokens = estimate_tokens(raw_out)
            markovian_cost = estimate_tokens(chunk_input) + output_tokens
            naive_cost = estimate_tokens(naive_transcript) + output_tokens
            naive_transcript = f"{naive_transcript}\n\n{raw_out}"
            naive_cumulative += naive_cost
            markovian_cumulative += markovian_cost
            cum_saved = naive_cumulative - markovian_cumulative
            cum_eff = round((cum_saved / naive_cumulative) * 100.0, 2) if naive_cumulative > 0 else 0.0

            # Spec-projected ledger (AGENTS.md formula, assumes full-size prior chunks)
            math_row = compute_chunk_token_math(idx1, markovian_cost, self.config)
            spec_standard_cumulative += int(math_row["standardChunkCost"])
            spec_markovian_cumulative += int(math_row["markovianChunkCost"])
            projected_eff = (
                round(((spec_standard_cumulative - spec_markovian_cumulative) / spec_standard_cumulative) * 100.0, 2)
                if spec_standard_cumulative > 0
                else 0.0
            )

            efficiency_curve.append(
                {
                    "chunk": idx1,
                    "standardCumulative": naive_cumulative,
                    "markovianCumulative": markovian_cumulative,
                    "actualEfficiencyPercent": cum_eff,
                    "projectedEfficiencyPercent": projected_eff,
                }
            )

            chunk_records.append(
                {
                    "chunk_index": idx1,
                    "stage": step_labels[idx0] if step_labels and idx0 < len(step_labels) else f"chunk_{idx1}",
                    "has_memory_seam": idx0 == 0 and bool(memory_context.strip()),
                    "marker": "[TASK_COMPLETE]" if is_final else "[STATE_CHECKPOINT]",
                    "compression_method": comp_info["method"],
                    "carryover_preview": carryover_text[:160],
                    "measured_tokens": {"bounded_prompt_and_output": markovian_cost, "naive_full_history": naive_cost},
                    "token_math": math_row,
                    "cleaned_output": cleaned,
                }
            )

        total_saved = naive_cumulative - markovian_cumulative
        efficiency_pct = round((total_saved / naive_cumulative) * 100.0, 2) if naive_cumulative > 0 else 0.0
        spec_eff = (
            round(((spec_standard_cumulative - spec_markovian_cumulative) / spec_standard_cumulative) * 100.0, 2)
            if spec_standard_cumulative > 0
            else 0.0
        )

        telemetry_record = {
            "telemetry_id": f"mrk-{uuid.uuid4().hex[:10]}",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "run_id": run_id or "system",
            "warm_mode": warm_mode,
            "chunks_executed": planned_chunks,
            "cold_baseline_chunks": baseline_chunks,
            "cold_vs_warm_chunk_delta": max(0, baseline_chunks - planned_chunks),
            "config": asdict(self.config),
            "standardCumulative": naive_cumulative,
            "totalTokensUsed": markovian_cumulative,
            "totalTokensSaved": total_saved,
            "efficiencyPercent": efficiency_pct,
            "efficiencyBasis": "measured: bounded chunk prompts vs. naive full-transcript chain",
            "specProjectedStandardCumulative": spec_standard_cumulative,
            "specProjectedEfficiencyPercent": spec_eff,
            "efficiencyCurve": efficiency_curve,
            "chunks": chunk_records,
            "final_output": "\n\n".join(cleaned_outputs),
        }

        with self.telemetry_path.open("a", encoding="utf-8") as f:
            f.write(redact_sensitive_text(json.dumps(telemetry_record, ensure_ascii=False)) + "\n")

        return telemetry_record

    @staticmethod
    def _default_chunk_step(idx0: int, is_final: bool, warm_mode: bool) -> str:
        """Domain-neutral placeholder used only when no step generator is supplied (standalone/conformance use)."""
        seam = " Memory seam active at chunk 0." if warm_mode and idx0 == 0 else ""
        if is_final:
            return f"Chunk {idx0 + 1}: Synthesized findings from carried-over state.{seam}\n[TASK_COMPLETE]"
        return (
            f"Chunk {idx0 + 1}: Processed segment {idx0 + 1}.{seam}\n"
            "[STATE_CHECKPOINT]\n"
            f"COMPLETED: Segment {idx0 + 1} processed. REMAINING: Continue bounded chain. "
            "DECISIONS: Carry only verified state forward."
        )
