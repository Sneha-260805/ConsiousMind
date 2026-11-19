# Stage B - blinded HUMAN annotation-fidelity review (amendment A5)

You review the 76 frozen items in two passes, in a fixed order, using the local review page:

    python scripts/19_human_review_server.py --pass 1 --reviewer "<your name>"

then open http://127.0.0.1:8765/ (the script prints the address). Every **Save** is written to disk at once,
with a timestamp, in an append-only log; you can stop at any time (close the page, Ctrl+C the script) and resume
later with the same command. Re-saving an item records a revision; earlier saves are kept.

## Blinding - please do NOT open, until BOTH passes are frozen
- `data/processed/stage_b_items.json`, `candidates_all.jsonl`, `data/raw/*` (contain the ProcessBench labels)
- `data/processed/annotation_review/annotation_review_packet.md` / `annotation_review_form.csv` (they show the
  annotated step)
- anything about Stage 1 (`stage1_*`, `data/stage1_records/`), the LLM review (`data/processed/llm_review/`,
  `data/smoke_records/`) or Stage 2
- `pass1_mapping.json` (written when Pass 1 is frozen) - leave it closed until Pass 2 is frozen too

## Pass 1 - earliest error (you see: problem, numbered original solution, gold answer, the solution's final answer)
Question: Inspect the reasoning from Step 1 onward. Identify the EARLIEST step that contains a substantive reasoning, arithmetic, algebraic, or logical error. Do not choose a later step that merely propagates an earlier error.
Record: **earliest_error_step** = the step number, or `NO_ERROR`, or `UNCLEAR`; **confidence** HIGH / MEDIUM / LOW;
optional short **reason** (max 500 characters).

When all 76 items are saved:  `python scripts/20_freeze_human_review.py --pass 1 --reviewer "<your name>"`
(Pass 1 is locked; only then is it compared with the annotation, and the Pass-2 page becomes available.)

## Pass 2 - candidate step (you see: problem, Steps 1..f only, and candidate Step f)
Question: Is this candidate step itself correct given the problem and all valid reasoning before it? Judge the step itself, not whether later steps are correct.
Record: **verdict** CORRECT / NOT_CORRECT / UNCLEAR; **confidence**; optional **reason**.

    python scripts/19_human_review_server.py --pass 2 --reviewer "<your name>"
    python scripts/20_freeze_human_review.py --pass 2 --reviewer "<your name>"

Then STOP and report. Decisions (07b) and Stage 2 only after researcher approval.

Notes: use one reviewer name throughout. Do not edit the log files by hand (they are hash-chained; any edit makes
the freeze refuse). OneDrive: let sync finish before freezing; never keep two copies of a log.
