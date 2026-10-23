"""Configuration and secret-redaction utilities for AuditWhy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SAMPLE_DATA_DIR = DATA_DIR / "sample"
LOGS_DIR = ROOT_DIR / "logs"
UI_DIR = Path(__file__).resolve().parent / "ui"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"([#&?]k=)[^&\s#]+", re.IGNORECASE),
    re.compile(r"(nvapi-[A-Za-z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"(ml_live_[A-Za-z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"(sk-[A-Za-z0-9_\-]{12,})", re.IGNORECASE),
]


def mask_secret(value: str | None) -> str:
    """Mask an entire credential value as [REDACTED]."""
    if not value:
        return ""
    return "[REDACTED]"


def redact_sensitive_text(text: str) -> str:
    """Redact any embedded tokens, connect-link keys, or bearer credentials from text."""
    if not text:
        return text
    sanitized = str(text)
    for idx, pattern in enumerate(_SECRET_PATTERNS):
        if idx in (0, 1):
            sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
        else:
            sanitized = pattern.sub("[REDACTED]", sanitized)
    # Also redact any configured non-empty env key values if they appear verbatim
    for env_key in ("ZETARIS_API_KEY", "METERLESS_API_KEY", "NVIDIA_API_KEY", "MODEL_API_KEY"):
        val = os.getenv(env_key, "").strip()
        if val and len(val) >= 6 and val in sanitized:
            sanitized = sanitized.replace(val, "[REDACTED]")
    return sanitized


class Settings:
    """Runtime settings dynamically resolved from environment variables."""

    @property
    def port(self) -> int:
        return int(os.getenv("PORT", "8000"))

    @property
    def sample_mode(self) -> bool:
        raw = os.getenv("SAMPLE_MODE", "true").strip().lower()
        return raw in ("1", "true", "yes", "on")

    @property
    def zetaris_rest_url(self) -> str:
        url = os.getenv("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0").strip()
        # Enforce Zetaris port 8888 rule (never 8889)
        if ":8889" in url:
            url = url.replace(":8889", ":8888")
        return url or "http://localhost:8888/api/v1.0"

    @property
    def zetaris_api_key(self) -> str:
        return os.getenv("ZETARIS_API_KEY", "").strip()

    @property
    def zetaris_org_id(self) -> int:
        try:
            return int(os.getenv("ZETARIS_ORG_ID", "1"))
        except ValueError:
            return 1

    @property
    def meterless_base_url(self) -> str:
        return os.getenv("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1").strip()

    @property
    def meterless_api_key(self) -> str:
        return os.getenv("METERLESS_API_KEY", "").strip()

    @property
    def meterless_connect_link(self) -> str:
        return os.getenv("METERLESS_CONNECT_LINK", "").strip()

    @property
    def meterless_app_name(self) -> str:
        return os.getenv("METERLESS_APP_NAME", "auditwhy").strip() or "auditwhy"

    @property
    def nvidia_base_url(self) -> str:
        return os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()

    @property
    def nvidia_api_key(self) -> str:
        return (os.getenv("NVIDIA_API_KEY", "") or os.getenv("MODEL_API_KEY", "")).strip()

    @property
    def nvidia_fast_model(self) -> str:
        return os.getenv("NVIDIA_FAST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b").strip()

    @property
    def nvidia_deep_model(self) -> str:
        return os.getenv("NVIDIA_DEEP_MODEL", "nvidia/nemotron-3-ultra-550b-a55b").strip()

    @property
    def nvidia_synth_model(self) -> str:
        return os.getenv("NVIDIA_SYNTH_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b").strip()

    @property
    def scout_hmac_key(self) -> str:
        return os.getenv("SCOUT_HMAC_KEY", "auditwhy-scout-contract-v1").strip() or "auditwhy-scout-contract-v1"


settings = Settings()
