# Stage B runbook

All commands run from `experiments/stage_b_sequential_escalation/`. Local machine = dataset, validation, mock tests only
(the real solver refuses to load off-Kaggle). Mock outputs land in `outputs/test_runs/` and are labelled
"NO REAL MODEL RESULTS YET – TEST ONLY".

## Done (no model)
```
python scripts/00_fetch_sources.py --verify     # pinned raw sources (ProcessBench, MATH rows dump, MATH-500, pilot GSM8K)
python scripts/01_inspect_processbench.py       # -> data/manifests/processbench_inspection.json, PROCESSBENCH_INSPECTION.md
python scripts/02_build_dataset.py              # frozen pool = all 265 eligible items (re-run = byte-identical check)
python scripts/03_validate_dataset.py           # independent re-derivation of every item
python scripts/04_build_stage1_requests.py      # frozen 265 Stage-1 requests + execution order
python scripts/freeze_interpretation_rule.py --approved-by ...   # DONE: precision rule + analysis settings locked
python scripts/register_protocol_amendment.py --smoke1-archive <zip> --approved-by ...
                                                # DONE (A1): 1536 tokens + Stage-B parser registered; smoke1 archive
                                                # preserved byte-for-byte in data/smoke_records/smoke1/ (not rescored)
python scripts/register_protocol_amendment.py --amendment-id A2 --trigger-archive <zip> --approved-by ...
                                                # DONE (A2): 2048 tokens registered (parser unchanged); smoke2 archive
                                                # preserved byte-for-byte in data/smoke_records/smoke2/ (not rescored)
python tests/run_all.py                         # unit + mock integration tests -> tests/LAST_PASS.json
python scripts/13_build_kaggle_bundle.py        # kaggle/stage_b_bundle.zip + kaggle/stage_b_run.ipynb
```

## Phase 1 on Kaggle (needs researcher approval at each step)
1. Upload `kaggle/stage_b_bundle.zip` as a Kaggle Dataset named `stage-b-sequential-escalation-bundle`; import
   `kaggle/stage_b_run.ipynb`; GPU T4 x2, Internet on, attach the dataset.
2. **Stage-1 smoke** (smoke1 at 768 tokens and smoke2 at 1536 tokens: done, NO-GO, preserved). **smoke3**
   (amendment A2, bundle v4): Cell 1 `MODE = "smoke3"` -> Save Version -> Save & Run All. Underlying command:
   `python scripts/05_stage1_smoke_test.py --solver qwen --output-root /kaggle/working/stage_b_runs`
   (run dir `smoke3_stage1_qwen`). Download `stage_b_smoke3_RESULTS.zip` (+ .sha256.txt) and give it to Claude.
   `smoke_gate.json` says: GO (>= 19/20 parsed; no persistent technical failure, no hash/prompt mismatch, no
   truncation, no response > 90% of 2048 = 1843.2 tokens) / REVIEW (18/20 parsed, rest passes) / NO-GO (otherwise).
   The notebook cell finishes normally for all three; the decision is in the printed gate.
3. **Full Stage 1** (only after approval; 265 requests): `MODE = "stage1"`. Runs `06_run_stage1.py` (resume: set `RESUME_FROM` to
   the last checkpoint of a previous version's output), then `07_freeze_stage1_failures.py`, then
   `12_package_results.py`. Download `stage_b_stage1_qwen3_4b_FINAL.zip` + `.sha256.txt`. **STOP.**
4. Locally: `python scripts/12_package_results.py --verify <zip>`, then
   `python scripts/07_freeze_stage1_failures.py --source <zip>` (reproduces and locks the identical failure set in
   `data/processed/`). Report the failure count and the gate result.
5. Blinded annotation review (local, researcher):
   `python scripts/07a_export_annotation_review.py` -> `data/processed/annotation_review/annotation_review_packet.md`
   + blank `annotation_review_form.csv` (failed items only, no Stage-1 model output). Copy the form to
   `annotation_review_form_FILLED.csv`, fill `first_error_verdict` (CONFIRM/DISAGREE/UNCLEAR) and
   `false_step_verdict` (CORRECT/NOT_CORRECT/UNCLEAR) for every row, then
   `python scripts/07b_freeze_annotation_review.py --reviewer "<name>"`. **STOP** and report.

## A3 blinded LLM-assisted annotation review (replaces the manual review; PROVISIONAL, not human-validated)
```
python scripts/register_protocol_amendment.py --amendment-id A3 --no-trigger --approved-by ...   # DONE
python scripts/14_build_llm_review_requests.py      # DONE: frozen Pass-1/Pass-2 requests (76 + 76), audited
python tests/run_all.py && python scripts/13_build_kaggle_bundle.py                               # bundle v5
# Kaggle (bundle v6, H1 test fix): MODE "review_smoke" -> NO-GO (Qwen2.5-Math: 0/5 valid JSON); record preserved
python scripts/register_protocol_amendment.py --amendment-id A4     --trigger-archive <path>/stage_b_llm_review_smoke_RESULTS.zip --approved-by ...   # DONE: reviewer -> Qwen2.5-7B-Instruct
python tests/run_all.py && python scripts/13_build_kaggle_bundle.py                               # bundle v7
# Kaggle (bundle v7): MODE "review_smoke" (15_llm_review_smoke.py --reviewer qwen_instruct; 20 generations) -> STOP, report
#                     MODE "review_full"  (16_run_llm_review.py --reviewer qwen_instruct; 152 generations) only after approval -> STOP
python scripts/17_freeze_llm_review.py --source <stage_b_llm_review_FINAL.zip>    # local: verify, map, fill form, report
python scripts/07b_freeze_annotation_review.py --reviewer "<A3/A4 LLM-assisted reviewer label>"   # only after approval
```
A4 reviewer smoke -> NO-GO (Pass 1 3/5, Pass 2 4/5 valid; self-contradictory reasoning on manual inspection). The LLM
reviewer is STOPPED by A5 (15/16 refuse the real reviewer, 17 refuses to freeze into data/; mock tests kept).

## A5 blinded HUMAN annotation review (replaces the LLM reviewer)
```
python scripts/register_protocol_amendment.py --amendment-id A5 --trigger-archive <path>/stage_b_llm_review_smoke_RESULTS.zip --approved-by ...   # DONE: A4 record -> data/smoke_records/review_smoke_a4/
python scripts/18_export_human_review.py                                     # DONE: blinded pass1_view.json + REVIEWER_INSTRUCTIONS.md
python scripts/19_human_review_server.py --pass 1 --reviewer "<name>"        # http://127.0.0.1:8765/ ; Ctrl+C anytime, rerun to resume
python scripts/20_freeze_human_review.py --pass 1 --reviewer "<name>"        # locks Pass 1, THEN maps vs ProcessBench k, writes Pass-2 view
python scripts/19_human_review_server.py --pass 2 --reviewer "<name>"
python scripts/20_freeze_human_review.py --pass 2 --reviewer "<name>"        # fills annotation_review_form_FILLED.csv; STOP, report
python scripts/07b_freeze_annotation_review.py --reviewer "<name>"           # only after approval; Stage 2 only after that
```
Files: `data/processed/human_review/` (views, append-only hash-chained logs, finals, mapping, judgments) and
`data/manifests/HUMAN_REVIEW_*.json` (locks). Reviewer must not open the files listed in REVIEWER_INSTRUCTIONS.md.

## Phase 2 (only after approval of the Stage-1 report)
```
python scripts/08_build_stage2_requests.py          # needs frozen review; refuses if < 50 failures / < 50 confirmed items
python tests/run_all.py && python scripts/13_build_kaggle_bundle.py   # new bundle (Phase-2 cells added then)
# Kaggle: 09_stage2_smoke_test.py --solver qwen ; after approval 10_run_stage2.py --solver qwen
python scripts/11_analyze_results.py --stage1-run <stage1 FINAL zip> --stage2-run <stage2 FINAL zip> --out-dir analysis/
```

## Mock dry run (TEST ONLY)
```
W=outputs/test_runs/e2e/stage2_inputs
python scripts/06_run_stage1.py --solver mock
python scripts/07_freeze_stage1_failures.py --source outputs/test_runs/test_stage1_mock --work-dir $W
python scripts/07a_export_annotation_review.py --work-dir $W
python scripts/_mock_fill_review_form.py --work-dir $W          # TEST ONLY verdicts
python scripts/07b_freeze_annotation_review.py --reviewer TEST --work-dir $W
python scripts/08_build_stage2_requests.py --work-dir $W
python scripts/10_run_stage2.py --solver mock --work-dir $W
python scripts/11_analyze_results.py --stage1-run outputs/test_runs/test_stage1_mock \
       --stage2-run outputs/test_runs/test_stage2_mock --out-dir outputs/test_runs/e2e/analysis --work-dir $W
```

## Persistence rules
- Source of truth = `raw_outputs.jsonl`: one fsync'ed line per completed request; never regenerated.
- `state.json` rewritten atomically; `checkpoints/checkpoint_NNNN.zip` every 10 rows + `checkpoint_FINAL.zip`.
- `run.lock` prevents two writers; a run dir is bound to its manifest identity (model, revision, max_new_tokens,
  request/order/items hashes, plan hash, upstream failure-set hash, parser version + sha256, protocol amendments).
- Retries: transport errors only (3 attempts). Wrong / unparsed / refused / truncated outputs are never retried.
- A frozen artifact may change only via `--rebuild "<approved reason>"` while no real run exists; every change is
  logged (old -> new sha256) in `data/manifests/FROZEN_HASHES.json` -> change_log.

## Stage 2 (A6 approved 2026-10-14) - bundle v8
```
python scripts/21_freeze_pass1_only_review.py --reviewer "Sneha"     # DONE (A6: Pass 2 waived)
python scripts/07b_freeze_annotation_review.py --reviewer ...         # DONE: 61 primary / 61 L_false (Pass-1-implied)
python scripts/08_build_stage2_requests.py                           # DONE: 244 requests (61 x 4)
python scripts/09a_freeze_stage2_smoke_selection.py                  # DONE: smoke = sb_gsm8k-107, sb_math-275, sb_math-355 (Stage-1 truncated)
python tests/run_all.py && python scripts/13_build_kaggle_bundle.py   # DONE: bundle v9 (supersedes v8)
# Kaggle (bundle v9): MODE "stage2_smoke" (12 requests) -> STOP, report
#                     MODE "stage2" (244) only after approval; Cell 1 needs the approved smoke archive SHA-256
```
# v9 Kaggle stage2_smoke -> NO-GO (sb_math-355 looped to 2048/2048 in all 4 conditions); archive c5bd1fb6 preserved in
#   data/smoke_records/stage2_smoke_v9/ (+ data/manifests/STAGE2_SMOKE_V9_NOGO.json). Full Stage 2 NOT run.
# Bundle v10: MODE "diag_headroom" = DIAGNOSTIC ONLY, one generation s2::sb_math-355::VV at max_new_tokens 4096
#   (scripts/22_diag_headroom.py; not Stage-2 data, not scored) -> stage_b_diag_headroom_RESULTS.zip -> report, STOP.
# v10 diag_headroom stopped in Cell 4 (test ran the real diagnostic; permitted on Kaggle) -> harness correction H2
#   (data/manifests/HARNESS_CORRECTIONS.json): STAGE_B_TEST_SUITE guard, mock-only tests. Bundle v11: rerun MODE "diag_headroom".

## Amendment A7 (2026-10-14) - Stage-2 smoke-gate clarification - bundle v12
- v9 Stage-2 smoke NO-GO (archive c5bd1fb6...) is preserved and never rescored.
- v11 DIAGNOSTIC-ONLY headroom run (archive 72af3dd8..., `data/smoke_records/diag_headroom_v11/`, record
  `data/manifests/DIAG_HEADROOM_V11_RECORD.json`): s2::sb_math-355::VV at 4096 -> length 4096/4096, no FINAL_ANSWER,
  first 2048 tokens identical to v9, loop continues. Not scored, never substituted. Cap stays 2048.
- A7 gate (`src/smoke_gate.decide_stage2_a7`, script 09): technical + persistence/integrity checks decide; a response
  reaching the 2048 cap that completed and was persisted correctly is model behaviour (failed repair if unparsed), not
  a NO-GO; cap hits / headroom are reported descriptively; parse thresholds (GO <= 1 / REVIEW 2) apply to responses
  that finished normally.
- Unchanged: 2048 cap, 244 requests, order, smoke selection (incl. sb_math-355), eligible set, prompts, parser,
  scorer, conditions, primary endpoint, interpretation rule.
- Bundle v12, MODE "stage2_smoke": same 12 requests -> stage_b_stage2_smoke_RESULTS.zip. Full Stage 2 stays LOCKED
  (`scripts/10_run_stage2.py --solver qwen` and notebook MODE "stage2" refuse) until a NEW smoke archive gets GO and is
  registered as `data/manifests/STAGE2_SMOKE_GO.json`.

## Stage-2 smoke GO (2026-10-14) - full Stage 2 unlocked - bundle v13
- v12 smoke archive 0a287c77... (researcher-inspected GO under A7) preserved byte-for-byte in
  `data/smoke_records/stage2_smoke_v12_go/`, record `data/manifests/STAGE2_SMOKE_GO.json`
  (`scripts/23_register_stage2_smoke_go.py`; integrity fields only - scored outputs never opened, no rescue rates).
- `stage2_runner.require_smoke_go()` / notebook MODE "stage2" accept ONLY this registered record.
- Bundle v13, MODE "stage2", two sessions (STAGE2_SESSION_LIMIT = 122 new generations each; plan/identity unchanged):
  session 1 -> `stage_b_stage2_PARTIAL.zip`; session 2 with RESUME_FROM_SHA256 (zip sha, or raw_outputs.jsonl sha if
  the zip was extracted) -> `stage_b_stage2_qwen3_4b_FINAL.zip`. Give Claude both. No analysis before approval.
