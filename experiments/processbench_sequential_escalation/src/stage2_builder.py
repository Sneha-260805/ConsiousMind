"""Freeze the Stage-1 failure set, then build the paired Stage-2 requests from it.

FREEZE (scripts/07): from a COMPLETE, verified Stage-1 run (run dir or FINAL archive):
  - every planned Stage-1 key present exactly once, prompts == frozen prompts, manifest identity == frozen inputs
  - rows re-scored with the frozen scorer; FAILURE := outcome != repair (outcome-adaptive by design)
  - stage1_failures.json (item ids + Stage-1 outcome/answer/truncation + response sha) and
    stage1_failure_responses.jsonl (verbatim Stage-1 responses of the failures) are written and hashed; a lock file
    records both hashes and the source archive hash. Once locked, the failure set can never change.
  - failure-count gate (config failure_gate.min_stage1_failures) evaluated and recorded.
BUILD (scripts/08): for every frozen failure x {VV, N_true, L_true, L_false} one stateless Stage-2 request whose
prompt embeds the item's exact Stage-1 response. Request key: s2::<item_id>::<condition>.
"""
import json
import math
import zipfile
from pathlib import Path

from . import common as C
from . import prompt_builder as pb
from . import scorer
from .persistence import read_jsonl_tolerant


def load_stage1_run(source):
    """-> (raw_rows, manifest, source_sha256). `source` = run directory or ZIP (checkpoint_FINAL / FINAL archive)."""
    source = Path(source)
    if source.is_dir():
        rows, note = read_jsonl_tolerant(source / "raw_outputs.jsonl", repair=False)
        if note:
            raise SystemExit(f"STOP: Stage-1 raw file is torn ({note}); resume the run first")
        man = C.read_json(source / "run_manifest.json")
        return rows, man, C.sha256_file(source / "raw_outputs.jsonl")
    with zipfile.ZipFile(source) as z:
        if z.testzip() is not None:
            raise SystemExit("STOP: Stage-1 archive is corrupt")
        names = z.namelist()
        raw_name = [n for n in names if n.endswith("raw_outputs.jsonl")]
        man_name = [n for n in names if n.endswith("run_manifest.json")]
        if len(raw_name) != 1 or len(man_name) != 1:
            raise SystemExit("STOP: archive must contain exactly one raw_outputs.jsonl and one run_manifest.json")
        rows = [json.loads(l) for l in z.read(raw_name[0]).decode("utf-8").splitlines() if l.strip()]
        man = json.loads(z.read(man_name[0]))
    return rows, man, C.sha256_file(source)


def verify_stage1_complete(rows, man, by_key, order, hashes):
    probs = []
    planned = [o["request_key"] for o in order["order"]]
    keys = [r["request_key"] for r in rows]
    if man.get("stage") != 1:
        probs.append("manifest is not a Stage-1 run")
    if man.get("mode") != "full":
        probs.append(f"run mode is {man.get('mode')!r}, not 'full'")
    for f, k in (("requests_sha256", "requests"), ("execution_order_sha256", "order"), ("items_sha256", "items")):
        if man.get(f) != hashes[k]:
            probs.append(f"run manifest {f} != frozen {k}")
    if man.get("planned_keys_sha256") != C.sha256_text("\n".join(planned)):
        probs.append("run plan is not the full frozen Stage-1 plan")
    if len(keys) != len(set(keys)):
        probs.append("duplicate request keys")
    missing = set(planned) - set(keys)
    extra = set(keys) - set(planned)
    if missing:
        probs.append(f"{len(missing)} planned Stage-1 requests missing (e.g. {sorted(missing)[:3]})")
    if extra:
        probs.append(f"{len(extra)} unknown request keys")
    for r in rows:
        f = by_key.get(r["request_key"])
        if f and (r["prompt_sha256"] != f["prompt_sha256"] or C.sha256_text(r["prompt"]) != f["prompt_sha256"]):
            probs.append(f"{r['request_key']}: prompt differs from the frozen prompt")
    return probs


def failures_from_rows(rows, n_pool, cfg):
    scored = {s["request_key"]: s for s in scorer.score_rows(rows)}
    raw = {r["request_key"]: r for r in rows}
    fails, resp = [], []
    for k in sorted(scored, key=lambda k: raw[k]["run_index"]):
        s = scored[k]
        if s["repair"]:
            continue
        text = raw[k]["raw_response"]
        fails.append({"item_id": s["item_id"], "domain": s["domain"], "stage1_request_key": k,
                      "stage1_outcome": s["outcome"], "stage1_numeric_answer": s["numeric_parsed_answer"],
                      "stage1_numeric_parse_status": s["numeric_parse_status"], "stage1_truncated": s["truncated"],
                      "stage1_new_tokens": s["new_tokens"], "stage1_response_sha256": C.sha256_text(text)})
        resp.append({"item_id": s["item_id"], "stage1_request_key": k, "stage1_response": text,
                     "stage1_response_sha256": C.sha256_text(text)})
    fails.sort(key=lambda f: f["item_id"])
    resp.sort(key=lambda f: f["item_id"])
    n_fail = len(fails)
    need = cfg["failure_gate"]["min_stage1_failures"]
    rate = n_fail / n_pool if n_pool else 0.0
    gate = {"min_stage1_failures": need, "n_stage1_failures": n_fail, "n_stage1_items": n_pool,
            "stage1_failure_rate": round(rate, 4), "passed": n_fail >= need}
    if not gate["passed"]:
        gate["additional_items_needed_at_observed_rate"] = (math.ceil(need / rate) - n_pool) if rate > 0 else None
        gate["note"] = ("Stage 2 must NOT be analysed as adequately powered. Expansion (from the frozen reserve, in "
                        "reserve order) may depend only on this Stage-1 failure count and needs researcher approval.")
    return fails, resp, gate


def failures_doc(fails, gate, source_sha, man, test_only):
    return {"stage": "B", "artifact": "stage1_failures", "TEST_ONLY": test_only,
            "definition": "Stage-1 V repair == failure (frozen numeric parse != gold). Outcome-adaptive BY DESIGN.",
            "source_run_id": man["run_id"], "source_sha256": source_sha,
            "source_requests_sha256": man["requests_sha256"], "failure_gate": gate,
            "n_failures": len(fails), "item_ids": [f["item_id"] for f in fails], "failures": fails,
            "oracle_status": C.ORACLE_STATEMENT}


# ------------------------------------------------------------------ Stage-2 requests
def request_key(item_id, condition):
    return f"s2::{item_id}::{condition}"


def build_requests(pool, failures, responses, conditions, decisions):
    """decisions: {item_id: frozen annotation-review decision}. Only primary_stage2 items get requests; L_false only
    where l_false_eligible."""
    items = {i["item_id"]: i for i in pool}
    resp = {r["item_id"]: r for r in responses}
    reqs = []
    for f in failures:
        dec = decisions[f["item_id"]]
        if not dec["primary_stage2"]:
            continue                       # excluded by the blinded annotation review (all conditions)
        it = items[f["item_id"]]
        r = resp[f["item_id"]]
        if C.sha256_text(r["stage1_response"]) != f["stage1_response_sha256"]:
            raise SystemExit(f"STOP: Stage-1 response hash mismatch for {f['item_id']}")
        for cond in conditions:
            if cond == "L_false" and not (it.get("false_location") and dec["l_false_eligible"]):
                continue          # ineligible ONLY for L_false
            prompt = pb.stage2_prompt(it, cond, r["stage1_response"])
            reqs.append({"request_key": request_key(it["item_id"], cond), "stage": 2, "item_id": it["item_id"],
                         "domain": it["domain"], "condition": cond, "pointed_step": pb.pointed_step(it, cond),
                         "true_error_step": it["true_error_step"], "gold_answer": it["gold_answer"],
                         "original_answer": it["original_answer"], "stage1_request_key": f["stage1_request_key"],
                         "stage1_numeric_answer": f["stage1_numeric_answer"], "stage1_outcome": f["stage1_outcome"],
                         "stage1_response_sha256": f["stage1_response_sha256"],
                         "prompt": prompt, "prompt_sha256": C.sha256_text(prompt), "prompt_chars": len(prompt)})
    return reqs


def execution_order(reqs, seed):
    keys = sorted((r["request_key"] for r in reqs), key=lambda k: C.sha256_text(f"{seed}:{k}"))
    return {"order_seed": seed, "n": len(keys),
            "order": [{"run_index": i, "request_key": k} for i, k in enumerate(keys)],
            "order_sha256": C.sha256_text("\n".join(keys))}


def audit(reqs, pool, failures, responses, decisions=None, conditions=("VV", "N_true", "L_true", "L_false")):
    items = {i["item_id"]: i for i in pool}
    resp = {r["item_id"]: r["stage1_response"].strip() for r in responses}
    fail_ids = {f["item_id"] for f in failures}
    probs = []
    keys = [r["request_key"] for r in reqs]
    if len(keys) != len(set(keys)):
        probs.append("duplicate Stage-2 request keys")
    if {r["item_id"] for r in reqs} - fail_ids:
        probs.append("an item that did not fail Stage 1 has a Stage-2 request")
    by_item = {}
    for r in reqs:
        by_item.setdefault(r["item_id"], {})[r["condition"]] = r
    if decisions is not None:
        for iid in fail_ids:
            dec = decisions[iid]
            want = set() if not dec["primary_stage2"] else                 {c for c in conditions if c != "L_false" or dec["l_false_eligible"]}
            got = set(by_item.get(iid, {}))
            if got != want:
                probs.append(f"{iid}: conditions {sorted(got)} != review-determined {sorted(want)}")
    for iid, conds in by_item.items():
        if iid not in fail_ids or iid not in resp:
            continue                       # already reported above; no Stage-1 response to check against
        it = items[iid]
        prev = resp[iid]
        k, f = it["true_error_step"], it["false_location"]["false_step"]
        shells = {}
        for cond, r in conds.items():
            p = r["prompt"]
            block = f"{pb.BEGIN}\n{prev}\n{pb.END}"
            if p.count(pb.BEGIN) != 1 or p.count(pb.END) != 1 or block not in p:
                probs.append(f"{r['request_key']}: previous repair attempt not inserted exactly once / verbatim")
            fb = p.split(pb.END, 1)[1].rsplit("\n\nFeedback:\n", 1)[1].split("\n\n", 1)[0]
            sentence = fb.replace(pb.STILL_WRONG, "", 1).strip()
            if fb != f"{pb.STILL_WRONG} {pb.condition_sentence(cond, pb.pointed_step(it, cond))}":
                probs.append(f"{r['request_key']}: Stage-2 feedback is not exactly <identical first sentence> + "
                             "<condition sentence>")
            if cond == "VV" and any(ch.isdigit() for ch in fb):
                probs.append(f"{r['request_key']}: VV feedback leaks a step number")
            if cond == "VV" and "step" in fb.lower():
                probs.append(f"{r['request_key']}: VV feedback mentions a step")
            if cond == "N_true" and (pb.find_terms(sentence, pb.ERROR_WORDS) or f"Step {k} " not in sentence + " "):
                probs.append(f"{r['request_key']}: N_true claims an error or points at the wrong step")
            if cond == "L_true" and sentence != f"The first mistake in the ORIGINAL proposed solution is in Step {k}.":
                probs.append(f"{r['request_key']}: L_true does not point at the annotated step")
            if cond == "L_false" and (sentence != f"The first mistake in the ORIGINAL proposed solution is in Step {f}."
                                      or not 1 <= f < k):
                probs.append(f"{r['request_key']}: L_false does not point at a correct pre-error step")
            sc = pb.scaffold(p, it, prev).replace(fb, "")
            if pb.find_terms(sc, pb.FORBIDDEN):
                probs.append(f"{r['request_key']}: forbidden scaffold terms {pb.find_terms(sc, pb.FORBIDDEN)}")
            shells[cond] = sc
        if len(set(shells.values())) != 1:
            probs.append(f"{iid}: Stage-2 prompts differ outside the condition sentence")
        if "L_true" in conds and "L_false" in conds:
            a, b = conds["L_true"]["prompt"], conds["L_false"]["prompt"]
            if a.replace(f"is in Step {k}.", "is in Step #.") != b.replace(f"is in Step {f}.", "is in Step #."):
                probs.append(f"{iid}: L_true and L_false differ beyond the step number")
        if len({r["stage1_response_sha256"] for r in conds.values()}) != 1:
            probs.append(f"{iid}: conditions do not share the identical Stage-1 response")
    return probs
