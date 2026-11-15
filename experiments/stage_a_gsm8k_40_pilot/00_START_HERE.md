# Information or Placebo? — 40-item GSM8K feasibility pilot

## Purpose of this folder

This folder contains the self-contained materials for the **initial 40-item GSM8K feasibility pilot**. It is intended to make the complete provenance easy to audit:

1. where the 40 problems came from;
2. how a single controlled arithmetic error was inserted;
3. how downstream dependent steps were recomputed;
4. how the feedback conditions were constructed;
5. what Qwen3-4B actually received;
6. where model outputs were stored, parsed and scored; and
7. how the reported pilot repair rates were obtained.

This pack deliberately focuses on the **40-item pilot only**. Later experiments are not included.

---

## One-page pipeline

```text
GSM8K training split: 7,473 problems
        |
        v
build_m1_candidates.py
Parse calculations + verify arithmetic + build dependencies
        |
        v
Reject ambiguous / structurally unsuitable problems
        |
        v
build_m3_candidates.py
Generate candidate M3 arithmetic slips
+ recompute only dependent later calculations
+ realism checks + automatic validation
        |
        v
build_m3_pilot40.py
Fixed-seed, diversity-aware final selection
        |
        v
40 controlled corrupted problems
m3_pilot_40.json
        |
        v
build_conditions.py
Create L0 / V / L / L_false / A prompts
        |
        v
Qwen/Qwen3-4B
        |
        v
run_pilot.py
        |
        v
raw outputs -> parse_answers.py -> score_pilot.py
        |
        v
repair rates and paired tables
```

---

## Where the 40 examples came from

The pilot did **not** take the first 40 GSM8K rows and did **not** first randomly select 40 raw questions and then try to corrupt them.

The construction pipeline scanned the GSM8K training split and first required an unambiguous arithmetic/dependency structure. It then tested whether a controlled M3 calculation slip could be created while preserving the experiment's constraints. The recorded build statistics are:

- GSM8K training problems examined: **7,473**
- structurally eligible problems: **2,200**
- problems with at least one automatically valid M3 corruption: **268**
- final pilot items: **40 distinct source problems**
- final direction balance: **20 under / 20 over**
- error-step distribution: Step 2 = 15, Step 3 = 15, Step 4 = 9, Step 5 = 1
- all final automatic validation checks passed; recorded leakage violations = 0

The final-selection script is `02_DATA_CONSTRUCTION_CODE/build_m3_pilot40.py`. Its key constants are `SEED = 42` and `N_ITEMS = 40`.

### Important clarification about `N_CANDIDATES = 10`

`build_m1_candidates.py` and `build_m3_candidates.py` contain an earlier **10-candidate development/review stage**. That is not the final pilot size. The final 40-item set is created by `build_m3_pilot40.py`, where `N_ITEMS = 40`.

---

## What an M3 error means

M3 is a **calculation slip**. At the injected step:

- the operands stay the same;
- the operator stays the same;
- the meaning and units stay the same;
- only the numerical result is changed to a nearby wrong result.

The candidate generator tries offsets such as `+1, -1, +2, -2, +10, -10`, subject to realism and structural checks. Later steps are changed **only if they depend on the corrupted value**, and those later steps are recomputed correctly using the wrong upstream value. This creates one causal first arithmetic error rather than several independent errors.

See `01_DATA/ONE_WORKED_EXAMPLE.md` for an end-to-end example.

---

## The five primary feedback conditions

For every one of the same 40 corrupted traces:

| Condition | Feedback added |
|---|---|
| `L0` | no feedback |
| `V` | “The final answer in the proposed solution is wrong.” |
| `L` | verification + the **true first-error step** |
| `L_false` / `L_false_near` | same wording, but points to a verified correct earlier step |
| `A` | supplies the correct final answer |

The exact prompt template and condition wording are constructed in `03_PROMPTS/build_conditions.py`.

Useful lines in that file:

- line 39: verification sentence
- line 40: `LOCATE = "The first mistake is in Step {k}."`
- line 50: true-location condition uses `true_step`
- line 52: false-location condition uses `false_step`

The frozen rendered prompts are in `03_PROMPTS/m3_pilot_40_conditions.jsonl`.

---

## What Qwen received

Each model request was stateless: one user prompt, no conversation history, and no experimental metadata such as item ID, gold answer, or the label `condition=L`.

The condition is already encoded in the **prompt text**. For example, under `L`, the prompt literally contains:

```text
The final answer in the proposed solution is wrong.
The first mistake is in Step 2.
```

Qwen receives that finished prompt, not the bookkeeping field that says which experimental condition it belongs to.

---

## Model and run setup

Main run:

- model: **Qwen/Qwen3-4B**
- pinned revision: `3101254bbe4169895668a0e7653c3fd1f313576e`
- FP16 on Kaggle T4 (`cuda:0`)
- greedy / deterministic decoding
- thinking disabled through the official chat-template switch
- `max_new_tokens = 768`
- batch size 1
- independent stateless requests

The exact run configuration, environment, provenance, execution order and full-run notebook are under `06_REPRODUCIBILITY/`.

---

## Primary 40-item pilot results

| Condition | Correct repairs | Repair rate |
|---|---:|---:|
| L0 | 20 / 40 | **50.0%** |
| V | 32 / 40 | **80.0%** |
| L_false_near | 22 / 40 | **55.0%** |
| L | 33 / 40 | **82.5%** |
| A | 37 / 40 | **92.5%** |

Main descriptive contrasts:

- verification effect: `V - L0 = +30.0 percentage points`
- incremental true-location benefit: `L - V = +2.5 points`
- false-location effect: `L_false - V = -25.0 points`
- true-vs-false location gap: `L - L_false = +27.5 points`

### Pilot interpretation

The pilot suggests that **generic verification is already a strong intervention** on this small controlled set. The true error location adds little beyond verification in this pilot, whereas a false specific location can substantially reduce repair. This is a feasibility result, not a universal conclusion.

---

## Where to see the exact Qwen outputs

Open:

`05_RESULTS/raw_outputs_PRIMARY_200.jsonl`

This contains the exact raw model response for the 40 items under the five primary conditions (200 requests total).

Then:

- `parsed_outputs_PRIMARY_200.jsonl` = extracted final answers
- `scored_outputs_PRIMARY_200.jsonl` = whether each answer matched the GSM8K gold answer
- `paired_primary.csv` = compact per-item comparison across the five primary conditions
- `PRIMARY_RESULTS_SUMMARY.csv` = simple summary table

The original run also contained a separate 25-item far-false sensitivity analysis. It is deliberately not foregrounded in this supervisor pack; `original_full_run_summary.json` is retained so the original run record is not altered.

---

## Validation and semantic-review note

The final pilot contains extensive automatic checks: arithmetic consistency, one-root-error structure, unchanged pre-error steps, preserved operands/operators, correct downstream propagation, final-answer change, text/annotation consistency and leakage checks.

There was also a **semantic-plausibility / gold-text review during dataset construction** to remove technically valid but implausible or awkward cases. The recorded reviewer for those judgments was **Claude (coding assistant)**. This should **not** be described as independent human validation of all 40 items; the source code explicitly records that the human review gate was still pending at construction time.

---

## Recommended reading order for a supervisor

1. `00_START_HERE.md` — overall study and results
2. `01_DATA/ONE_WORKED_EXAMPLE.md` — one complete example
3. `01_CODE_STRUCTURE.md` — exact file/function map
4. `01_DATA/m3_pilot_40.json` — frozen 40-item dataset
5. `02_DATA_CONSTRUCTION_CODE/build_m3_pilot40.py` — final selection logic
6. `03_PROMPTS/build_conditions.py` — feedback conditions
7. `03_PROMPTS/m3_pilot_40_conditions.jsonl` — exact rendered prompts
8. `05_RESULTS/raw_outputs_PRIMARY_200.jsonl` — exact Qwen responses
9. `05_RESULTS/scored_outputs_PRIMARY_200.jsonl` — exact correctness decisions
10. `06_REPRODUCIBILITY/` — model/run provenance if needed

