# Code structure for the 40-item pilot

This file is a short map of which Python file is responsible for which stage.

## 1. `02_DATA_CONSTRUCTION_CODE/build_m1_candidates.py`

Purpose: parse GSM8K worked solutions and establish an explicit calculation/dependency structure.

Key code locations:

- line 244 — `parse_trace(...)`: parses the worked answer into arithmetic calculations and verifies arithmetic when strict mode is enabled.
- line 319 — `build_graph(...)`: traces operands to the question or earlier calculations and records dependencies.
- line 379 — `descendants(...)`: returns all later calculations that directly or indirectly depend on a chosen calculation.

This is the structural foundation reused by the M3 pipeline.

## 2. `02_DATA_CONSTRUCTION_CODE/build_m3_candidates.py`

Purpose: generate candidate controlled M3 arithmetic slips and validate them.

Important constants:

```python
MIN_ERROR_STEP = 2
DELTAS = [1, -1, 2, -2, 10, -10]
MAX_RELATIVE_DIFFERENCE = Fraction(1, 4)
FINAL_RATIO_BAND = (Fraction(1, 2), Fraction(2))
```

Key code locations:

- line 179 — `make_m3(...)`: changes the result at one eligible arithmetic step and recomputes dependency descendants.
- line 389 — `validate_m3(...)`: checks that the intended M3 step is the only root arithmetic error, that dependent later calculations are recomputed, and that unrelated calculations are unchanged.
- line 541 — early 10-candidate development/review workflow.

Important: `N_CANDIDATES = 10` here belongs to the early pipeline-development stage, not to the final 40-item pilot size.

## 3. `02_DATA_CONSTRUCTION_CODE/build_m3_pilot40.py`

Purpose: produce the **final frozen 40-item pilot**.

Important lines:

```python
36: SEED = 42
37: N_ITEMS = 40
38: TARGET_PER_DIRECTION = 20
40: MAGNITUDE_CAPS = {1: 18, 2: 14, 10: 10}
41: OPERATOR_CAP = 15
42: ERROR_STEP_CAP = 15
```

- line 542 — `main()`: scans GSM8K train, uses the structural and M3 machinery, applies the final quality filters and diversity constraints, and writes `m3_pilot_40.json`.
- lines 52–54 explicitly note that the recorded gold-text judgments were made by the coding assistant and that human review was still pending.

This is the main file to show when asked, “Which code selected the final 40?”

## 4. `03_PROMPTS/build_conditions.py`

Purpose: take the already-frozen 40 corrupted items and create the experimental prompt conditions.

Important lines:

```python
39: VERIFY = "The final answer in the proposed solution is wrong."
40: LOCATE = "The first mistake is in Step {k}."
41: ORACLE = "The correct final answer is {a}."
```

Condition logic:

```python
45-46: L0 -> no feedback
47-48: V  -> verification only
49-50: L  -> verification + LOCATE(true_step)
51-52: L_false -> verification + LOCATE(false_step)
53-54: A  -> correct answer supplied
```

- line 58 — `build_prompt(...)`: inserts the feedback into the common prompt template.

This is the file to show when asked, “Where do we actually tell Qwen that the first mistake is in a particular step?”

## 5. `04_INFERENCE_AND_SCORING_CODE/run_pilot.py`

Purpose: execute the frozen requests against the selected backend/model and save responses durably.

- line 306 — command-line `main(...)` runner.
- raw responses are written during the run rather than waiting until the end.

Supporting files:

- `pilot_common.py`: frozen input paths/hashes, request/order utilities and crash-safe JSONL handling.
- `backends.py`: model/backend adapters, including Hugging Face Transformers.

## 6. `04_INFERENCE_AND_SCORING_CODE/parse_answers.py`

Purpose: extract the model's final answer.

- line 74 — strict `FINAL_ANSWER` parser
- line 116 — numeric fallback parser
- line 142 — parse one response
- line 146 — parse a complete run

## 7. `04_INFERENCE_AND_SCORING_CODE/score_pilot.py`

Purpose: compare parsed answers with the original GSM8K gold answer.

- line 40 — score one row
- line 90 — score a complete run and write summary / paired tables

## Complete dependency flow

```text
build_m1_candidates.py
       |
       v
build_m3_candidates.py
       |
       v
build_m3_pilot40.py
       |
       v
m3_pilot_40.json
       |
       v
build_conditions.py
       |
       v
m3_pilot_40_conditions.jsonl
       |
       v
run_pilot.py + backends.py + pilot_common.py
       |
       v
raw_outputs
       |
       v
parse_answers.py
       |
       v
parsed_outputs
       |
       v
score_pilot.py
       |
       v
scored_outputs + summary + paired_primary.csv
```
