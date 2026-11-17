"""Build and FREEZE the paired Stage-2 requests from the FROZEN Stage-1 failure set AND the FROZEN
blinded annotation-review decisions (researcher approval first). Only review-confirmed items get requests; L_false only
where the false step was confirmed correct.

    python scripts/08_build_stage2_requests.py [--allow-underpowered] [--rebuild]
    python scripts/08_build_stage2_requests.py --work-dir outputs/test_runs/<x>/stage2_inputs     # TEST ONLY
One stateless request per (failure, condition) for VV, N_true, L_true, L_false; every condition of an item embeds the
identical Stage-1 response. Prompt audits (verbatim insertion, no VV location leakage, N_true makes no error claim,
L_true = annotated step, L_false = correct pre-error step, scaffold identical apart from the condition sentence)
must pass. Refuses to build if the failure-count gate failed, unless --allow-underpowered (researcher decision).
"""
import argparse
import json
import sys
from collections import Counter

import _bootstrap  # noqa: F401
from src import common as C
from src import stage2_builder as s2b
from src import stage2_runner as s2r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--allow-underpowered", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    lock = s2r.verify_lock(P)
    review, rlock = s2r.verify_review_lock(P)
    decisions = {d["item_id"]: d for d in review["decisions"]}
    doc = C.read_json(P["failures"])
    if set(decisions) != set(doc["item_ids"]):
        raise SystemExit("STOP: review decisions do not cover exactly the frozen failure set")
    rs = review["summary"]
    if not rs["primary_gate_passed"] and not a.allow_underpowered:
        raise SystemExit(f"STOP: only {rs['n_primary_stage2']} failures passed the annotation review "
                         f"(< {rs['min_primary_items']}); report to the researcher (--allow-underpowered only on their decision)")
    if not doc["failure_gate"]["passed"] and not a.allow_underpowered:
        raise SystemExit(f"STOP: only {doc['n_failures']} Stage-1 failures (< {doc['failure_gate']['min_stage1_failures']}). "
                         "Stage 2 is not adequately powered; report to the researcher (--allow-underpowered only on "
                         "their explicit decision).")
    C.verify_frozen([C.rel(C.ITEMS_FILE)])
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    resp = C.read_jsonl(P["responses"])
    reqs = s2b.build_requests(pool, doc["failures"], resp, cfg["stage2"]["conditions"], decisions)
    probs = s2b.audit(reqs, pool, doc["failures"], resp, decisions, cfg["stage2"]["conditions"])
    if probs:
        raise SystemExit("STOP: Stage-2 prompt audit failed:\n" + "\n".join(probs[:20]))
    order = s2b.execution_order(reqs, cfg["stage2"]["execution_order_seed"])
    res = {"requests": C.write_frozen(P["requests"], C.write_jsonl, reqs, a.rebuild),
           "order": C.write_frozen(P["order"], C.write_json, order, a.rebuild)}
    chars = sorted(r["prompt_chars"] for r in reqs)
    man = {"stage": 2, "TEST_ONLY": doc["TEST_ONLY"], "conditions": cfg["stage2"]["conditions"],
           "n_stage1_failures": len(doc["failures"]), "n_items": len({r["item_id"] for r in reqs}),
           "n_requests": len(reqs), "annotation_review_decisions_sha256": rlock["decisions_sha256"],
           "annotation_review_summary": rs,
           "request_key_format": "s2::<item_id>::<condition>", "failures_sha256": lock["failures_sha256"],
           "responses_sha256": lock["responses_sha256"], "order_sha256": order["order_sha256"],
           "prompt_chars": {"min": chars[0], "median": chars[len(chars) // 2], "max": chars[-1]} if chars else None,
           "underpowered_override": bool(a.allow_underpowered and not (doc["failure_gate"]["passed"]
                                                                       and rs["primary_gate_passed"])),
           "audit": "passed", "artifacts_sha256": {k: v[0] for k, v in res.items()},
           "requests_by_condition": dict(sorted(Counter(r["condition"] for r in reqs).items())),
           "primary_endpoint": "paired rescue difference V -> L_true vs V -> V (unchanged; interpretation rule "
                               "lock " + (C.read_json(C.RULE_LOCK)["rule_sha256"] if C.RULE_LOCK.exists() else "absent") + ")",
           "l_false_validity_basis": dict(Counter(d.get("false_step_validity_basis") or "unspecified"
                                                  for d in decisions.values() if d["l_false_eligible"])),
           "excluded_from_stage2": {"n": sum(not d["primary_stage2"] for d in decisions.values()),
                                    "first_error_verdicts": dict(Counter(d["first_error_verdict"] for d in
                                                                         decisions.values() if not d["primary_stage2"]))}}
    if any(d["false_step_verdict"] == "PASS1_IMPLIED" for d in decisions.values()):
        w = cfg["annotation_review"]["pass2_waiver_A6"]
        man["amendment_A6"] = {"pass2_waived": True, "statement": w["statement"],
                               "l_false_validity_label": w["l_false_validity_label"], "primary_set": w["primary_set"]}
    res["manifest"] = C.write_frozen(P["manifest"], C.write_json, man, a.rebuild)
    if P["real"]:
        C.update_frozen_hashes({C.rel(P[k]): v[0] for k, v in res.items()})
    print(json.dumps({**{k: v[1] for k, v in res.items()}, "n_requests": len(reqs), "n_items": man["n_items"],
                      "prompt_chars": man["prompt_chars"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
