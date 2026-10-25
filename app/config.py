"""Configuration and secret-redaction utilities for AuditWhy."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SAMPLE_DATA_DIR = DATA_DIR / "sample"

# Public hosted demo (Vercel sets VERCEL=1). Zetaris and Nightshift run on the builder's own machine, so a hosted
# instance can only ever run sample mode; it also has a read-only filesystem apart from /tmp.
PUBLIC_DEMO = os.getenv("AUDITWHY_PUBLIC_DEMO", "").strip().lower() in ("1", "true", "yes", "on") or bool(os.getenv("VERCEL"))

# AUDITWHY_LOGS_DIR lets tests (and multi-tenant deployments) isolate traces, H-MEM state and ledgers.
_DEFAULT_LOGS = Path("/tmp/auditwhy-logs") if PUBLIC_DEMO else ROOT_DIR / "logs"
LOGS_DIR = Path(os.getenv("AUDITWHY_LOGS_DIR", "").strip() or _DEFAULT_LOGS).resolve()
UI_DIR = Path(__file__).resolve().parent / "ui"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Per-process signing key used when SCOUT_HMAC_KEY is unset, so contracts can never be forged
# from a publicly known default. Contracts are only verified inside the process that issued them.
_EPHEMERAL_SCOUT_KEY = secrets.token_hex(32)

_SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_identifier(value: str | None, fallback: str = "run", max_len: int = 80) -> str:
    """Reduce a caller-supplied id to [A-Za-z0-9_.-] so it is safe inside filenames and log keys.

    Leading dots are stripped so ids like '../../etc' can never traverse out of LOGS_DIR.
    """
    cleaned = _SAFE_ID_PATTERN.sub("-", str(value or "")).strip("-.")[:max_len]
    return cleaned or fallback

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
    for env_key in ("ZETARIS_API_KEY", "METERLESS_RUNTIME_TOKEN", "METERLESS_API_KEY", "NVIDIA_API_KEY", "MODEL_API_KEY"):
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
    def public_demo(self) -> bool:
        return PUBLIC_DEMO

    @property
    def sample_mode(self) -> bool:
        if PUBLIC_DEMO:
            return True
        raw = os.getenv("SAMPLE_MODE", "true").strip().lower()
        return raw in ("1", "true", "yes", "on")

    @property
    def zetaris_timeout_seconds(self) -> float:
        try:
            return max(1.0, float(os.getenv("SOURCE_TIMEOUT_SECONDS", "30")))
        except ValueError:
            return 30.0

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
        """Nightshift runtime token: METERLESS_RUNTIME_TOKEN (setup-guide name), METERLESS_API_KEY, or a token file.

        The runtime writes its token to <stateDir>/proxy-token; METERLESS_TOKEN_FILE can point straight at it.
        """
        token = (os.getenv("METERLESS_RUNTIME_TOKEN", "") or os.getenv("METERLESS_API_KEY", "")).strip()
        token_file = os.getenv("METERLESS_TOKEN_FILE", "").strip()
        if not token and token_file:
            try:
                token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        return token

    @property
    def meterless_model(self) -> str:
        """Optional pinned routing mode (e.g. a Nemotron-backed lane). Empty = route per task class."""
        return os.getenv("METERLESS_MODEL", "").strip()

    @property
    def meterless_connect_link(self) -> str:
        return os.getenv("METERLESS_CONNECT_LINK", "").strip()

    @property
    def meterless_app_name(self) -> str:
        return os.getenv("METERLESS_APP_NAME", "auditwhy").strip() or "auditwhy"

    # Direct Nemotron route (setup guide "Option B"): NVIDIA_* names, or the generic MODEL_* names the hackathon
    # templates use. Base URL, key and model IDs must all come from the same route.
    @property
    def nvidia_base_url(self) -> str:
        return (os.getenv("NVIDIA_BASE_URL", "") or os.getenv("MODEL_BASE_URL", "") or "https://integrate.api.nvidia.com/v1").strip()

    @property
    def nvidia_api_key(self) -> str:
        return (os.getenv("NVIDIA_API_KEY", "") or os.getenv("MODEL_API_KEY", "")).strip()

    @property
    def nvidia_fast_model(self) -> str:
        return (os.getenv("NVIDIA_FAST_MODEL", "") or os.getenv("MODEL_FAST", "") or "nvidia/nemotron-3.5-lightning-30b-a3b").strip()

    @property
    def nvidia_deep_model(self) -> str:
        return (os.getenv("NVIDIA_DEEP_MODEL", "") or os.getenv("MODEL_REASONING", "") or "nvidia/nemotron-3-ultra-550b-a55b").strip()

    @property
    def nvidia_synth_model(self) -> str:
        return (
            os.getenv("NVIDIA_SYNTH_MODEL", "")
            or os.getenv("NEMOTRON_MODEL", "")
            or os.getenv("MODEL_NAME", "")
            or "nvidia/nemotron-3.5-super-vl-120b-a12b"
        ).strip()

    @property
    def scout_hmac_key(self) -> str:
        return os.getenv("SCOUT_HMAC_KEY", "").strip() or _EPHEMERAL_SCOUT_KEY

    @property
    def log_level(self) -> str:
        level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        return level if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO"

    @property
    def scout_rate_limit_per_minute(self) -> int:
        try:
            return max(1, int(os.getenv("SCOUT_RATE_LIMIT_PER_MINUTE", "120")))
        except ValueError:
            return 120

    @property
    def admin_token(self) -> str:
        """Optional bearer token that unlocks state-changing admin endpoints for non-loopback clients."""
        return os.getenv("AUDITWHY_ADMIN_TOKEN", "").strip()

    @property
    def allow_insecure_tls(self) -> bool:
        """Opt-in escape hatch for broken local CA stores; only ever applied to public, credential-free fetches."""
        return os.getenv("AUDITWHY_ALLOW_INSECURE_TLS", "false").strip().lower() in ("1", "true", "yes", "on")


settings = Settings()
