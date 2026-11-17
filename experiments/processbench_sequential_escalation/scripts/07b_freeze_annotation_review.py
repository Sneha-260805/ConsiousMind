"""Validate and FREEZE the blinded annotation-review decisions BEFORE any Stage-2 prompt is built.

    python scripts/07b_freeze_annotation_review.py --reviewer "<name>"
    python scripts/07b_freeze_annotation_review.py --reviewer "TEST" --work-dir outputs/test_runs/<x>/stage2_inputs
Reads <review_dir>/annotation_review_form_FILLED.csv; it must contain exactly the exported items with unedited step
columns and valid verdicts. Applies the pre-specified rules (src/annotation_review.py), writes
annotation_review_decisions.json and ANNOTATION_REVIEW_FROZEN.json (sha256 of filled form + decisions + packet +
failure set). Once frozen, decisions never change. Refuses if any Stage-2 request file already exists.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import annotation_review as ar
from src import common as C
from src import stage2_runner as s2r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    lock = s2r.verify_lock(P)
    if P["review_lock"].exists():
        raise SystemExit("STOP: annotation review already frozen; decisions are never changed")
    if P["requests"].exists():
        raise SystemExit("STOP: Stage-2 requests already exist; the review must be frozen before they are built")
    ex = C.read_json(P["review_export_lock"])
    d = P["review_dir"]
    for name, key in (("annotation_review_packet.md", "packet_sha256"), ("annotation_review_form.csv", "blank_form_sha256")):
        if C.sha256_file(d / name) != ex[key]:
            raise SystemExit(f"STOP: exported {name} was modified")
    if ex["failures_sha256"] != lock["failures_sha256"]:
        raise SystemExit("STOP: review packet was exported for a different failure set")
    filled = d / "annotation_review_form_FILLED.csv"
    if not filled.exists():
        raise SystemExit(f"STOP: {filled} not found (copy the blank form to this name and fill it)")
    decisions, probs = ar.validate_and_decide(ar.read_form(filled), ar.read_form(d / "annotation_review_form.csv"), cfg)
    if probs:
        raise SystemExit("STOP: review form invalid:\n" + "\n".join(probs[:30]))
    summ = ar.summary(decisions, cfg)
    doc = {"TEST_ONLY": ex["TEST_ONLY"], "reviewer": a.reviewer, "frozen": C.now(),
           "rules": cfg["annotation_review"]["decision_rules"], "summary": summ,
           "decisions": [decisions[k] for k in sorted(decisions)]}
    dsha = C.write_json(P["review_decisions"], doc)
    rl = {"frozen": C.now(), "TEST_ONLY": ex["TEST_ONLY"], "reviewer": a.reviewer,
          "failures_sha256": lock["failures_sha256"], "packet_sha256": ex["packet_sha256"],
          "filled_form_sha256": C.sha256_file(filled), "decisions_sha256": dsha, "summary": summ,
          "rule": "Frozen before any Stage-2 prompt; Stage-2 outcomes are never used for exclusions."}
    C.write_json(P["review_lock"], rl)
    if P["real"]:
        C.update_frozen_hashes({C.rel(filled): rl["filled_form_sha256"], C.rel(P["review_decisions"]): dsha,
                                C.rel(P["review_lock"]): C.sha256_file(P["review_lock"])})
    print(json.dumps({**summ, "decisions_sha256": dsha,
                      "NEXT": "report to the researcher; then scripts/08_build_stage2_requests.py" if summ["primary_gate_passed"]
                      else f"STOP: fewer than {summ['min_primary_items']} items passed review - report to the researcher"},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
