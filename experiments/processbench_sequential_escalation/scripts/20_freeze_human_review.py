"""A5: freeze one pass of the blinded HUMAN annotation review. STOP after each.

    python scripts/20_freeze_human_review.py --pass 1 --reviewer "<name>"
    python scripts/20_freeze_human_review.py --pass 2 --reviewer "<name>"
    ... --work-dir outputs/test_runs/<x>/stage2_inputs                                 # TEST ONLY
--pass 1: the Pass-1 log must be intact (hash chain) and give a judgment for every item, all by <name>, all on the
  exported view. Writes pass1_final.json + HUMAN_REVIEW_PASS1_FROZEN.json (log + final sha256) FIRST; only after that
  lock is on disk is the ProcessBench step k read: pass1_mapping.json (CONFIRM / DISAGREE / UNCLEAR, reviewer step
  recorded, never substituted) and the Pass-2 view (Steps 1..f only) are written and bound to the Pass-1 lock in
  HUMAN_REVIEW_PASS2_EXPORT.json. Prints aggregate counts only.
--pass 2: same checks on the Pass-2 log; writes pass2_final.json + HUMAN_REVIEW_PASS2_FROZEN.json, then fills
  annotation_review_form_FILLED.csv (the exported blank form; verdict columns from the frozen human judgments) and
  human_review_judgments.json + HUMAN_REVIEW_FROZEN.json, all hashed (registered in FROZEN_HASHES for the real run).
  Does NOT run 07b and never builds Stage-2 requests.
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


def freeze_log(P, p, reviewer, prereq):
    if P["pass_lock"][p].exists():
        raise SystemExit(f"STOP: Pass {p} is already frozen; it is never re-frozen")
    for k in ("review_lock", "requests"):
        if P[k].exists():
            raise SystemExit(f"STOP: {P[k].name} exists; the human review must precede decisions / Stage 2")
    el = C.read_json(P["export_lock"][p])
    v_sha = C.sha256_file(P["view"][p])
    if v_sha != el[f"pass{p}_view_sha256"]:
        raise SystemExit(f"STOP: {P['view'][p].name} changed after export")
    view = C.read_json(P["view"][p])
    log = hr.read_log(P["log"][p])
    probs = hr.completeness(view, log, v_sha)
    if log and {e["reviewer"] for e in log} != {reviewer}:
        probs.append(f"log reviewer {sorted({e['reviewer'] for e in log})} != --reviewer {reviewer!r}")
    if probs:
        raise SystemExit(f"STOP: Pass {p} cannot be frozen:\n  " + "\n  ".join(probs))
    final = hr.final_rows(view, log)
    f_sha = C.write_json(P["final"][p], {"pass": p, "TEST_ONLY": view["TEST_ONLY"], "reviewer": reviewer,
                                         "view_sha256": v_sha, "rows": final})
    lock = {"frozen": C.now(), "amendment": "A5", "pass": p, "TEST_ONLY": view["TEST_ONLY"], "reviewer": reviewer,
            "n_items": len(final), "n_log_lines": len(log), "n_revisions": len(log) - len(final),
            "first_save": min(e["saved_at"] for e in log), "last_save": max(e["saved_at"] for e in log),
            "view_sha256": v_sha, "log_sha256": C.sha256_file(P["log"][p]), "final_sha256": f_sha, **prereq,
            "statement": ("Pass-1 judgments frozen BEFORE the ProcessBench annotation was read or any agreement "
                          "computed." if p == 1 else "Pass-2 judgments frozen.")}
    C.write_json(P["pass_lock"][p], lock)
    return view, final, lock


def register(P, entries):
    if P["real"]:
        C.update_frozen_hashes({C.rel(k): v for k, v in entries.items()})


def pass1(P, cfg, reviewer, lock_s1):
    if P["pass_lock"][1].exists() and not P["export_lock"][2].exists():
        # interrupted after the Pass-1 lock was written: finish the mapping step from the SAME frozen judgments
        lock = C.read_json(P["pass_lock"][1])
        if (C.sha256_file(P["log"][1]), C.sha256_file(P["final"][1]), lock["reviewer"]) != (
                lock["log_sha256"], lock["final_sha256"], reviewer):
            raise SystemExit("STOP: frozen Pass-1 log / final judgments / reviewer do not match the Pass-1 lock")
        view, final = C.read_json(P["view"][1]), C.read_json(P["final"][1])["rows"]
    else:
        view, final, lock = freeze_log(P, 1, reviewer, {"failures_sha256": lock_s1["failures_sha256"],
                                                        "export_lock_sha256": C.sha256_file(P["export_lock"][1])})
    register(P, {P["log"][1]: lock["log_sha256"], P["final"][1]: lock["final_sha256"],
                 P["pass_lock"][1]: C.sha256_file(P["pass_lock"][1])})
    # ---- ONLY NOW: read the ProcessBench annotation (pre-specified mapping; the reviewer's step is never substituted)
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    items = {i["item_id"]: i for i in pool}
    rows = []
    for r in final:
        k = items[r["item_id"]]["true_error_step"]
        v = hr.map_first_error(r["judgment"], k)
        rows.append({"item_id": r["item_id"], "human_judgment": r["judgment"], "confidence": r["confidence"],
                     "annotated_first_error_step": k, "first_error_verdict": v,
                     "reviewer_first_error_step": r["judgment"] if v == "DISAGREE" and isinstance(r["judgment"], int)
                     else None})
    counts = dict(Counter(x["first_error_verdict"] for x in rows))
    m_sha = C.write_json(P["mapping"], {"pass1_lock_sha256": C.sha256_file(P["pass_lock"][1]), "computed": C.now(),
                                        "rule": cfg["human_review"]["pass1"]["mapping_after_freeze"],
                                        "no_substitution": cfg["human_review"]["pass1"]["no_substitution"],
                                        "counts": counts, "rows": rows})
    doc = C.read_json(P["failures"])
    v2 = hr.pass2_view_doc(pool, doc, cfg, bool(doc["TEST_ONLY"]))
    probs = hr.audit_view(v2, pool, C.read_jsonl(P["responses"]))
    if v2["item_ids"] != view["item_ids"]:
        probs.append("Pass-2 order differs from Pass 1")
    if probs:
        raise SystemExit(f"STOP: Pass-2 view failed the audit: {probs[:5]}")
    v2_sha = C.write_json(P["view"][2], v2)
    el2 = {"exported": C.now(), "amendment": "A5", "TEST_ONLY": v2["TEST_ONLY"], "n_items": v2["n_items"],
           "pass1_lock_sha256": C.sha256_file(P["pass_lock"][1]), "pass1_mapping_sha256": m_sha,
           "pass2_view_sha256": v2_sha, "pass2_fields_shown": list(hr.PASS2_ITEM_KEYS),
           "blinding_audit": "passed (exact keys; Steps 1..f only; no Stage-1 response text)"}
    C.write_json(P["export_lock"][2], el2)
    register(P, {P["mapping"]: m_sha, P["view"][2]: v2_sha, P["export_lock"][2]: C.sha256_file(P["export_lock"][2])})
    print(json.dumps({"pass1_frozen": {k: lock[k] for k in ("frozen", "reviewer", "n_items", "n_log_lines",
                                                            "n_revisions", "log_sha256", "final_sha256")},
                      "pass1_lock_sha256": el2["pass1_lock_sha256"],
                      "first_error_verdict_counts": counts, "pass1_mapping_sha256": m_sha,
                      "pass2_view_sha256": v2_sha,
                      "NEXT": f'python scripts/19_human_review_server.py --pass 2 --reviewer "{reviewer}"   '
                              "(keep pass1_mapping.json closed until Pass 2 is frozen)"}, indent=1))
    return 0


def pass2(P, cfg, reviewer, lock_s1):
    if not P["pass_lock"][1].exists():
        raise SystemExit("STOP: Pass 1 is not frozen")
    el2 = C.read_json(P["export_lock"][2])
    p1_lock_sha = C.sha256_file(P["pass_lock"][1])
    if el2["pass1_lock_sha256"] != p1_lock_sha or C.sha256_file(P["mapping"]) != el2["pass1_mapping_sha256"]:
        raise SystemExit("STOP: Pass-1 lock / mapping changed after the Pass-2 export")
    l1 = C.read_json(P["pass_lock"][1])
    if C.sha256_file(P["log"][1]) != l1["log_sha256"] or C.sha256_file(P["final"][1]) != l1["final_sha256"]:
        raise SystemExit("STOP: the frozen Pass-1 log / final judgments changed")
    if l1["reviewer"] != reviewer:
        raise SystemExit(f"STOP: Pass 1 was reviewed by {l1['reviewer']!r}, not {reviewer!r}")
    ex = C.read_json(P["review_export_lock"])
    blank_path = P["review_dir"] / "annotation_review_form.csv"
    filled_path = P["review_dir"] / "annotation_review_form_FILLED.csv"
    if C.sha256_file(blank_path) != ex["blank_form_sha256"] or ex["failures_sha256"] != lock_s1["failures_sha256"]:
        raise SystemExit("STOP: exported blank form changed / belongs to another failure set")
    if filled_path.exists():
        raise SystemExit(f"STOP: {filled_path} already exists; it is never overwritten")
    view, final2, lock = freeze_log(P, 2, reviewer, {"pass1_lock_sha256": p1_lock_sha,
                                                     "export_lock_sha256": C.sha256_file(P["export_lock"][2])})
    register(P, {P["log"][2]: lock["log_sha256"], P["final"][2]: lock["final_sha256"],
                 P["pass_lock"][2]: C.sha256_file(P["pass_lock"][2])})

    pool = {i["item_id"]: i for i in C.read_json(C.ITEMS_FILE)["pool"]}
    f1 = {r["item_id"]: r for r in C.read_json(P["final"][1])["rows"]}
    f2 = {r["item_id"]: r for r in final2}
    mp = {r["item_id"]: r for r in C.read_json(P["mapping"])["rows"]}
    recs = []
    for iid in view["item_ids"]:
        it = pool[iid]
        recs.append({"item_id": iid, "domain": it["domain"], "math_level": it["math_level"], "n_steps": it["n_steps"],
                     "annotated_first_error_step": it["true_error_step"],
                     "true_error_relative_position": it["true_error_relative_position"],
                     "proposed_false_step": it["false_location"]["false_step"],
                     "pass1": {k: f1[iid][k] for k in ("judgment", "confidence", "reason", "opened_at", "saved_at",
                                                       "n_saves")},
                     "pass2": {k: f2[iid][k] for k in ("judgment", "confidence", "reason", "opened_at", "saved_at",
                                                       "n_saves")},
                     "first_error_verdict": mp[iid]["first_error_verdict"],
                     "false_step_verdict": f2[iid]["judgment"],
                     "reviewer_first_error_step": mp[iid]["reviewer_first_error_step"]})
    by = {r["item_id"]: r for r in recs}
    blank = ar.read_form(blank_path)
    filled = []
    for b in blank:
        r = by[b["item_id"]]
        filled.append({**b, "first_error_verdict": r["first_error_verdict"],
                       "reviewer_first_error_step": "" if r["reviewer_first_error_step"] is None
                       else str(r["reviewer_first_error_step"]),
                       "false_step_verdict": r["false_step_verdict"],
                       "notes": f"A5 HUMAN blinded two-pass review ({reviewer}); pass1 {r['pass1']['judgment']}/"
                                f"{r['pass1']['confidence']}, pass2 {r['pass2']['confidence']}"})
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
        "TEST_ONLY": view["TEST_ONLY"], "STATUS": "HUMAN blinded two-pass annotation-fidelity review (amendment A5)",
        "reviewer": reviewer, "n_items": len(recs),
        "first_error_verdicts": summ["first_error_verdicts"], "n_primary_stage2": summ["n_primary_stage2"],
        "min_primary_items": summ["min_primary_items"], "primary_gate_passed": summ["primary_gate_passed"],
        "false_step_verdicts": summ["false_step_verdicts"], "n_l_false_eligible": summ["n_l_false_eligible"],
        "agreement_by_domain": group(lambda r: r["domain"]),
        "agreement_by_annotated_step_k": group(lambda r: r["annotated_first_error_step"]),
        "agreement_by_relative_error_position": group(lambda r: pos(r["true_error_relative_position"])),
        "disagree_reviewer_vs_annotation": dict(Counter(
            "NO_ERROR" if r["pass1"]["judgment"] == "NO_ERROR" else
            "earlier" if r["pass1"]["judgment"] < r["annotated_first_error_step"] else "later"
            for r in recs if r["first_error_verdict"] == "DISAGREE")),
        "confidence": {"pass1": dict(Counter(r["pass1"]["confidence"] for r in recs)),
                       "pass2": dict(Counter(r["pass2"]["confidence"] for r in recs))},
        "revisions": {"pass1": l1["n_revisions"], "pass2": lock["n_revisions"]},
        "low_confidence_cases": [{"item_id": r["item_id"], "pass": p, "judgment": r[f"pass{p}"]["judgment"],
                                  "verdict": r["first_error_verdict"] if p == 1 else r["false_step_verdict"],
                                  "annotated_k": r["annotated_first_error_step"], "reason": r[f"pass{p}"]["reason"]}
                                 for r in recs for p in (1, 2) if r[f"pass{p}"]["confidence"] == "LOW"]}
    j_sha = C.write_json(P["judgments"], {"STATUS": report["STATUS"], "records": recs, "report": report})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ar.FORM_FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(filled)
    filled_path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    fl = {"frozen": C.now(), "amendment": "A5", "TEST_ONLY": view["TEST_ONLY"], "reviewer": reviewer,
          "failures_sha256": lock_s1["failures_sha256"], "blank_form_sha256": ex["blank_form_sha256"],
          "pass1_view_sha256": l1["view_sha256"], "pass1_log_sha256": l1["log_sha256"],
          "pass1_final_sha256": l1["final_sha256"], "pass1_lock_sha256": p1_lock_sha,
          "pass1_mapping_sha256": el2["pass1_mapping_sha256"], "pass2_view_sha256": lock["view_sha256"],
          "pass2_log_sha256": lock["log_sha256"], "pass2_final_sha256": lock["final_sha256"],
          "pass2_lock_sha256": C.sha256_file(P["pass_lock"][2]),
          "judgments_sha256": j_sha, "filled_form_sha256": C.sha256_file(filled_path)}
    C.write_json(P["final_lock"], fl)
    files = {P["judgments"]: j_sha, filled_path: fl["filled_form_sha256"], P["final_lock"]: C.sha256_file(P["final_lock"])}
    register(P, files)
    print(json.dumps({**report, "files": {C.rel(k): v for k, v in files.items()}}, indent=1, ensure_ascii=False))
    if not summ["primary_gate_passed"]:
        print(f"STOP: only {summ['n_primary_stage2']} CONFIRM first-error verdicts (< {summ['min_primary_items']}).")
        return 3
    print("STOP: report to the researcher. 07b (freeze decisions) and Stage 2 only after approval.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pass", dest="pass_no", type=int, choices=[1, 2], required=True)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = hr.paths(a.work_dir)
    if a.pass_no == 2 and P["real"] and cfg["human_review"].get("pass2_waived_by"):
        raise SystemExit(f"STOP: the separate Pass-2 review was waived by amendment {cfg['human_review']['pass2_waived_by']}"
                         "; decisions come from scripts/21_freeze_pass1_only_review.py")
    lock_s1 = s2r.verify_lock(P)
    if P["real"]:
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + ["src/human_review.py", "src/llm_review.py",
                                                    C.rel(P["export_lock"][1]), C.rel(P["view"][1])])
    return (pass1 if a.pass_no == 1 else pass2)(P, cfg, a.reviewer.strip(), lock_s1)


if __name__ == "__main__":
    sys.exit(main())
