"""End-to-End Smoke Test for AuditWhy (FastAPI TestClient + 3 Canonical Input Examples)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from run import app


def main() -> None:
    print("=" * 72)
    print("AuditWhy — End-to-End Smoke Verification")
    print("=" * 72)

    client = TestClient(app)

    # 1. Verify GET /health
    health_resp = client.get("/health")
    assert health_resp.status_code == 200, f"GET /health failed: {health_resp.text}"
    health_data = health_resp.json()
    assert health_data["status"] == "ok"
    assert health_data["service"] == "auditwhy"
    print(f"[PASS] GET /health -> {json.dumps(health_data)}")

    # 2. Verify GET / (UI Dashboard)
    ui_resp = client.get("/")
    assert ui_resp.status_code == 200 and "AuditWhy" in ui_resp.text
    print(f"[PASS] GET / (Dashboard HTML) -> {len(ui_resp.text)} bytes served")

    # 3. Verify POST /run across all 3 examples
    for idx in (1, 2, 3):
        ex_path = ROOT / "input_examples" / f"example_{idx}.json"
        payload = json.loads(ex_path.read_text(encoding="utf-8"))
        resp = client.post("/run", json=payload)
        assert resp.status_code == 200, f"POST /run failed on example_{idx}: {resp.text}"
        body = resp.json()
        print(
            f"[PASS] POST /run (example_{idx}.json) -> status={body['status']} "
            f"events={len(body['trace_events'])} duration={body['duration_ms']}ms"
        )

    # 4. Verify Vibe Coding payload compatibility
    vibe_payload = {
        "request_id": "vibe-smoke-001",
        "user_query": "Why did enterprise retention drop in Computer & Office Equipment?",
        "context": {"track": "Track 02 — The Agent That Can Explain Why", "sample_mode": True},
    }
    vibe_resp = client.post("/run", json=vibe_payload)
    assert vibe_resp.status_code == 200
    vibe_body = vibe_resp.json()
    assert vibe_body["status"] == "success"
    print(
        f"[PASS] POST /run (Vibe Coding payload) -> status={vibe_body['status']} "
        f"events={len(vibe_body['trace_events'])}"
    )

    print("=" * 72)
    print("ALL SMOKE TESTS PASSED (100% GREEN)")
    print("=" * 72)


if __name__ == "__main__":
    main()
