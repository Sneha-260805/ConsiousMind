# Stage B plan: verification-first diagnostic escalation feasibility

Status: **protocol approved with changes (2026-10-12); amendment A1 applied after the real smoke1 test (NO-GO) and
amendment A2 after the real smoke2 test (NO-GO, headroom only); no full Stage-1 run has been made.** Everything here
except A1/A2 was fixed before any Stage-B model output existed; A1 and A2 were fixed after the 20-request smoke
integrity tests and before any full run. Stage A is frozen and untouched; its NO
SUPPORT verdict stands.

> **Protocol amendment A1 (researcher-approved pre-full-run smoke-test correction, 2026-10-12).** The real smoke1
> test (`stage_b_smoke1_RESULTS.zip`, sha256 `c8e965fb…81b1`) passed every technical check but returned NO-GO: 12/20
> parsed, 6/20 truncated at 768 tokens, 0 tokens of headroom. Changes: (1) `max_new_tokens` 768 -> **1536** for
> Stage 1 and Stage 2; (2) a versioned **Stage-B numeric parser** (`stage_b_numeric_v1`, §4a) replaces the frozen
> pilot parser as the scoring parser. Unchanged: dataset items, Stage-1 requests/prompts/order, the 20 smoke
> requests, conditions, feedback wording, gates, interpretation rule + analysis settings, model + revision, decoding.
> The smoke1 archive is preserved byte-for-byte in `data/smoke_records/smoke1/` and is **not** altered or rescored.
> Record: `data/manifests/PROTOCOL_AMENDMENTS.json`; config + parser hashes logged old -> new in `FROZEN_HASHES.json`.

> **Protocol amendment A2 (researcher-approved pre-full-run smoke-test correction, 2026-10-13).** The real smoke2
> test (`stage_b_smoke2_RESULTS.zip`, sha256 `e95729e4…6c59`, A1 settings) completed 20/20, parsed 19/20, had zero
> transport failures/retries and passed prompt/hash integrity, determinism, resume, canary and no-visible-thinking
> checks (the A1 parser change appears successful), but returned NO-GO on generation headroom only: sb_math-150 hit
> 1536/1536 tokens (truncated, no valid answer) and sb_math-7 completed at 1422 tokens (> 1382.4 = 90%). Change:
> `max_new_tokens` 1536 -> **2048** for Stage 1 and Stage 2. Unchanged: dataset, requests, the 20 smoke requests,
> feedback wording, parser (`src/parser.py` byte-identical), scorer, gates, interpretation rule, model revision,
> decoding settings, statistical plan. smoke1 and smoke2 are preserved byte-for-byte in `data/smoke_records/` and
> are **not** altered or rescored. Config hash logged old -> new in `FROZEN_HASHES.json`; record appended to
> `PROTOCOL_AMENDMENTS.json`.

> **Protocol amendment A3 (researcher decision 2026-10-13, before any Stage-2 inference or frozen annotation decision).** Stage-B feasibility annotation review uses a blinded LLM-assisted reviewer rather than a human reviewer. These judgments are provisional, must never be described as human-validated, and final-scale experiments should receive human validation. Reviewer: Qwen/Qwen2.5-Math-7B-Instruct @ `ef9926d7`, bitsandbytes 0.50.2 4-bit NF4 (float16 compute), greedy, batch 1, fixed prompts/order, JSON output (`configs/stage_b.json` -> `llm_review`; `src/llm_review.py`). Two passes: Pass 1 (blinded first-error localisation; mapped to CONFIRM/DISAGREE/UNCLEAR against k only after all Pass-1 rows exist), then Pass 2 (candidate false step k-1: CORRECT/NOT_CORRECT/UNCLEAR). Unchanged: failure set, Stage-1 outputs, dataset, parser, scorer, Stage-2 conditions, decision rules, interpretation rule.

> **Protocol amendment A4 (researcher-approved 2026-10-13, before the full review).** The A3 reviewer smoke (Qwen2.5-Math-7B-Instruct) was NO-GO: loading, blinding, determinism, context and resume passed, but Pass-1 and Pass-2 valid JSON were 0/5 (long reasoning ending in boxed pseudo-JSON; one item hit 768 tokens). Archive `9205bdda...` is preserved byte-for-byte in `data/smoke_records/review_smoke_a3/` and never rescored; the parser was NOT relaxed. Reviewer replaced by **Qwen/Qwen2.5-7B-Instruct @ `a09a35458c702b33eeacc393d103063234e8bc28`** (4-bit NF4, float16 compute, batch 1, greedy do_sample=False with repetition_penalty 1.0, max_new_tokens 192, context 32768). Only the fixed system prompt changed (prompt version v2: a classification task, not a request to solve the problem; one JSON object only); user prompts, items, order, smoke items, two-pass procedure, mapping and parse rules are unchanged. The reviewer smoke additionally requires every output to be exactly one JSON object with exactly the allowed keys.

> **Protocol amendment A5 (researcher decision 2026-10-14).** The A4 reviewer smoke (Qwen2.5-7B-Instruct, archive `dd15c0cb...`, preserved byte-for-byte in `data/smoke_records/review_smoke_a4/`) was NO-GO: infrastructure passed, but valid structured judgments were Pass 1 3/5 and Pass 2 4/5, and manual inspection showed self-contradictory reviewer reasoning. LLM-reviewer development was stopped (not repaired by loosening the parser or JSON format). The annotation-fidelity review became a blinded HUMAN two-pass review (`src/human_review.py`, scripts 18-20; append-only hash-chained judgment logs). Pass 1 (76 items) was completed and frozen before any comparison with ProcessBench: 61 CONFIRM, 15 DISAGREE, 0 UNCLEAR.

> **Protocol amendment A6 (researcher decision 2026-10-14, before any Stage-2 request or inference).** A separate manual Pass-2 review is no longer required. For the 61 Pass-1 CONFIRM items the reviewer already asserted that Step k is the earliest substantive error, so every preceding step, including the predetermined false-location candidate f = k-1, was implicitly judged free of an earlier substantive error. **A separate Pass-2 human recheck was waived for feasibility because it would duplicate the same reviewer's earlier judgment.** Consequences: (i) Stage-2 true-location analyses use only the 61 Pass-1 CONFIRM items; (ii) L_false uses only those 61 items and the frozen candidate f = k-1, labelled **"Pass-1-implied false-step validity"** (NOT independently Pass-2-validated); (iii) the 15 DISAGREE items are not used in the primary Stage-2 analysis (excluded in all conditions; the reviewer's alternative step is recorded only, never substituted); (iv) the frozen Pass-1 files and hashes are unchanged and Pass 1 is not reopened; (v) the primary endpoint is unchanged: paired rescue difference V -> L_true vs V -> V, under the unchanged frozen interpretation rule. The V -> L_true vs V -> L_false diagnostic-content contrast is reported with the L_false validity label. Decisions: `scripts/21_freeze_pass1_only_review.py` -> 07b.

> **Oracle status.** Stage B uses two oracle components: (1) the gold answer decides whether Stage 1 failed and
> therefore whether escalation is triggered; (2) L_true / N_true use the expert-annotated first-error step (further
> checked by a blinded human review). Stage B is **not** a deployable system. It asks: *is there potential value in
> diagnostic escalation if failure detection and localisation were available?*
>
> **Limitation: possible training contamination.** Items come from GSM8K-test and MATH-test source problems, which
> Qwen3-4B may have seen in training. Absolute accuracy and repair rates are **not** uncontaminated capability
> estimates. The quantities of interest are paired, within-item differences between feedback conditions.

## 1. Question

When V ("The final answer in the proposed solution is wrong.") fails to repair a trace, does escalating to the
first-error location help more than another generic verification-only attempt? The primary, budget-matched
comparison is **V -> L_true vs V -> V**. Not in scope: routers, utility models, learned critics or verifiers, other
solver families, ProofWriter, Stage-A items, and any novelty or publication claim.

## 2. Dataset (frozen: `data/processed/stage_b_items.json`)

Source: **Qwen/ProcessBench @ `3bdcd5371ed567559a78f559c01c13a6deee7604`**, 3,400 records. The schema is
`id, generator, problem, steps[], final_answer_correct, label`, where `label` is the 0-indexed first error step
(−1 means none). There is no reference-answer field, so gold answers come by exact problem match:

- GSM8K split -> GSM8K test (after `####`).
- MATH split -> Hendrycks MATH test (last `\boxed{}`). This was fetched as JSON from the HF rows API. It agrees with
  MATH-500 on all 112 overlapping records.

OlympiadBench and Omni-MATH are not used (their answers are symbolic). Eligibility rules E0–E13 are in
`src/dataset_builder.py`.

**Stage 1 uses ALL 265 eligible items** (121 GSM8K, 144 MATH; researcher decision). There is no sampling and no
reserve. Domain, MATH level and MATH subject are kept for **stratified secondary** analysis. All gold answers are
integers. The annotated step k is 1-indexed (k = label + 1).

## 3. Model

Frozen pilot protocol, identical to frozen Stage A and checked by code: Qwen/Qwen3-4B @
`3101254bbe4169895668a0e7653c3fd1f313576e`, FP16, cuda:0 (Kaggle T4), greedy decoding, enable_thinking False,
batch 1, stateless single user message. `max_new_tokens = 2048` (amendment A2; was 768 for smoke1 and 1536 for smoke2). It applies to
Stage 1 and Stage 2 and is frozen for the full runs.

## 4. Stage 1 (265 requests, V on every item)

```
Problem:
{question}

Original proposed solution:
Step 1: ...

Feedback:
The final answer in the proposed solution is wrong.

Assess the proposed solution carefully and give your best corrected final answer.

Return the final answer on the last line exactly as:

FINAL_ANSWER: <answer>
```

### 4a. Answer parsing (amendment A1: `src/parser.py`, `stage_b_numeric_v1`; frozen and hash-registered)

Numeric parser (scoring). The first applicable rule decides; nothing else is searched.

- **a.** If a `FINAL_ANSWER:` label exists (case-insensitive), the **last** label's field must hold exactly one
  standalone number (the frozen pilot field rule). With a label present there is no fallback. If the generation
  was truncated and the field runs to the end of the text, the number may be cut off -> `truncated_field`.
- **b.** Only without a label: the **terminal answer region** = the text after the last "final answer" phrase if it
  lies within the last 6 non-empty lines, else the last paragraph. Only properly closed `\boxed{…}` answers there
  count, normalised like the gold answers; exactly one distinct numeric value -> parsed.
- **c.** The reasoning body is never searched; bare numbers are never taken (the pilot's last-line fallback is not
  used: it read "5" from a truncated "### Step 5: Final").
- **d.** An unclosed `\boxed{` in the region (e.g. truncated `\boxed{90`) -> `incomplete_boxed`, unparseable.
- **e.** Two or more incompatible candidates in the region -> `ambiguous`, unparseable.
- **f.** `strict_format_compliant` comes only from the frozen pilot strict parser; a fallback parse never makes a
  response strictly compliant. Format compliance is reported separately.

Regression fixtures: verbatim smoke1 responses (`tests/fixtures/smoke1_parser_cases.json`): sb_math-64 -> 0,
sb_math-268 -> 27, sb_gsm8k-82 -> 10, sb_math-186 -> 0, truncated sb_math-114 `\boxed{90` -> not accepted.

**Smoke gate (20 requests; `src/smoke_gate.py`).** smoke3 = the same 20 frozen requests under A1 + A2.

- **GO:** at least 19/20 responses parse, AND
  - no persistent technical failure (a transport error that succeeds on retry is not persistent),
  - no hash or prompt mismatch,
  - no truncation,
  - no response above 90% of the generation limit (1843.2 of 2048 tokens),
  - and every other technical check passes: resume, determinism, the 2+2 canary, and no visible thinking.
- **REVIEW:** 18/20 parse and every other check passes.
- **NO-GO:** fewer than 18/20 parse, or any technical or truncation problem.

Unparseable outputs are never regenerated just because they are unparseable.

## 5. Freezing Stage-1 outcomes

`07_freeze_stage1_failures.py` runs only on a complete, verified run. It defines **failure := outcome != repair**,
which makes Stage-2 eligibility outcome-adaptive by design. It writes the failure set and the verbatim failed
responses, hashes both, and locks them. **Truncated Stage-1 generations are observed failures** and are carried into
Stage 2 if they pass review. The pre-specified sensitivity analysis excluding them is kept.

**Failure-count gate:** at least 50 Stage-1 failures. Otherwise the exact count is reported, along with the number
of additional items needed at the observed rate. No further eligible ProcessBench items exist under the frozen rules.

## 6. Blinded annotation-fidelity gate (between Stage 1 and Stage 2)

1. **Export** (`07a`). The packet covers only the frozen failed items, in seeded random order. For each item it shows
   the problem, the numbered original solution, the gold answer, the original final answer, the annotated
   first-error step k, and the proposed false step k−1. **It shows no Stage-1 model response, answer or outcome**
   (checked before writing). The packet and blank form are hashed.
2. **Review** (by the researcher; Claude's review is not human validation). For each item:
   - `first_error_verdict`: CONFIRM / DISAGREE / UNCLEAR, with an optional reviewer step that is recorded only;
   - `false_step_verdict`: CORRECT / NOT_CORRECT / UNCLEAR.
3. **Freeze** (`07b`). The form must exactly match the export. The pre-specified rules are:
   - **primary Stage-2 set** = CONFIRM. Other items are excluded from **all** Stage-2 conditions, so pairing is
     preserved, and prompts are never re-pointed.
   - **L_false** only where the false step is CORRECT. Otherwise the item is ineligible only for L_false.
   - If fewer than 50 items pass, STOP and report.

   Decisions are hashed and locked **before any Stage-2 prompt exists**. `07b` refuses once Stage-2 requests exist,
   and `08` refuses without the lock. Stage-2 outcomes are never used for exclusions.

## 7. Stage 2 (new stateless requests; primary set × conditions)

```
Problem / Original proposed solution / (as Stage 1)

Feedback on the original proposed solution:
The final answer in the proposed solution is wrong.

Your previous repair attempt:
----- BEGIN PREVIOUS REPAIR ATTEMPT -----
{the item's exact Stage-1 response, outer whitespace stripped - identical in every condition}
----- END PREVIOUS REPAIR ATTEMPT -----

Feedback:
Your previous repair attempt is still incorrect. {SECOND SENTENCE}

Assess the original proposed solution carefully and give your best corrected final answer.

Return the final answer on the last line exactly as:

FINAL_ANSWER: <answer>
```

The feedback is exactly two sentences. **Sentence 1 is identical for every condition. Sentence 2 alone defines the
condition** and is the only text that differs between an item's prompts (audited and tested):

| Condition | Sentence 2 |
|---|---|
| V -> V | Reassess the original proposed solution and your previous repair carefully. |
| V -> N_true | Pay special attention to Step {k} of the ORIGINAL proposed solution. |
| V -> L_true | The first mistake in the ORIGINAL proposed solution is in Step {k}. |
| V -> L_false | The first mistake in the ORIGINAL proposed solution is in Step {k−1}. |

Stage-2 smoke gate: 3 reviewed failures × conditions. GO if at most 1 response is unparsed, REVIEW if 2, NO-GO
otherwise or on any technical or truncation failure.

## 8. Outcomes and statistics

- **Rescue(c):** Stage-2 repairs / primary-set items that received c.
- **FinalRepair(V->c):** (Stage-1 repairs + Stage-2 repairs) / the analysis population, i.e. the pool minus
  review-excluded failures. A conservative full-pool variant counts review-excluded failures as unrepaired.
- **Primary:** Delta_rescue = Rescue(L_true) − Rescue(VV), paired over the primary set.
- **Paired statistics** for each comparison: both / A-only / B-only / neither, an exact McNemar test, and a paired
  bootstrap 95% percentile CI (10,000 resamples, seed 4242).
- **Separate diagnostic-content contrast:** V->L_true vs V->L_false, on items where L_false is eligible.
- **Secondary comparisons:** N_true vs VV, L_false vs VV, L_true vs N_true.
- **Strata:** domain and MATH level.
- **Sensitivities:** excluding truncated/unparsed VV or L_true outputs; excluding truncated Stage-1 outputs.
- **Also reported:** retention, new wrong answers, parse failures, truncation, format compliance, tokens, calls per
  item, and the unnecessary second-call rate (0 by design).

## 9. Interpretation rule (FROZEN: `data/manifests/INTERPRETATION_RULE_FROZEN.json`)

For Delta_rescue = Rescue(V->L_true) − Rescue(V->V) with its paired 95% CI:

- **STRONG:** CI lower bound > 0 AND point estimate ≥ +10 pp.
- **NO SUPPORT:** CI upper bound < +10 pp.
- **INCONCLUSIVE:** otherwise.

The L_true vs L_false contrast, the false-location gain, the robustness checks and the strata are reported but do
**not** enter the label. The lock hashes the rule together with the analysis settings that define the CI. Full
Stage-2 inference refuses to start if the rule or those settings change.

## 10. Workflow

**Phase 1 (Kaggle).**
1. Stage-1 smoke test.
2. Approval.
3. Full Stage 1 (265 requests).
4. Preserve, score, freeze and hash the failures.
5. **STOP** and report.

**Between phases (local).**
1. Verify the archive.
2. Re-freeze the failure set (byte-identical).
3. Export the blinded packet.
4. Researcher review.
5. Freeze the decisions.
6. **STOP** and report.

**Phase 2 (after approval).**
1. Build the Stage-2 requests.
2. New bundle.
3. Stage-2 smoke test.
4. Full Stage 2.
5. Analysis.
6. Separate FINAL archives for each stage.

### Amendment A7 (2026-10-14): Stage-2 smoke-gate clarification
"A Stage-2 response reaching the frozen 2048-token cap is not automatically a technical smoke NO-GO when the request
completed normally and the output was correctly persisted. Such an output is genuine model behavior and remains a
failed repair if no valid final answer parses. Smoke GO/NO-GO should be based on pipeline/integrity criteria rather
than requiring every model response to finish before the token cap." Basis: the v11 DIAGNOSTIC-ONLY run (sb_math-355
VV still looping at 4096 tokens). max_new_tokens stays 2048; requests, order, smoke selection, eligible set, prompts,
parser, scorer, conditions, primary endpoint and interpretation rule are unchanged. Full Stage 2 requires a new smoke
archive with GO under this gate.
