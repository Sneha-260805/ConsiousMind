"""A5: export the BLINDED Pass-1 view for the human annotation-fidelity review of the 76 frozen Stage-1 failures.

    python scripts/18_export_human_review.py
    python scripts/18_export_human_review.py --work-dir outputs/test_runs/<x>/stage2_inputs     # TEST ONLY
Requires the frozen failure set (07), the exported A3 blank form (07a; it is filled only at the Pass-2 freeze) and,
for the real export, amendment A5 registered. Writes data/processed/human_review/pass1_view.json (ONLY item_id,
problem, numbered original solution, gold answer, original final answer - audited, incl. a mutation test showing the
view does not depend on any annotation field and a check for Stage-1 response text) + REVIEWER_INSTRUCTIONS.md, hashed
in HUMAN_REVIEW_EXPORT.json. Re-running is a byte-identical no-op. The Pass-2 view is NOT written here.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import human_review as hr
from src import stage2_runner as s2r

INSTRUCTIONS = """# Stage B - blinded HUMAN annotation-fidelity review (amendment A5)

You review the {n} frozen items in two passes, in a fixed order, using the local review page:

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
Question: {q1}
Record: **earliest_error_step** = the step number, or `NO_ERROR`, or `UNCLEAR`; **confidence** HIGH / MEDIUM / LOW;
optional short **reason** (max {maxr} characters).

When all {n} items are saved:  `python scripts/20_freeze_human_review.py --pass 1 --reviewer "<your name>"`
(Pass 1 is locked; only then is it compared with the annotation, and the Pass-2 page becomes available.)

## Pass 2 - candidate step (you see: problem, Steps 1..f only, and candidate Step f)
Question: {q2}
Record: **verdict** CORRECT / NOT_CORRECT / UNCLEAR; **confidence**; optional **reason**.

    python scripts/19_human_review_server.py --pass 2 --reviewer "<your name>"
    python scripts/20_freeze_human_review.py --pass 2 --reviewer "<your name>"

Then STOP and report. Decisions (07b) and Stage 2 only after researcher approval.

Notes: use one reviewer name throughout. Do not edit the log files by hand (they are hash-chained; any edit makes
the freeze refuse). OneDrive: let sync finish before freezing; never keep two copies of a log.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = hr.paths(a.work_dir)
    lock = s2r.verify_lock(P)
    if P["real"]:
        am = [x["id"] for x in C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]]
        if "A5" not in am:
            raise SystemExit("STOP: amendment A5 is not registered (scripts/register_protocol_amendment.py)")
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + ["src/human_review.py", "src/llm_review.py"])
    for k in ("review_lock", "requests"):
        if P[k].exists():
            raise SystemExit(f"STOP: {P[k].name} exists; the human review must precede decisions / Stage 2")
    ex = C.read_json(P["review_export_lock"])
    if C.sha256_file(P["review_dir"] / "annotation_review_form.csv") != ex["blank_form_sha256"] or \
            ex["failures_sha256"] != lock["failures_sha256"]:
        raise SystemExit("STOP: exported blank form changed / belongs to another failure set")
    if (P["review_dir"] / "annotation_review_form_FILLED.csv").exists():
        raise SystemExit("STOP: annotation_review_form_FILLED.csv already exists")
    if P["pass_lock"][1].exists():
        raise SystemExit("STOP: Pass 1 is already frozen; the view is never re-exported")

    doc = C.read_json(P["failures"])
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    view = hr.pass1_view_doc(pool, doc, cfg, bool(doc["TEST_ONLY"]))
    probs = hr.audit_view(view, pool, C.read_jsonl(P["responses"]))
    if probs:
        raise SystemExit(f"STOP: Pass-1 view failed the blinding audit: {probs[:5]}")
    hc = cfg["human_review"]
    text = INSTRUCTIONS.format(n=view["n_items"], q1=hr.QUESTIONS[1], q2=hr.QUESTIONS[2], maxr=hc["reason_max_chars"])
    v_sha, v_status = C.write_frozen(P["view"][1], C.write_json, view)
    i_sha, _ = C.write_frozen(P["instructions"], lambda p, t: p.write_text(t, encoding="utf-8", newline="\n"), text)
    el = {"exported": C.now(), "amendment": "A5", "TEST_ONLY": view["TEST_ONLY"], "n_items": view["n_items"],
          "failures_sha256": lock["failures_sha256"], "blank_form_sha256": ex["blank_form_sha256"],
          "packet_order_seed": cfg["annotation_review"]["packet_order_seed"],
          "item_ids_sha256": C.sha256_text("\n".join(view["item_ids"])),
          "pass1_view_sha256": v_sha, "instructions_sha256": i_sha,
          "pass1_fields_shown": list(hr.PASS1_ITEM_KEYS),
          "blinding_audit": "passed (exact keys; mutation test vs annotation fields; no Stage-1 response text)"}
    if P["export_lock"][1].exists():
        old = C.read_json(P["export_lock"][1])
        if (old["pass1_view_sha256"], old["instructions_sha256"]) != (v_sha, i_sha):
            raise SystemExit("STOP: a different Pass-1 view was already exported")
        el = old
    else:
        C.write_json(P["export_lock"][1], el)
    if P["real"]:
        C.update_frozen_hashes({C.rel(P["view"][1]): v_sha, C.rel(P["instructions"]): i_sha,
                                C.rel(P["export_lock"][1]): C.sha256_file(P["export_lock"][1])})
    print(json.dumps({**el, "pass1_view": C.rel(P["view"][1]), "view_status": v_status,
                      "instructions": C.rel(P["instructions"]),
                      "NEXT": 'python scripts/19_human_review_server.py --pass 1 --reviewer "<name>"'}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
