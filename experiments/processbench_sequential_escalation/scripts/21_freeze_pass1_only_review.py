"""A6: fill the annotation-review form from the FROZEN human Pass 1 only (separate Pass-2 recheck waived). STOP.

    python scripts/21_freeze_pass1_only_review.py --reviewer "<Pass-1 reviewer>"
    python scripts/21_freeze_pass1_only_review.py --reviewer "<name>" --work-dir outputs/test_runs/<x>/stage2_inputs  # TEST
Pass 1 is never reopened or altered: every Pass-1 file (view, log, final judgments, mapping, locks) is verified against
its registered sha256 (real run) and against the Pass-1 lock, and nothing is written to them. No Pass-2 judgment may
exist. Fills annotation_review_form_FILLED.csv (the exported blank form):
    first_error_verdict        = the frozen Pass-1 mapping (CONFIRM / DISAGREE / UNCLEAR)
    reviewer_first_error_step  = the reviewer's step for DISAGREE (recorded only, never substituted)
    false_step_verdict         = PASS1_IMPLIED for CONFIRM items ("Pass-1-implied false-step validity", NOT
                                 independently Pass-2-validated), NOT_REVIEWED otherwise
+ human_review_judgments.json + HUMAN_REVIEW_FROZEN.json (hashed; registered for the real run). Does NOT run 07b and
never builds Stage-2 requests.
"""
import argparse
import csv
import io
import json
import sys
from collections import Counter

import _bootstrap  # noqa: F401
from src import annotation_review as ar
from src import common as C
from src import human_review as hr
from src import stage2_runner as s2r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = hr.paths(a.work_dir)
    lock_s1 = s2r.verify_lock(P)
    waiver = cfg["annotation_review"]["pass2_waiver_A6"]
    pass1_files = [P["view"][1], P["log"][1], P["final"][1], P["mapping"], P["pass_lock"][1], P["export_lock"][1],
                   P["view"][2], P["export_lock"][2]]
    if P["real"]:
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + ["src/human_review.py"] + [C.rel(p) for p in pass1_files])
    before = {C.rel(p): C.sha256_file(p) for p in pass1_files}
    for k in ("review_lock", "requests"):
        if P[k].exists():
            raise SystemExit(f"STOP: {P[k].name} exists; decisions must be frozen before Stage 2")
    if P["log"][2].exists() or P["pass_lock"][2].exists():
        raise SystemExit("STOP: Pass-2 judgments exist; A6 (Pass 2 waived) does not apply")
    l1 = C.read_json(P["pass_lock"][1])
    el2 = C.read_json(P["export_lock"][2])
    if (C.sha256_file(P["log"][1]), C.sha256_file(P["final"][1])) != (l1["log_sha256"], l1["final_sha256"]) or \
            el2["pass1_lock_sha256"] != C.sha256_file(P["pass_lock"][1]) or \
            el2["pass1_mapping_sha256"] != C.sha256_file(P["mapping"]):
        raise SystemExit("STOP: frozen Pass-1 log / final / lock / mapping are not mutually consistent")
    if l1["reviewer"] != a.reviewer.strip():
        raise SystemExit(f"STOP: Pass 1 was reviewed by {l1['reviewer']!r}, not {a.reviewer.strip()!r}")
    hr.read_log(P["log"][1])                                                   # hash chain intact
    ex = C.read_json(P["review_export_lock"])
    blank_path = P["review_dir"] / "annotation_review_form.csv"
    filled_path = P["review_dir"] / "annotation_review_form_FILLED.csv"
    if C.sha256_file(blank_path) != ex["blank_form_sha256"] or ex["failures_sha256"] != lock_s1["failures_sha256"]:
        raise SystemExit("STOP: exported blank form changed / belongs to another failure set")
    if filled_path.exists():
        raise SystemExit(f"STOP: {filled_path} already exists; it is never overwritten")

    pool = {i["item_id"]: i for i in C.read_json(C.ITEMS_FILE)["pool"]}
    f1 = {r["item_id"]: r for r in C.read_json(P["final"][1])["rows"]}
    mp = {r["item_id"]: r for r in C.read_json(P["mapping"])["rows"]}
    order = C.read_json(P["view"][1])["item_ids"]
    recs = []
    for iid in order:
        it, m = pool[iid], mp[iid]
        if m["annotated_first_error_step"] != it["true_error_step"] or \
                hr.map_first_error(f1[iid]["judgment"], it["true_error_step"]) != m["first_error_verdict"]:
            raise SystemExit(f"STOP: frozen mapping for {iid} does not reproduce")
        confirm = m["first_error_verdict"] == "CONFIRM"
        recs.append({"item_id": iid, "domain": it["domain"], "math_level": it["math_level"], "n_steps": it["n_steps"],
                     "annotated_first_error_step": it["true_error_step"],
                     "true_error_relative_position": it["true_error_relative_position"],
                     "proposed_false_step": it["false_location"]["false_step"],
                     "pass1": {k: f1[iid][k] for k in ("judgment", "confidence", "reason", "opened_at", "saved_at",
                                                       "n_saves")},
                     "pass2": None,
                     "first_error_verdict": m["first_error_verdict"],
                     "false_step_verdict": "PASS1_IMPLIED" if confirm else "NOT_REVIEWED",
                     "false_step_validity_basis": waiver["l_false_validity_label"] if confirm else None,
                     "reviewer_first_error_step": m["reviewer_first_error_step"]})
    by = {r["item_id"]: r for r in recs}
    blank = ar.read_form(blank_path)
    filled = []
    for b in blank:
        r = by[b["item_id"]]
        filled.append({**b, "first_error_verdict": r["first_error_verdict"],
                       "reviewer_first_error_step": "" if r["reviewer_first_error_step"] is None
                       else str(r["reviewer_first_error_step"]),
                       "false_step_verdict": r["false_step_verdict"],
                       "notes": f"A5 HUMAN blinded Pass-1 review ({l1['reviewer']}); pass1 {r['pass1']['judgment']}/"
                                f"{r['pass1']['confidence']}; A6: separate Pass 2 waived"
                                + ("; false step Pass-1-implied valid (not independently Pass-2-validated)"
                                   if r["false_step_verdict"] == "PASS1_IMPLIED" else "")})
    decisions, probs = ar.validate_and_decide(filled, blank, cfg)
    if probs:
        raise SystemExit(f"STOP: filled form would not validate: {probs[:5]}")
    summ = ar.summary(decisions, cfg)
    pos = lambda x: "early (<=1/3)" if x <= 1 / 3 else ("middle (<=2/3)" if x <= 2 / 3 else "late (>2/3)")

    def rate(xs):
        n, c = len(xs), sum(x == "CONFIRM" for x in xs)
        return {"n": n, "CONFIRM": c, "DISAGREE": sum(x == "DISAGREE" for x in xs),
                "UNCLEAR": sum(x == "UNCLEAR" for x in xs), "agreement": round(c / n, 3) if n else None}
    group = lambda key: {str(g): rate([r["first_error_verdict"] for r in recs if key(r) == g])
                         for g in sorted({key(r) for r in recs}, key=str)}
    report = {
        "TEST_ONLY": l1["TEST_ONLY"], "STATUS": "HUMAN blinded Pass-1 review (A5); separate Pass 2 waived (A6)",
        "pass2_waiver": waiver["statement"], "l_false_validity_label": waiver["l_false_validity_label"],
        "reviewer": l1["reviewer"], "n_items": len(recs),
        "first_error_verdicts": summ["first_error_verdicts"], "n_primary_stage2": summ["n_primary_stage2"],
        "min_primary_items": summ["min_primary_items"], "primary_gate_passed": summ["primary_gate_passed"],
        "false_step_verdicts": {k: v for k, v in summ["false_step_verdicts"].items() if v},
        "n_l_false_eligible": summ["n_l_false_eligible"],
        "agreement_by_domain": group(lambda r: r["domain"]),
        "agreement_by_relative_error_position": group(lambda r: pos(r["true_error_relative_position"])),
        "disagree_reviewer_vs_annotation": dict(Counter(
            "NO_ERROR" if r["pass1"]["judgment"] == "NO_ERROR" else
            "earlier" if r["pass1"]["judgment"] < r["annotated_first_error_step"] else "later"
            for r in recs if r["first_error_verdict"] == "DISAGREE")),
        "pass1_confidence": dict(Counter(r["pass1"]["confidence"] for r in recs)),
        "pass1_confidence_confirm_items": dict(Counter(r["pass1"]["confidence"] for r in recs
                                                       if r["first_error_verdict"] == "CONFIRM")),
        "pass1_revisions": l1["n_revisions"]}
    j_sha = C.write_json(P["judgments"], {"STATUS": report["STATUS"], "records": recs, "report": report})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ar.FORM_FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(filled)
    filled_path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    if {C.rel(p): C.sha256_file(p) for p in pass1_files} != before:
        raise SystemExit("STOP: a Pass-1 file changed during this run")
    fl = {"frozen": C.now(), "amendment": "A6", "TEST_ONLY": l1["TEST_ONLY"], "reviewer": l1["reviewer"],
          "pass2_waived": True, "pass2_waiver": waiver["statement"],
          "l_false_validity_label": waiver["l_false_validity_label"],
          "failures_sha256": lock_s1["failures_sha256"], "blank_form_sha256": ex["blank_form_sha256"],
          "pass1_files_sha256_unchanged": before,
          "judgments_sha256": j_sha, "filled_form_sha256": C.sha256_file(filled_path)}
    C.write_json(P["final_lock"], fl)
    files = {P["judgments"]: j_sha, filled_path: fl["filled_form_sha256"], P["final_lock"]: C.sha256_file(P["final_lock"])}
    if P["real"]:
        C.update_frozen_hashes({C.rel(k): v for k, v in files.items()})
    print(json.dumps({**report, "files": {C.rel(k): v for k, v in files.items()}}, indent=1, ensure_ascii=False))
    if not summ["primary_gate_passed"]:
        print(f"STOP: only {summ['n_primary_stage2']} CONFIRM first-error verdicts (< {summ['min_primary_items']}).")
        return 3
    print("STOP: next 07b (freeze decisions), then 08 (build Stage-2 requests). No inference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
