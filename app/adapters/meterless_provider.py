"""Meterless Nightshift Runtime 1.4 (Provider Contract v4) Client & Route Planner.

Implements:
- Base URL normalisation to <scheme>://<host>/v1 (never /v1/v1)
- Quick Connect URL parser: parse_connect_link(text)
- Immediate rejection of placeholder token 'rt-local'
- Mandatory headers: Authorization: Bearer <token>, meterless-app: auditwhy, meterless-scope: user
- GET /v1/models discovery (401/403 -> 'unauthorized', 200 with meterless/* -> 'online', error -> 'offline')
- Port auto-detection across 8088, 8090, 8787, 11434 on 127.0.0.1 and localhost
- Task Router plan_route(task) mapping agent tasks to Meterless classes and NVIDIA Nemotron models
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse
import httpx

from app.config import settings

AUTODETECT_PORTS = (8088, 8090, 8787, 11434)
AUTODETECT_HOSTS = ("127.0.0.1", "localhost", "[::1]")
DEFAULT_ROUTING_MODE = "meterless/auto"
MIN_COMPLETION_TOKENS = 800


def strip_reasoning(content: str) -> str:
    """Drop reasoning a lane leaked into the answer.

    Some Nemotron lanes return their reasoning inline, with or without the opening <think> tag
    ("...reasoning...</think>answer"), so everything up to the last closing tag is discarded.
    """
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    return content.strip()


class MeterlessAuthError(PermissionError):
    """Raised when an invalid or prohibited placeholder token (such as 'rt-local') is used."""


def is_valid_meterless_token(token: Optional[str]) -> bool:
    """Validate token per Provider Contract v4: reject empty strings and placeholder 'rt-local'."""
    if not token:
        return False
    clean = token.strip()
    if not clean or clean.lower() == "rt-local":
        return False
    return True


def normalize_base_url(raw_url: str) -> str:
    """Normalize any base URL (with /v1, /v1/chat/completions, /v1/models, /healthz, /connect, or no scheme)
    to <scheme>://<host>[:<port>]/v1. Never produces /v1/v1.
    """
    text = (raw_url or "").strip()
    if not text:
        return "http://127.0.0.1:8088/v1"

    # Strip fragment and query if present
    text = text.split("#", 1)[0].split("?", 1)[0].strip()

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", text):
        text = "http://" + text

    parsed = urlparse(text)
    scheme = (parsed.scheme or "http").lower()
    netloc = parsed.netloc
    path = parsed.path or ""

    # Strip known suffixes repeatedly so we never produce /v1/v1
    suffix_patterns = [
        r"/v1/chat/completions/?$",
        r"/v1/completions/?$",
        r"/v1/models/?$",
        r"/chat/completions/?$",
        r"/models/?$",
        r"/healthz/?$",
        r"/health/?$",
        r"/connect/?$",
        r"(/v1)+/?$",
    ]
    changed = True
    while changed:
        changed = False
        for pat in suffix_patterns:
            new_path = re.sub(pat, "", path, flags=re.IGNORECASE)
            if new_path != path:
                path = new_path
                changed = True

    path = path.rstrip("/")
    return f"{scheme}://{netloc}{path}/v1"


def parse_connect_link(text: str) -> Dict[str, str]:
    """Parse a Meterless Quick Connect link of the form:
    http(s)://<host>[:<port>]/connect[/]#k=<key>&m=<model>&n=<app>
    Raises ValueError on malformed URLs, non-http/https schemes, non-/connect paths, missing k, or k='rt-local'.
    Never logs the raw connect link.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Connect link cannot be empty.")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Invalid connect link scheme; must be http or https.")
    if not parsed.netloc:
        raise ValueError("Invalid connect link: missing host.")

    clean_path = (parsed.path or "").rstrip("/")
    if clean_path != "/connect":
        raise ValueError("Invalid connect link path; must be '/connect'.")

    if not parsed.fragment:
        raise ValueError("Invalid connect link: missing '#k=...' fragment.")

    params = parse_qs(parsed.fragment, keep_blank_values=True)
    raw_k = params.get("k", [""])[0]
    api_key = unquote(raw_k).strip()

    if not api_key:
        raise ValueError("Invalid connect link: missing 'k' API key parameter in fragment.")
    if not is_valid_meterless_token(api_key):
        raise MeterlessAuthError("Invalid connect link: placeholder token 'rt-local' is rejected by Provider Contract v4.")

    raw_m = params.get("m", [""])[0]
    model = unquote(raw_m).strip() if raw_m else "meterless/auto"
    if not model:
        model = "meterless/auto"

    raw_n = params.get("n", [""])[0]
    app_name = unquote(raw_n).strip() if raw_n else "auditwhy"
    if not app_name:
        app_name = "auditwhy"

    origin = f"{parsed.scheme.lower()}://{parsed.netloc}"
    return {
        "base_url": f"{origin}/v1",
        "api_key": api_key,
        "model": model,
        "app": app_name,
    }


def plan_route(task: str) -> Dict[str, Any]:
    """Map an agent task to Meterless Runtime 1.4 class/disposition and NVIDIA Nemotron model."""
    t = (task or "analyze").strip().lower()
    if t in ("plan", "retrieve", "scout", "fast"):
        return {
            "task": t,
            "meterless_model": "meterless/text.fast",
            "disposition": "fast",
            "nvidia_model": settings.nvidia_fast_model,
            "enable_thinking": False,
        }
    if t in ("evaluate", "grade", "critique", "review", "judge"):
        return {
            "task": t,
            "meterless_model": "meterless/judge.deep",
            "disposition": "deep_verify",
            "nvidia_model": settings.nvidia_deep_model,
            "enable_thinking": True,
        }
    return {
        "task": t,
        "meterless_model": "meterless/text.auto",
        "disposition": "auto_synthesis",
        "nvidia_model": settings.nvidia_synth_model,
        "enable_thinking": False,
    }


class MeterlessProviderClient:
    """Client for Meterless Nightshift Runtime 1.4 implementing Provider Contract v4."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        connect_link: Optional[str] = None,
        app_name: Optional[str] = None,
    ) -> None:
        link = (connect_link if connect_link is not None else settings.meterless_connect_link).strip()
        if link:
            parsed = parse_connect_link(link)
            self.base_url = parsed["base_url"]
            self.api_key = parsed["api_key"]
            self.default_model = parsed["model"]
            self.app_name = parsed["app"]
        else:
            self.base_url = normalize_base_url(base_url or settings.meterless_base_url)
            self.api_key = (api_key if api_key is not None else settings.meterless_api_key).strip()
            self.default_model = DEFAULT_ROUTING_MODE
            self.app_name = (app_name or settings.meterless_app_name).strip() or "auditwhy"
        self.available_models: List[str] = []  # filled by probe_status() from GET /v1/models

    def resolve_model(self, task: str) -> str:
        """Pick the routing mode for a task.

        A METERLESS_MODEL pin wins (use it to lock a Nemotron-backed lane for award evidence). Next, the task's
        planned Nemotron model is requested by id when the runtime advertises it: Nightshift 1.2.7 serves concrete
        provider ids exactly, while its deep/judge modes collapse to the smallest lane, so the judge would otherwise
        run on a 4B model. Then the class.disposition if advertised, falling back to meterless/auto, which every
        runtime serves. Routing modes not recognised here are kept, never rewritten.
        """
        pinned = settings.meterless_model
        if pinned:
            return pinned
        route = plan_route(task)
        if route["nvidia_model"] in self.available_models:
            return route["nvidia_model"]
        wanted = route["meterless_model"]
        if not self.available_models or wanted in self.available_models:
            return wanted
        return DEFAULT_ROUTING_MODE if DEFAULT_ROUTING_MODE in self.available_models else self.available_models[0]

    def build_headers(self, scope: str = "user") -> Dict[str, str]:
        if not is_valid_meterless_token(self.api_key):
            raise MeterlessAuthError(
                "Meterless API key is missing or uses rejected placeholder 'rt-local'."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "meterless-app": self.app_name,
            "meterless-scope": scope,
            "Content-Type": "application/json",
        }

    def probe_status(self, timeout: float = 2.5) -> Dict[str, Any]:
        """Probe GET /v1/models (not /healthz alone) per Provider Contract v4:
        - 'rt-local' or 401/403 -> 'unauthorized'
        - 200 with meterless/* IDs -> 'online'
        - network error -> 'offline'
        """
        if self.api_key.lower() == "rt-local":
            return {
                "status": "unauthorized",
                "base_url": self.base_url,
                "reason": "Placeholder token 'rt-local' is rejected by Provider Contract v4.",
                "models": [],
            }
        headers: Dict[str, str] = {"meterless-app": self.app_name, "meterless-scope": "user"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"{self.base_url}/models", headers=headers)
                if resp.status_code in (401, 403):
                    return {
                        "status": "unauthorized",
                        "base_url": self.base_url,
                        "http_status": resp.status_code,
                        "models": [],
                    }
                if resp.status_code == 200:
                    body = resp.json()
                    models_list = [
                        m.get("id", "")
                        for m in body.get("data", [])
                        if isinstance(m, dict)
                    ]
                    # Contract v4: only "200 with meterless ids" is online; a generic OpenAI server is not Nightshift.
                    if any(m.startswith("meterless/") for m in models_list):
                        if not is_valid_meterless_token(self.api_key):
                            return {
                                "status": "unauthorized",
                                "base_url": self.base_url,
                                "models": models_list,
                                "reason": "Valid runtime token required.",
                            }
                        self.available_models = models_list
                        return {
                            "status": "online",
                            "base_url": self.base_url,
                            "models": models_list,
                        }
                return {
                    "status": "offline",
                    "base_url": self.base_url,
                    "http_status": resp.status_code,
                    "models": [],
                }
        except Exception as exc:
            return {
                "status": "offline",
                "base_url": self.base_url,
                "error": type(exc).__name__,
                "models": [],
            }

    @classmethod
    def auto_detect_runtime(
        cls,
        api_key: Optional[str] = None,
        timeout: float = 0.75,
    ) -> Dict[str, Any]:
        """Probe ports 8088, 8090, 8787, 11434 across 127.0.0.1 and localhost."""
        eff_key = api_key if api_key is not None else settings.meterless_api_key
        checked: List[Dict[str, Any]] = []
        for host in AUTODETECT_HOSTS:
            for port in AUTODETECT_PORTS:
                candidate_url = f"http://{host}:{port}/v1"
                client = cls(base_url=candidate_url, api_key=eff_key)
                probe = client.probe_status(timeout=timeout)
                checked.append({"url": candidate_url, "status": probe["status"]})
                if probe["status"] in ("online", "unauthorized"):
                    return {
                        "found": True,
                        "selected_base_url": candidate_url,
                        "status": probe["status"],
                        "models": probe.get("models", []),
                        "checked": checked,
                    }
        return {
            "found": False,
            "selected_base_url": normalize_base_url(settings.meterless_base_url),
            "status": "offline",
            "models": [],
            "checked": checked,
        }

    def chat_completion(
        self,
        *,
        messages: List[Dict[str, str]],
        task: str = "analyze",
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> Dict[str, Any]:
        """Execute a chat completion request against Meterless Nightshift Runtime 1.4 (180s timeout)."""
        model = self.resolve_model(task)
        headers = self.build_headers(scope="user")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # Nemotron lanes reason before answering, and a small budget can come back empty (organisers: >= 500).
            "max_tokens": max(max_tokens, MIN_COMPLETION_TOKENS),
        }
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            if resp.status_code in (401, 403):
                raise MeterlessAuthError(f"Meterless runtime returned {resp.status_code} unauthorized.")
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            content = strip_reasoning(content if isinstance(content, str) else str(content or ""))
            routing = data.get("meterless") or {}
            return {
                "provider": "meterless_nightshift_1.4",
                "model": model,
                # The runtime reports which concrete model served the routing mode (e.g. a Nemotron lane).
                "served_by": routing.get("routed_model") or data.get("model") or "",
                "content": content,
            }
