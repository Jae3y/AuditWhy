"""Meterless Nightshift Runtime 1.4 & NVIDIA NIM Provider Diagnostics Script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.meterless_provider import MeterlessProviderClient, normalize_base_url, plan_route
from app.config import mask_secret, settings
from app.llm import LLMRouter


def main() -> None:
    print("=" * 72)
    print("AuditWhy — Provider Contract v4 & Nemotron Routing Diagnostic")
    print("=" * 72)

    normalized_url = normalize_base_url(settings.meterless_base_url)
    print(f"Configured Meterless Base URL : {normalized_url}")
    print(f"Meterless API Key             : {mask_secret(settings.meterless_api_key) or '(not set)'}")
    print(f"NVIDIA NIM API Key            : {mask_secret(settings.nvidia_api_key) or '(not set)'}")
    print(f"Sample Mode Active            : {settings.sample_mode}")

    autodetect = MeterlessProviderClient.auto_detect_runtime(timeout=0.4)
    print("\n[Auto-Discovery Probe (Ports 8088, 8090, 8787, 11434)]")
    print(json.dumps(autodetect, indent=2))

    print("\n[Task Route Matrix]")
    for task in ("plan", "retrieve", "evaluate", "analyze", "synthesize"):
        print(f"  {task:<12} -> {json.dumps(plan_route(task))}")

    router = LLMRouter()
    print("\n[Active Provider Selection]")
    print(json.dumps(router.get_provider_status(), indent=2))
    print("=" * 72)


if __name__ == "__main__":
    main()
