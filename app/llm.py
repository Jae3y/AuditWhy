"""Unified LLM Router & Deterministic Structured Reasoning Fallback.

Priority Chain:
1. Meterless Nightshift Runtime 1.4 (if online & authorized)
2. NVIDIA NIM OpenAI-compatible API (nvidia/nemotron-3.5-lightning-30b-a3b,
   nvidia/nemotron-3-ultra-550b-a55b with enable_thinking: True,
   nvidia/nemotron-3.5-super-vl-120b-a12b) with 2s/4s/8s exponential backoff on HTTP 429
3. Deterministic fallback: the calling agent supplies `fallback_text` that it has already derived from the
   live SQL rows / World Model state, so offline output is still grounded in the data actually retrieved.

Decisions (evidence gates, DQ verdicts, claim blocking) are always computed in code by the agents; the LLM only
contributes narrative, so a judge running SAMPLE_MODE=true sees the exact same verdicts as a live run.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
import httpx

from app.adapters.meterless_provider import (
    MeterlessProviderClient,
    is_valid_meterless_token,
    plan_route,
)
from app.config import redact_sensitive_text, settings

DETERMINISTIC_PROVIDER = "deterministic_structured_generator"


class LLMRouter:
    """Routes agent reasoning requests across Meterless Nightshift, NVIDIA Nemotron NIM, and Deterministic Synthesis."""

    def __init__(self, sample_mode: Optional[bool] = None) -> None:
        self.sample_mode = sample_mode if sample_mode is not None else settings.sample_mode
        self.meterless = MeterlessProviderClient()
        self.call_history: List[Dict[str, Any]] = []
        self._probe_cache: Optional[Dict[str, Any]] = None

    def _meterless_probe(self) -> Dict[str, Any]:
        """Probe /v1/models once per router (i.e. once per run) instead of before every completion."""
        if self._probe_cache is None:
            self._probe_cache = self.meterless.probe_status(timeout=1.5)
        return self._probe_cache

    def get_provider_status(self) -> Dict[str, Any]:
        if self.sample_mode:
            return {
                "active_provider": DETERMINISTIC_PROVIDER,
                "meterless_status": "offline_sample_mode",
                "nvidia_nim_configured": bool(settings.nvidia_api_key),
            }
        probe = self._meterless_probe() if is_valid_meterless_token(self.meterless.api_key) else {"status": "unconfigured"}
        return {
            "active_provider": (
                "meterless_nightshift_1.4"
                if probe["status"] == "online"
                else ("nvidia_nim_nemotron" if settings.nvidia_api_key else DETERMINISTIC_PROVIDER)
            ),
            "meterless_status": probe["status"],
            "nvidia_nim_configured": bool(settings.nvidia_api_key),
        }

    def generate(
        self,
        *,
        task: str,
        system_prompt: str,
        user_prompt: str,
        fallback_text: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> Dict[str, Any]:
        route = plan_route(task)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        errors: List[str] = []

        # Priority 1: Meterless Nightshift Runtime 1.4
        if not self.sample_mode and is_valid_meterless_token(self.meterless.api_key):
            if self._meterless_probe()["status"] == "online":
                try:
                    res = self.meterless.chat_completion(
                        messages=messages,
                        task=task,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    if res.get("content", "").strip():
                        self._record_call(task, res["provider"], res["model"], route, live=True, served_by=res.get("served_by", ""))
                        return {**res, "is_live": True}
                    errors.append("meterless: empty completion")
                except Exception as exc:  # noqa: BLE001 - any provider failure falls through to the next tier
                    errors.append(f"meterless: {type(exc).__name__}")

        # Priority 2: NVIDIA NIM OpenAI-compatible Endpoint (with 2s/4s/8s backoff on 429)
        if not self.sample_mode and settings.nvidia_api_key:
            try:
                res = self._call_nvidia_nim(messages, route, temperature, max_tokens)
                if res.get("content", "").strip():
                    self._record_call(task, res["provider"], res["model"], route, live=True)
                    return {**res, "is_live": True}
                errors.append("nvidia_nim: empty completion")
            except Exception as exc:  # noqa: BLE001
                code = getattr(getattr(exc, "response", None), "status_code", None)
                errors.append(f"nvidia_nim: {type(exc).__name__}" + (f" HTTP {code}" if code else ""))

        # Priority 3: data-grounded deterministic text supplied by the calling agent
        provider_label = f"{DETERMINISTIC_PROVIDER} (nemotron-route-simulated)"
        self._record_call(task, provider_label, route["nvidia_model"], route, live=False, errors=errors)
        return {
            "provider": provider_label,
            "model": route["nvidia_model"],
            "meterless_class": route["meterless_model"],
            "enable_thinking": route["enable_thinking"],
            "content": fallback_text,
            "is_live": False,
        }

    def _call_nvidia_nim(
        self,
        messages: List[Dict[str, str]],
        route: Dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        url = f"{settings.nvidia_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.nvidia_api_key}",
            "Content-Type": "application/json",
        }
        thinking = bool(route.get("enable_thinking"))
        payload: Dict[str, Any] = {
            "model": route["nvidia_model"],
            "messages": messages,
            "temperature": temperature,
            # Hidden reasoning tokens count against max_tokens, so the judge lane gets extra room.
            "max_tokens": max_tokens * 4 if thinking else max_tokens,
            # Raw HTTP: chat_template_kwargs is a top-level field (`extra_body` only exists in the OpenAI SDK).
            # Nemotron 3.5 reasons by default and can spend a whole budget on hidden reasoning, so every lane
            # states its mode explicitly.
            "chat_template_kwargs": {"enable_thinking": thinking},
        }

        backoff_delays = [2.0, 4.0, 8.0]
        with httpx.Client(timeout=60.0) as client:
            for attempt in range(len(backoff_delays) + 1):
                resp = client.post(url, headers=headers, json=payload)
                # Rate limits and transient gateway errors on the hosted endpoints are retried with backoff.
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(backoff_delays):
                    time.sleep(backoff_delays[attempt])
                    continue
                resp.raise_for_status()
                message = (resp.json().get("choices") or [{}])[0].get("message", {})
                return {
                    "provider": "nvidia_nim",
                    "model": route["nvidia_model"],
                    "meterless_class": route["meterless_model"],
                    "enable_thinking": route["enable_thinking"],
                    "content": message.get("content") or "",
                }
        raise RuntimeError("NVIDIA NIM rate limit exceeded after 2s/4s/8s backoff.")

    def _record_call(
        self,
        task: str,
        provider: str,
        model: str,
        route: Dict[str, Any],
        *,
        live: bool,
        errors: Optional[List[str]] = None,
        served_by: str = "",
    ) -> None:
        entry: Dict[str, Any] = {
            "task": task,
            "provider": provider,
            "model": model,
            "meterless_class": route["meterless_model"],
            "disposition": route["disposition"],
            "enable_thinking": route["enable_thinking"],
            "live": live,
        }
        if served_by:
            entry["served_by"] = served_by
        if errors:
            entry["fallback_reasons"] = [redact_sensitive_text(e) for e in errors]
        self.call_history.append(entry)
