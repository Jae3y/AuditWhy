"""Provider Contract v4 tests for Meterless Nightshift Runtime 1.4 & NVIDIA Nemotron routing."""

from __future__ import annotations

import pytest

from app.adapters.meterless_provider import (
    MeterlessAuthError,
    MeterlessProviderClient,
    is_valid_meterless_token,
    normalize_base_url,
    parse_connect_link,
    plan_route,
)


def test_url_normalisation_never_produces_v1_v1() -> None:
    cases = [
        ("http://127.0.0.1:8088", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/v1", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/chat/completions", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/models", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/healthz", "http://127.0.0.1:8088/v1"),
        ("127.0.0.1:8088/v1/chat/completions/", "http://127.0.0.1:8088/v1"),
        ("https://runtime.meterless.ai/connect", "https://runtime.meterless.ai/v1"),
    ]
    for raw, expected in cases:
        norm = normalize_base_url(raw)
        assert norm == expected
        assert "/v1/v1" not in norm


def test_rt_local_token_rejected_immediately() -> None:
    assert is_valid_meterless_token("rt-local") is False
    assert is_valid_meterless_token("RT-LOCAL") is False
    assert is_valid_meterless_token("") is False
    assert is_valid_meterless_token(None) is False
    assert is_valid_meterless_token("ml_live_valid_token_123") is True

    client = MeterlessProviderClient(base_url="http://127.0.0.1:8088/v1", api_key="rt-local")
    with pytest.raises(MeterlessAuthError):
        client.build_headers()

    probe = client.probe_status()
    assert probe["status"] == "unauthorized"


def test_parse_connect_link_eight_edge_cases() -> None:
    # Case 1: Full valid connect link
    c1 = parse_connect_link(
        "http://127.0.0.1:8088/connect#k=ml_live_123&m=meterless/text.fast&n=auditwhy"
    )
    assert c1 == {
        "base_url": "http://127.0.0.1:8088/v1",
        "api_key": "ml_live_123",
        "model": "meterless/text.fast",
        "app": "auditwhy",
    }

    # Case 2: Trailing slash on /connect/
    c2 = parse_connect_link("https://nightshift.local:8090/connect/#k=ml_key_abc")
    assert c2["base_url"] == "https://nightshift.local:8090/v1"
    assert c2["api_key"] == "ml_key_abc"
    assert c2["model"] == "meterless/auto"
    assert c2["app"] == "auditwhy"

    # Case 3: URL-encoded characters in k, m, n
    c3 = parse_connect_link(
        "http://localhost:8787/connect#k=ml_live_%2B99%2F1&m=meterless%2Fjudge.deep&n=auditwhy"
    )
    assert c3["api_key"] == "ml_live_+99/1"
    assert c3["model"] == "meterless/judge.deep"

    # Case 4: Empty m and n fall back to defaults
    c4 = parse_connect_link("http://127.0.0.1:8088/connect#k=ml_live_ok&m=&n=")
    assert c4["model"] == "meterless/auto"
    assert c4["app"] == "auditwhy"

    # Case 5: Placeholder token k=rt-local rejected
    with pytest.raises(MeterlessAuthError):
        parse_connect_link("http://127.0.0.1:8088/connect#k=rt-local&m=meterless/auto")

    # Case 6: Missing fragment / missing k parameter
    with pytest.raises(ValueError):
        parse_connect_link("http://127.0.0.1:8088/connect#m=meterless/auto")

    # Case 7: Non-/connect path rejected
    with pytest.raises(ValueError):
        parse_connect_link("http://127.0.0.1:8088/v1/models#k=ml_live_123")

    # Case 8: Non-http/https scheme rejected
    with pytest.raises(ValueError):
        parse_connect_link("ftp://127.0.0.1:8088/connect#k=ml_live_123")


def test_task_router_plan_route() -> None:
    r_plan = plan_route("plan")
    assert r_plan["meterless_model"] == "meterless/text.fast"
    assert r_plan["nvidia_model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert r_plan["enable_thinking"] is False

    r_eval = plan_route("evaluate")
    assert r_eval["meterless_model"] == "meterless/judge.deep"
    assert r_eval["nvidia_model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert r_eval["enable_thinking"] is True

    r_synth = plan_route("synthesize")
    assert r_synth["meterless_model"] == "meterless/text.auto"
    assert r_synth["nvidia_model"] == "nvidia/nemotron-3.5-super-vl-120b-a12b"
