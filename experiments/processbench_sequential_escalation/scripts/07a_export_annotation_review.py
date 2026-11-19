"""Export the BLINDED annotation-fidelity review packet for the frozen Stage-1 failures (no model output shown).

    python scripts/07a_export_annotation_review.py
    python scripts/07a_export_annotation_review.py --work-dir outputs/test_runs/<x>/stage2_inputs     # TEST ONLY
Requires the frozen failure set (07). Writes <review_dir>/annotation_review_packet.md and
<review_dir>/annotation_review_form.csv (blank), hashes both in ANNOTATION_REVIEW_EXPORT.json. The packet shows only the
problem, the numbered original solution, gold answer, original final answer, annotated first-error step and proposed
false step; it never contains a Stage-1 response, Stage-1 answer or Stage-1 outcome (checked before writing).
Next: the reviewer copies the form to annotation_review_form_FILLED.csv, fills it, then 07b freezes it.
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
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    s2r.verify_lock(P)
    if P["review_lock"].exists():
        raise SystemExit("STOP: the annotation review is already frozen; the packet is never re-exported")
    doc = C.read_json(P["failures"])
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    packet, form = ar.build_packet(pool, doc, cfg, doc["TEST_ONLY"])
    resp = C.read_jsonl(P["responses"])
    leaks = [r["item_id"] for r in resp if len(r["stage1_response"].strip()) > 20
             and r["stage1_response"].strip()[:200] in packet]
    lowered = packet.lower()
    for word in ("stage 1", "stage-1", "repair attempt", "previous attempt", "retention", "parse_failure",
                 "new_wrong_answer", "stage1"):
        if word in lowered:
            leaks.append(f"term:{word}")
    if leaks:
        raise SystemExit(f"STOP: review packet would leak Stage-1 information: {leaks[:5]}")
    d = P["review_dir"]
    d.mkdir(parents=True, exist_ok=True)
    pk, fm = d / "annotation_review_packet.md", d / "annotation_review_form.csv"
    if P["review_export_lock"].exists():
        old = C.read_json(P["review_export_lock"])
        if old["packet_sha256"] != C.sha256_text(packet) or old["blank_form_sha256"] != C.sha256_text(ar.form_csv(form)):
            raise SystemExit("STOP: a different review packet was already exported")
    pk.write_text(packet, encoding="utf-8", newline="\n")
    fm.write_text(ar.form_csv(form), encoding="utf-8", newline="\n")
    lock = {"exported": C.now(), "TEST_ONLY": doc["TEST_ONLY"], "n_items": len(form),
            "failures_sha256": C.read_json(P["lock"])["failures_sha256"],
            "packet_sha256": C.sha256_file(pk), "blank_form_sha256": C.sha256_file(fm),
            "blinding": "no Stage-1 response, answer or outcome; seeded random item order",
            "packet_order_seed": cfg["annotation_review"]["packet_order_seed"]}
    C.write_json(P["review_export_lock"], lock)
    if P["real"]:
        C.update_frozen_hashes({C.rel(pk): lock["packet_sha256"], C.rel(fm): lock["blank_form_sha256"],
                                C.rel(P["review_export_lock"]): C.sha256_file(P["review_export_lock"])})
    print(json.dumps({**lock, "packet": str(pk), "form": str(fm),
                      "NEXT": f"reviewer: copy {fm.name} to annotation_review_form_FILLED.csv in the same folder, fill "
                              "first_error_verdict / false_step_verdict for every row, then run "
                              "scripts/07b_freeze_annotation_review.py --reviewer '<name>'"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
