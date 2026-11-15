# Stage A — 40-item GSM8K Controlled Pilot

This folder contains the complete Stage A work contributed by Swetha for the ConsciousMind project.

## Research purpose
This controlled feasibility pilot tests how different forms of diagnostic feedback affect LLM reasoning repair on deliberately corrupted GSM8K reasoning traces.

## Conditions
- `L0`: no feedback
- `V`: final answer is wrong
- `L`: true first-error location
- `L_false`: false earlier location
- `A`: correct final answer supplied
- Far-false sensitivity subset is included where available.

## Main results
- L0: 20/40 = 50.0%
- V: 32/40 = 80.0%
- L: 33/40 = 82.5%
- L_false: 22/40 = 55.0%
- A: 37/40 = 92.5%

Derived effects:
- Verification effect: +30.0 percentage points (`V - L0`)
- True-location gain beyond verification: +2.5 pp (`L - V`)
- False-location effect relative to verification: -25.0 pp (`L_false - V`)
- Correct-location advantage over false location: +27.5 pp (`L - L_false`)

## Folder guide
- `01_DATA/`: frozen pilot data and worked example
- `02_DATA_CONSTRUCTION_CODE/`: deterministic GSM8K parsing and M3 corruption construction
- `03_PROMPTS/`: feedback-condition construction and frozen prompt files
- `04_INFERENCE_AND_SCORING_CODE/`: model backend, runner, answer parser, and scorer
- `05_RESULTS/`: scored and raw outputs plus paired results
- `06_REPRODUCIBILITY/`: model config, execution order, provenance, environment, notebook, and logs
- `00_START_HERE.md`: original supervisor-facing guide
- `SHA256SUMS.txt`: integrity hashes

## Scope note
This folder contains only the controlled 40-item Stage A pilot. Reliability-gating and ProcessBench sequential-escalation experiments should be contributed separately by their owners under sibling experiment folders.
