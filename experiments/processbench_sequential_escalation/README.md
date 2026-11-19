# Stage B: verification-first diagnostic escalation (feasibility)

Question: after verification-only feedback (V) fails, does escalating to the specific first-error location
(V -> L_true) help more than a budget-matched generic retry (V -> V)? See `STAGE_B_PLAN.md` (pre-specified design) and
`RUNBOOK.md` (commands).

**Status: only the 20-request real smoke tests have been run: smoke1 (768 tokens) and smoke2 (1536 tokens), both
NO-GO, both preserved byte-for-byte and not rescored.** Protocol amendment A1 (max_new_tokens 768 -> 1536; Stage-B
numeric parser `stage_b_numeric_v1`) followed smoke1; amendment A2 (max_new_tokens 1536 -> 2048, nothing else) followed
smoke2 (headroom-only failure). Both were applied before any full run; next is smoke3 on the same 20 requests. Stage A (`experiments/stage_a_reliability_gating/`) is frozen; Stage B only reads
two of its files (sha-verified) to confirm the solver identity, and vendors its persistence module.

> **Protocol amendment A3 (researcher decision 2026-10-13, before any Stage-2 inference or frozen annotation decision).** Stage-B feasibility annotation review uses a blinded LLM-assisted reviewer rather than a human reviewer. These judgments are provisional, must never be described as human-validated, and final-scale experiments should receive human validation. Reviewer: Qwen/Qwen2.5-Math-7B-Instruct @ `ef9926d7`, bitsandbytes 0.50.2 4-bit NF4 (float16 compute), greedy, batch 1, fixed prompts/order, JSON output (`configs/stage_b.json` -> `llm_review`; `src/llm_review.py`). Two passes: Pass 1 (blinded first-error localisation; mapped to CONFIRM/DISAGREE/UNCLEAR against k only after all Pass-1 rows exist), then Pass 2 (candidate false step k-1: CORRECT/NOT_CORRECT/UNCLEAR). Unchanged: failure set, Stage-1 outputs, dataset, parser, scorer, Stage-2 conditions, decision rules, interpretation rule.

> **Protocol amendment A4 (researcher-approved 2026-10-13, before the full review).** The A3 reviewer smoke (Qwen2.5-Math-7B-Instruct) was NO-GO: loading, blinding, determinism, context and resume passed, but Pass-1 and Pass-2 valid JSON were 0/5 (long reasoning ending in boxed pseudo-JSON; one item hit 768 tokens). Archive `9205bdda...` is preserved byte-for-byte in `data/smoke_records/review_smoke_a3/` and never rescored; the parser was NOT relaxed. Reviewer replaced by **Qwen/Qwen2.5-7B-Instruct @ `a09a35458c702b33eeacc393d103063234e8bc28`** (4-bit NF4, float16 compute, batch 1, greedy do_sample=False with repetition_penalty 1.0, max_new_tokens 192, context 32768). Only the fixed system prompt changed (prompt version v2: a classification task, not a request to solve the problem; one JSON object only); user prompts, items, order, smoke items, two-pass procedure, mapping and parse rules are unchanged. The reviewer smoke additionally requires every output to be exactly one JSON object with exactly the allowed keys.

> Stage B uses oracle failure detection (gold answer) and oracle localisation (ProcessBench annotation, checked by a
> blinded human review). It is not a deployable system.
>
> Limitation: GSM8K-test / MATH-test source problems may be in Qwen3-4B's training data; absolute accuracy is not an
> uncontaminated capability estimate. Paired within-item condition differences are the quantities of interest.

## Layout
```
configs/stage_b.json            all settings, pinned source hashes, seeds, gates, annotation review, FROZEN rule
data/raw/                       ProcessBench @3bdcd537, Hendrycks MATH test (rows-API JSON + pinned parquet), MATH-500
data/processed/                 candidates_all.jsonl, stage_b_items.json (all 265 eligible items), stage1_requests.jsonl,
                                stage1_execution_order.json, stage_b_items_review.txt; later stage1_failures.json, ...
data/manifests/                 inspection, dataset/request manifests, dataset_validation.json, FROZEN_HASHES.json,
                                PROTOCOL_AMENDMENTS.json (A1, A2)
data/smoke_records/smoke1/      the real smoke1 archive, byte-for-byte (trigger of A1; never altered or rescored)
data/smoke_records/smoke2/      the real smoke2 archive, byte-for-byte (trigger of A2; never altered or rescored)
src/                            processbench_adapter, dataset_builder, prompt_builder, parser (Stage-B numeric
                                parser v1 + frozen pilot strict parser),
                                scorer, persistence (vendored from Stage A), runner_core, stage1_runner, smoke_gate,
                                annotation_review,
                                stage2_builder, stage2_runner, solver_loader, mock_solver, analysis, figures,
                                frozen_sources, common
scripts/00-13                   fetch, inspect, build, validate, requests, smoke, run, freeze, 07a/07b blinded
                                annotation review, stage-2, analyse, package, kaggle bundle;
                                freeze_interpretation_rule.py, register_protocol_amendment.py,
                                _mock_fill_review_form.py (TEST ONLY)
tests/                          unittest suite (python tests/run_all.py); fixtures/ = verbatim smoke1 parser cases
kaggle/                         bundle + notebook (Phase 1 only)
outputs/                        runs/ (real) and test_runs/ (mock, TEST ONLY)
```
