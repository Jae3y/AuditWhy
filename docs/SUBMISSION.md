# AuditWhy — Submission Pack (Open Agent Hackathon 2026)

Everything the HackOS submission form asks for, ready to paste. Hard deadline: **27 Oct 2026, 23:45 UTC**. Mentors advise submitting by midday on the 27th and updating afterwards.

---

## 1. Project name and description

**Name:** AuditWhy

**One sentence:** AuditWhy helps finance and risk analysts answer "why don't our numbers match?" by sending seven collaborating agents across SEC EDGAR filings, the SIC industry taxonomy and internal billing and support data, and returning an evidence-backed explanation in which every claim without supporting rows is struck out instead of asserted.

**Short paragraph:** When a risk lead asks why enterprise retention dropped, or why internal revenue reporting disagrees with regulatory filings, the evidence lives in disconnected systems. Naive joins between them silently lose data. AuditWhy plans falsifiable hypotheses and queries every source through Zetaris. An independent Evaluator then runs its own data-quality check. In our example it finds IBM filed under SIC 3570, a summary code that a standard join drops, along with $62.75B of revenue. It sends the plan back for revision, and blocks any causal claim, such as "pricing changes caused churn", that has no data behind it. The result is a cited explanation with confidence intervals, an explicit list of what was not tested, and recommended actions. What it learns (the 3570 → 3571 reconciliation rule) is stored in Meterless H-MEM, so the next investigation skips the rediscovery.

## 2. Track

**The Agent That Can Explain Why** (the README, deck, demo and examples all name the same track).

## 3. Team

| Full name | HackOS email | GitHub | Role | Contact |
| :--- | :--- | :--- | :--- | :--- |
| Jackson Sixtus Abetianbe | *(your HackOS account email)* | Jae3y | Solo builder | Primary |

## 4. Links

- **GitHub repository (public):** https://github.com/Jae3y/AuditWhy
- **Live demo (sample mode, no login):** https://auditwhy.vercel.app
- **Demo video:** *(add the link after upload; test it in a private window)*
- **Slide deck:** https://claude.ai/artifact/TQSvLVr9sBxQ84T4nLEYhD (9 slides covering the 8 required points). It is private until shared, so use **Share** on the deck to make it viewable, or **Export** it to PDF/PPTX and upload that file.

## 5. Sponsor technology explanation

**Zetaris (data layer).** All evidence is read through Zetaris Lightning SQL over the REST API (`/sql-editor/sqls/run`, with bearer key, org ID and a fresh request ID on every call). The internal ERP billing, support telemetry, EDGAR facts and SIC tables are registered as Zetaris REST tables and schemastore views (`scripts/zetaris_onboard.py`). The taxonomy check runs on the official `lightning.metastore.usl_demo.sic_edgar_usl` Unified Semantic Layer. The Evidence Evaluator issues its own `SHOW DQ ALL INVALID TABLE …sic_edgar_usl.company` query, and the foreign-key rule's invalid record (IBM, SIC 3570) drives the revision loop. Remove Zetaris and the agents have no evidence and no data-quality verdict, so the system cannot answer.

**Meterless (build and power layers).** Four engines are implemented from the AGENTS.md specs and used in the working feature:
- **Scout Intent** screens every question through a 5-layer guard and signs an HMAC-SHA256 execution contract that each Zetaris call verifies.
- **World Model** grounds rows from three sources as stable `ent_` entities that the analyst reads back.
- **Markovian** runs the analysis as bounded chunks with compressed carryover. A warm run skips a whole stage, and the token savings are measured (not projected).
- **H-MEM** stores the reconciliation rule with provenance. It scores recalled rules by outcome (helpful / wrong) and supersedes a wrong rule automatically.

The conformance suite (`scripts/run_conformance.py`, 7/7) and 83 tests verify them. **Nightshift** is the primary model provider: every agent model call goes to the OpenAI-compatible runtime with the runtime token and `meterless-app` / `meterless-scope` headers, under provider contract v4.

**NVIDIA Nemotron.** Three Nemotron lanes, routed by task class:
- a fast lane (`nemotron-3.5-lightning-30b-a3b`) for planning;
- a deep/judge lane (`nemotron-3-ultra-550b-a55b`, thinking enabled) for evaluation;
- a synthesis lane (`nemotron-3.5-super-vl-120b-a12b`) for the executive explanation.

Nightshift is tried first, and each call records `served_by`, the concrete model behind the routing mode. When Nightshift isn't running, the same lanes are called directly on NVIDIA's hosted API (setup guide Option B). This path has been verified live: a full run of `example_1` made 8 of its 10 model calls through Lightning, Ultra and Super-VL. Each lane sets `enable_thinking` explicitly, so Nemotron 3.5 never spends the whole token budget on hidden reasoning. Nemotron writes the narrative; verdicts stay in code, so they are reproducible.

**Coding tool.** Built with Claude Code as the AI coding assistant. Cursor is optional since the 22 Oct onboarding-guide update, and the AI Usage Policy permits any tool. Every change was reviewed and covered by tests.

**What was unique.** The Evaluator doesn't trust the Retriever: it queries Zetaris itself to verify the joined evidence before any finding is approved. A data-quality error the agents discover becomes a reusable H-MEM rule, and that memory is trust-scored by outcome: a recalled rule that fails to restore its filer is penalised and superseded on the spot.

## 6. Pre-existing components

None. All code, data samples and documentation were created during the build window. The first commit is dated 24 Oct 2026, after the 22 Oct 00:00 UTC start. Open-source libraries (FastAPI, Uvicorn, Pydantic, HTTPX, Tenacity, python-dotenv, pytest) need no declaration. The ERP billing and support telemetry data are synthetic and labelled as such.

## 7. Colab links

Not applicable (no Colab).

---

## Demo video script (target 3:20, hard limit 4:00)

Record at 1080p with a large terminal font and the browser at 110–125% zoom. Never show `.env` or a key page. Suggested file name: `OAH2026-AuditWhy-ExplainWhy-Demo.mp4`.

| Time | Show | Say |
| :--- | :--- | :--- |
| 0:00–0:20 | README header | "Our entry is for The Agent That Can Explain Why. A risk lead asks why enterprise retention dropped, but the answer is spread across SEC filings, an industry taxonomy and internal billing and support systems. Naive joins between them quietly lose data." |
| 0:20–0:50 | README architecture diagram | "Seven agents. Scout guards the input and signs a contract. The Planner writes testable hypotheses. The Retriever queries every source through Zetaris. The Evaluator challenges the evidence with its own data-quality query. The Analyst reasons in bounded Markovian chunks. The Writer and Reviewer produce an answer, and the Reviewer won't approve it without a caveat." |
| 0:50–1:40 | auditwhy.vercel.app: press **Investigate** on the cold example | "Here's a real run. Every number you see is computed from rows retrieved in this run, nothing is pre-written. In the trail on the right, step six is highlighted: the Evaluator found IBM filed under SIC 3570, a summary code the standard join drops, along with $62.75 billion of revenue. It sends the plan back, the Retriever expands the hierarchy, and the re-check passes." |
| 1:40–2:20 | Scroll to the struck claims and the evidence ledger | "The Analyst proposed that pricing changes caused churn. The Evaluator checked the ledger, saw the pricing table returned no rows, and struck the claim. It's listed as untested, with exactly the data needed to test it. Correlations come with confidence intervals, and the caveat is mandatory." |
| 2:20–2:50 | Terminal: `python scripts/demo.py --pause` acts 2 and 4 | "Memory compounds. A differently worded follow-up recalls the rule from H-MEM, skips the discovery, and finishes in fewer stages. And to prove it isn't scripted: change Oracle's SIC code in the data, and the agents catch the new orphan and recompute everything." |
| 2:50–3:20 | Sponsor section of the README, then the trace JSONL | "Zetaris is the data layer and runs the data-quality rule. Meterless provides the four engines, and Nightshift is the model provider routing to NVIDIA Nemotron. The output is an explanation an analyst can act on, with traceable evidence, stated limits and next steps." |

**Live-mode cut (optional, if Zetaris and Nightshift are running):** show `python scripts/live_check.py` passing with a Nemotron model reported in `served_by`, and a live run of `input_examples/example_1.json`.

---

## Final checklist (from the Disqualification Checklist)

| Item | Status |
| :--- | :--- |
| Public repo with README (setup, usage, dependencies) | Done |
| Runs from a clean clone (CI: Python 3.11 and 3.12, Docker `/health` and `/run`) | Done |
| `SAMPLE_MODE=true` runs real agent logic with no credentials | Done |
| 3+ sample inputs and outputs, JSONL traces in `logs/` | Done |
| No hardcoded outputs (perturbation tests in `tests/test_investigation_integrity.py`) | Done |
| No committed secrets (`.env` gitignored; scan clean) | Done |
| Zetaris, Meterless and NVIDIA live | Done and verified on 27 Oct. Local Zetaris 2.4.3 served 11/11 queries, and the official `sic_edgar_usl` DQ rule flagged IBM from live SEC data. Nightshift 1.2.7 served 10/10 Nemotron calls. Evidence: `output_examples/live_example_*.json`, `docs/live_check_2026-10-27.txt` |
| HackOS draft | Saved (track, description, GitHub link, deck PDF). Add the video link, then press Submit |
| Demo video 1–4 min, link opens in a private window | **To do** (script above) |
| Slide deck (8 points) | Done; share or export it before submitting |
| Submitted on HackOS before 23:45 UTC, 27 Oct | **To do** |
