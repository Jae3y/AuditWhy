"""Three-minute judge demo: the AuditWhy story in four acts, reproducible on any machine.

Runs against a throwaway logs/H-MEM directory, so Act 1 is always a genuine cold discovery and Act 2 compounds on
the rule Act 1 actually mined.

  python scripts/demo.py           # run straight through
  python scripts/demo.py --pause   # presenter mode: press Enter between acts
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_WORK = Path(tempfile.mkdtemp(prefix="auditwhy-demo-"))
os.environ["AUDITWHY_LOGS_DIR"] = str(_WORK / "logs")
os.environ["SAMPLE_MODE"] = "true"
os.environ.setdefault("LOG_LEVEL", "CRITICAL")  # keep per-event trace lines out of the presentation

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analytics import fmt_usd  # noqa: E402
from app.config import SAMPLE_DATA_DIR  # noqa: E402
from app.engines.hmem import HMemEngine  # noqa: E402
from app.orchestration.graph import AuditWhyOrchestrator  # noqa: E402
from app.schemas import RunRequest  # noqa: E402

WIDTH = 78


def banner(title: str) -> None:
    print("\n" + "=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def trace_line(body: dict) -> None:
    flow = " -> ".join(
        f"{e['agent_name'].replace('Agent', '')}[{e['status']}]" for e in body["trace_events"] if e["status"] != "success"
    )
    print(f"  Critique moments: {flow or 'none'}")


def hypotheses_line(body: dict) -> None:
    print("  Hypotheses: " + ", ".join(f"{h['hypothesis_id']}={h['status']} ({h['confidence']:.2f})" for h in body["hypotheses"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pause", action="store_true", help="wait for Enter between acts")
    args = parser.parse_args()

    def pause() -> None:
        if args.pause:
            input("\n  [Enter] next act ")

    hmem = HMemEngine(ledger_path=_WORK / "ledger.jsonl")
    orch = AuditWhyOrchestrator(hmem_engine=hmem, persist_memory=False)

    def run(path: str) -> dict:
        payload = json.loads((ROOT / path).read_text(encoding="utf-8"))
        return orch.run(RunRequest(**payload)).model_dump()

    # ------------------------------------------------------------------ Act 1
    banner("ACT 1  Cold investigation: the evaluator catches what a naive join hides")
    cold = run("input_examples/example_1.json")
    tax = cold["taxonomic_anomaly_resolution"]
    o = tax["orphan_entity"]
    print(f"  Question: {cold['query'][:110]}...")
    print(f"  fk_sic_check: {o['company_name']} (CIK {o['cik']}) files under summary SIC {o['edgar_sic_code']}")
    print(f"  Pass 2 reconciled it to leaf {o['reconciled_leaf_sic_code']}, restoring {fmt_usd(o['restored_fy2025_revenue_usd'])} FY2025 revenue")
    hypotheses_line(cold)
    for u in cold["untested_factors"]:
        print(f"  Blocked as untested: {u['hypothesis_id']} {u['title']}")
    r = cold["multivariate_correlations"]["timeout_vs_retention_loss_r"]
    print(f"  Computed, not asserted: timeout rate vs retention loss r = {r}; overall confidence {cold['confidence']}")
    trace_line(cold)
    pause()

    # ------------------------------------------------------------------ Act 2
    banner("ACT 2  Compounding: a differently worded question reuses the mined rule")
    warm = orch.run(RunRequest(query="Which Computer & Office Equipment accounts carry the most Q4 SLA risk?")).model_dump()
    hm, mk = warm["meterless_engines"]["hmem"], warm["meterless_engines"]["markovian"]
    applied = ", ".join(f"{k}->{v['leaf_code']}" for k, v in hm["rules_applied"].items())
    print(f"  H-MEM policy: {hm['memory_mode']}  |  rules applied: {applied}")
    print(f"  Retrieval passes: {cold['zetaris_lineage']['retriever_passes_executed']} cold -> {warm['zetaris_lineage']['retriever_passes_executed']} warm")
    print(f"  Markovian stages: {len(cold['meterless_engines']['markovian']['stages'])} cold -> {mk['chunks_executed']} warm ({' -> '.join(mk['stages'])})")
    print(f"  Trust ledger: {', '.join(hm['feedback_applied'])}")
    print(f"  Lead finding: {warm['why_explanation']['q4_forward_risk_driver'][:150]}")
    pause()

    # ------------------------------------------------------------------ Act 3
    banner("ACT 3  Adversarial input: blocked before any SQL or model call")
    blocked = run("input_examples/example_3.json")
    layers = blocked["why_explanation"]["risk_assessment"]["layers"]["layer1_prompt_injection"]["violations"]
    print(f"  Status: {blocked['status']}  |  violations: {layers}")
    print(f"  Zetaris statements executed: {blocked['zetaris_lineage']['queries_executed_count']}")
    conflict = blocked["meterless_engines"]["hmem"]["conflict_resolution_test"]
    print(f"  H-MEM conflict test: low-trust claim superseded by {conflict.get('incumbent_id')}")
    pause()

    # ------------------------------------------------------------------ Act 4
    banner("ACT 4  Proof it is not scripted: change the data, the verdict changes")
    perturbed = _WORK / "sample"
    shutil.copytree(SAMPLE_DATA_DIR, perturbed)
    facts_path = perturbed / "edgar_company_facts.json"
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    for row in facts:
        if row["ticker"] == "ORCL":
            row["sic_code"] = "7370"  # a summary code no code path special-cases
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    fresh = AuditWhyOrchestrator(hmem_engine=HMemEngine(ledger_path=_WORK / "l2.jsonl"), sample_dir=perturbed, persist_memory=False)
    body = fresh.run(RunRequest(query="Why did Q3 retention drop in Computer & Office Equipment?")).model_dump()
    for orphan in body["taxonomic_anomaly_resolution"]["orphans"]:
        print(f"  Caught {orphan['ticker']}: SIC {orphan['edgar_sic_code']} -> {orphan['reconciled_leaf_sic_code']} ({fmt_usd(orphan['restored_fy2025_revenue_usd'])})")
    print(f"  Restored revenue now {fmt_usd(body['taxonomic_anomaly_resolution']['restored_revenue_usd'])} (was {fmt_usd(tax['restored_revenue_usd'])})")
    oracle = orch.run(RunRequest(query="Why did Oracle's support MTTR change?")).model_dump()
    h1 = next(h for h in oracle["hypotheses"] if h["hypothesis_id"] == "H1")
    print(f"  Ask about Oracle instead: focus={oracle['why_explanation']['focus']['label']}, H1 {h1['status']} - {h1['finding_summary'][:90]}")

    banner(f"Done. Traces and ledgers for this demo: {_WORK}")


if __name__ == "__main__":
    main()
