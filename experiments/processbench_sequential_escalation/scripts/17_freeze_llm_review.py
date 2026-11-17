"""LOCAL: verify the A3 review archive, apply the pre-specified mapping, fill the decision form, freeze, report. STOP.

    python scripts/17_freeze_llm_review.py --source <stage_b_llm_review_FINAL.zip>
    python scripts/17_freeze_llm_review.py --source <mock zip> --work-dir outputs/test_runs/<x>/stage2_inputs   # TEST
1. Archive integrity (ARCHIVE_INDEX + sidecar); both passes complete (76 + 76), request/prompt hashes equal the
   frozen review requests, reviewer identity = registered configuration, Pass 2 bound to the completed Pass-1 raw
   outputs (upstream_sha256 == PASS1_COMPLETE) and every Pass-1 row written before the first Pass-2 row.
2. Re-parses every raw output with the frozen rules (src/llm_review.py) - must equal the archived judgments.
3. ONLY NOW compares Pass 1 with the frozen ProcessBench annotation k (llm_review.map_first_error); the reviewer's
   step is recorded, never substituted. Pass 2 -> false_step_verdict (map_false_step).
4. Writes <review_dir>/annotation_review_form_FILLED.csv (the blank exported form, verdict columns filled by the
   LLM-assisted reviewer; notes mark every row PROVISIONAL / NOT human-validated) + llm_review_judgments.json +
   LLM_REVIEW_FROZEN.json, all hashed and registered. Prints the report. Does NOT run 07b, never builds Stage 2.
"""
import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict

import _bootstrap  # noqa: F401
from src import annotation_review as ar
from src import common as C
from src import llm_review as lr
from src import llm_review_runner as lrr
from src import stage2_runner as s2r


def rows_of(data, suffix):
    names = [n for n in data if n.endswith(suffix)]
    if len(names) != 1:
        raise SystemExit(f"STOP: expected exactly one {suffix} in the archive, found {names}")
    return names[0], data[names[0]]


def jl(b):
    return [json.loads(l) for l in b.decode("utf-8").splitlines() if l.strip()]


def rate(xs):
    n = len(xs)
    c = sum(x == "CONFIRM" for x in xs)
    return {"n": n, "CONFIRM": c, "DISAGREE": sum(x == "DISAGREE" for x in xs), "UNCLEAR": sum(x == "UNCLEAR" for x in xs),
            "agreement": round(c / n, 3) if n else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    if P["real"] and cfg["llm_review"].get("stopped_by"):
        raise SystemExit(f"STOP: the LLM reviewer was stopped by amendment {cfg['llm_review']['stopped_by']}; no LLM "
                         "verdict is ever frozen into data/ (human review: scripts/18-20). --work-dir TEST runs only.")
    lock = s2r.verify_lock(P)
    by_key, order, hashes = lrr.frozen_inputs()
    if P["review_lock"].exists() or P["requests"].exists():
        raise SystemExit("STOP: review decisions already frozen / Stage-2 requests exist")
    ex = C.read_json(P["review_export_lock"])
    blank_path = P["review_dir"] / "annotation_review_form.csv"
    if C.sha256_file(blank_path) != ex["blank_form_sha256"] or ex["failures_sha256"] != lock["failures_sha256"]:
        raise SystemExit("STOP: exported blank form changed / belongs to another failure set")
    filled_path = P["review_dir"] / "annotation_review_form_FILLED.csv"
    if filled_path.exists():
        raise SystemExit(f"STOP: {filled_path} already exists; it is never overwritten")

    data = lrr.read_archive(a.source)
    src_sha = C.sha256_file(a.source)
    pool = {i["item_id"]: i for i in C.read_json(C.ITEMS_FILE)["pool"]}
    rc = cfg["llm_review"]["reviewer"]
    n = len(order["item_ids"])
    raw, mans, jud = {}, {}, {}
    for p in (1, 2):
        rn, rb = rows_of(data, f"_pass{p}_" + rn_suffix(data, p) + "/raw_outputs.jsonl")
        raw[p] = jl(rb)
        mans[p] = json.loads(data[rn.replace("raw_outputs.jsonl", "run_manifest.json")])
        jud[p] = jl(data[rn.replace("raw_outputs.jsonl", f"judgments_pass{p}.jsonl")])
        m = mans[p]
        test_only = bool(m["test_only"])
        if P["real"] and test_only:
            raise SystemExit("STOP: a TEST-ONLY (mock) review can never be frozen into data/; use --work-dir")
        probs = []
        if m["requests_sha256"] != hashes["requests"][p] or m["execution_order_sha256"] != hashes["order"]:
            probs.append("request / order hash")
        if not test_only and (m["model"], m["model_revision"], m["solver_overrides"]["quantization"],
                              m["max_new_tokens"], m["decoding"], m.get("repetition_penalty")) != (
                rc["model"], rc["model_revision"], rc["quantization"], rc["max_new_tokens"], rc["decoding"],
                rc["repetition_penalty"]):
            probs.append("reviewer identity")
        if not {"A3", "A4"} <= set(m["protocol_amendments"]) or m["parser_sha256"] != C.sha256_file(C.STAGE_ROOT / "src" / "llm_review.py"):
            probs.append("amendment / prompt-module hash")
        keys = [lr.request_key(p, i) for i in order["item_ids"]]
        if sorted(r["request_key"] for r in raw[p]) != sorted(keys) or len(raw[p]) != n:
            probs.append(f"pass {p} incomplete or has unplanned rows")
        for r in raw[p]:
            if r["prompt_sha256"] != by_key[p][r["request_key"]]["prompt_sha256"] or \
                    C.sha256_text(r["prompt"]) != r["prompt_sha256"]:
                probs.append(f"prompt mismatch {r['request_key']}")
        re_j = [{**lr.parse_output(r["raw_response"], p, pool[r["item_id"]]["n_steps"]),
                 "request_key": r["request_key"]} for r in raw[p]]
        if [(x["request_key"], x["parse_status"], x["judgment"], x["confidence"]) for x in re_j] != \
                [(x["request_key"], x["parse_status"], x["judgment"], x["confidence"]) for x in jud[p]]:
            probs.append(f"pass {p} archived judgments differ from a re-parse of the raw outputs")
        if probs:
            raise SystemExit(f"STOP: review archive check failed: {probs[:10]}")
    _, done_b = rows_of(data, "PASS1_COMPLETE.json")
    p1_sha = C.sha256_bytes(rows_of(data, f"_pass1_{rn_suffix(data, 1)}/raw_outputs.jsonl")[1])
    if json.loads(done_b)["pass1_raw_outputs_sha256"] != p1_sha or mans[2]["upstream_sha256"] != p1_sha or \
            max(r["timestamp"] for r in raw[1]) > min(r["timestamp"] for r in raw[2]):
        raise SystemExit("STOP: Pass 2 is not bound to / did not start after the completed Pass 1")

    # ---- ONLY NOW: compare with the frozen annotation (pre-specified mapping)
    j1 = {x["request_key"].split("::")[1]: x for x in jud[1]}
    j2 = {x["request_key"].split("::")[1]: x for x in jud[2]}
    recs = []
    for iid in order["item_ids"]:
        it = pool[iid]
        k, f = it["true_error_step"], it["false_location"]["false_step"]
        fe = lr.map_first_error(j1[iid]["judgment"], k)
        fs_ = lr.map_false_step(j2[iid]["judgment"])
        recs.append({"item_id": iid, "domain": it["domain"], "math_level": it["math_level"], "n_steps": it["n_steps"],
                     "annotated_first_error_step": k, "true_error_relative_position": it["true_error_relative_position"],
                     "proposed_false_step": f,
                     "pass1": {k2: j1[iid][k2] for k2 in ("parse_status", "judgment", "confidence", "reason",
                                                          "raw_response_sha256", "finish_reason")},
                     "pass2": {k2: j2[iid][k2] for k2 in ("parse_status", "judgment", "confidence", "reason",
                                                          "raw_response_sha256", "finish_reason")},
                     "first_error_verdict": fe, "false_step_verdict": fs_,
                     "reviewer_first_error_step": j1[iid]["judgment"] if fe == "DISAGREE" and
                     isinstance(j1[iid]["judgment"], int) else None})
    blank = ar.read_form(blank_path)
    by = {r["item_id"]: r for r in recs}
    filled = []
    for b in blank:
        r = by[b["item_id"]]
        filled.append({**b, "first_error_verdict": r["first_error_verdict"],
                       "reviewer_first_error_step": "" if r["reviewer_first_error_step"] is None
                       else str(r["reviewer_first_error_step"]),
                       "false_step_verdict": r["false_step_verdict"],
                       "notes": f"A3 LLM-assisted reviewer {rc['reviewer_tag']}; PROVISIONAL, NOT human-validated; "
                                f"pass1 {r['pass1']['parse_status']}/{r['pass1']['confidence']}, "
                                f"pass2 {r['pass2']['parse_status']}/{r['pass2']['confidence']}"})
    decisions, probs = ar.validate_and_decide(filled, blank, cfg)
    if probs:
        raise SystemExit(f"STOP: filled form would not validate: {probs[:5]}")
    summ = ar.summary(decisions, cfg)

    pos = lambda x: "early (<=1/3)" if x <= 1 / 3 else ("middle (<=2/3)" if x <= 2 / 3 else "late (>2/3)")
    group = lambda key: {g: rate([r["first_error_verdict"] for r in recs if key(r) == g])
                         for g in sorted({key(r) for r in recs}, key=str)}
    report = {
        "TEST_ONLY": test_only, "STATUS": cfg["llm_review"]["provisional_statement"],
        "reviewer": rc["reviewer_tag"], "source_archive_sha256": src_sha, "n_items": n,
        "first_error_verdicts": summ["first_error_verdicts"], "n_primary_stage2": summ["n_primary_stage2"],
        "min_primary_items": summ["min_primary_items"], "primary_gate_passed": summ["primary_gate_passed"],
        "false_step_verdicts": summ["false_step_verdicts"], "n_l_false_eligible": summ["n_l_false_eligible"],
        "agreement_by_domain": group(lambda r: r["domain"]),
        "agreement_by_annotated_step_k": group(lambda r: r["annotated_first_error_step"]),
        "agreement_by_relative_error_position": group(lambda r: pos(r["true_error_relative_position"])),
        "disagree_reviewer_vs_annotation": dict(Counter(
            ("NO_ERROR" if r["pass1"]["judgment"] == "NO_ERROR" else
             "earlier" if r["pass1"]["judgment"] < r["annotated_first_error_step"] else "later")
            for r in recs if r["first_error_verdict"] == "DISAGREE")),
        "parse_status": {"pass1": dict(Counter(r["pass1"]["parse_status"] for r in recs)),
                         "pass2": dict(Counter(r["pass2"]["parse_status"] for r in recs))},
        "low_confidence_cases": [{"item_id": r["item_id"], "pass": p, "confidence": r[f"pass{p}"]["confidence"],
                                  "verdict": r["first_error_verdict"] if p == 1 else r["false_step_verdict"],
                                  "reviewer_judgment": r[f"pass{p}"]["judgment"],
                                  "annotated_k": r["annotated_first_error_step"], "reason": r[f"pass{p}"]["reason"]}
                                 for r in recs for p in (1, 2) if r[f"pass{p}"]["confidence"] in ("LOW", None)],
    }
    jpath = (lrr.REVIEW_DIR if P["real"] else P["review_dir"]) / "llm_review_judgments.json"
    j_sha = C.write_json(jpath, {"STATUS": report["STATUS"], "records": recs, "report": report})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ar.FORM_FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(filled)
    filled_path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    lk = {"frozen": C.now(), "TEST_ONLY": test_only, "amendment": "A3", "STATUS": report["STATUS"],
          "source_archive_sha256": src_sha,
          "pass1_raw_outputs_sha256": p1_sha,
          "pass2_raw_outputs_sha256": C.sha256_bytes(rows_of(data, f"_pass2_{rn_suffix(data, 2)}/raw_outputs.jsonl")[1]),
          "judgments_sha256": j_sha, "filled_form_sha256": C.sha256_file(filled_path),
          "failures_sha256": lock["failures_sha256"], "blank_form_sha256": ex["blank_form_sha256"]}
    lpath = (C.DATA_MAN if P["real"] else P["review_dir"]) / "LLM_REVIEW_FROZEN.json"
    C.write_json(lpath, lk)
    if P["real"]:
        C.update_frozen_hashes({C.rel(jpath): j_sha, C.rel(filled_path): lk["filled_form_sha256"],
                                C.rel(lpath): C.sha256_file(lpath)})
    print(json.dumps({**report, "files": {C.rel(jpath): j_sha, C.rel(filled_path): lk["filled_form_sha256"],
                                          C.rel(lpath): C.sha256_file(lpath)}}, indent=1, ensure_ascii=False))
    if not summ["primary_gate_passed"]:
        print(f"STOP: only {summ['n_primary_stage2']} CONFIRM first-error verdicts (< {summ['min_primary_items']}).")
        return 3
    print("STOP: report to the researcher. 07b (freeze decisions) and Stage 2 only after approval.")
    return 0


def rn_suffix(data, p):
    names = [n for n in data if f"_pass{p}_" in n and n.endswith("/raw_outputs.jsonl") and "smoke" not in n]
    if len(names) != 1:
        raise SystemExit(f"STOP: expected one Pass-{p} run in the archive, found {names}")
    return names[0].split(f"_pass{p}_", 1)[1].split("/", 1)[0]


if __name__ == "__main__":
    sys.exit(main())
