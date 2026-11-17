"""Stage 1 (V on every frozen pool item): request building, smoke selection and run orchestration.

Request key: s1::<item_id>::V  (globally unique; one request per pool item).
Execution order: sorted by sha256(execution_order_seed : request_key) - fixed, seeded, recorded with its hash.
"""
from pathlib import Path

from . import common as C
from . import prompt_builder as pb
from . import runner_core as rc


def request_key(item_id):
    return f"s1::{item_id}::V"


def build_requests(pool):
    reqs = []
    for it in pool:
        prompt = pb.stage1_prompt(it)
        reqs.append({"request_key": request_key(it["item_id"]), "stage": 1, "item_id": it["item_id"],
                     "domain": it["domain"], "condition": "V", "pointed_step": None,
                     "true_error_step": it["true_error_step"], "gold_answer": it["gold_answer"],
                     "original_answer": it["original_answer"], "stage1_request_key": None,
                     "stage1_numeric_answer": None, "stage1_outcome": None, "stage1_response_sha256": None,
                     "prompt": prompt, "prompt_sha256": C.sha256_text(prompt), "prompt_chars": len(prompt)})
    return reqs


def execution_order(reqs, seed):
    keys = sorted((r["request_key"] for r in reqs), key=lambda k: C.sha256_text(f"{seed}:{k}"))
    return {"order_seed": seed, "n": len(keys),
            "order": [{"run_index": i, "request_key": k} for i, k in enumerate(keys)],
            "order_sha256": C.sha256_text("\n".join(keys))}


def audit(reqs, pool):
    """Prompt audits; returns a list of problems (empty = pass)."""
    items = {i["item_id"]: i for i in pool}
    probs = []
    keys = [r["request_key"] for r in reqs]
    if len(keys) != len(set(keys)) or len(reqs) != len(pool):
        probs.append("request keys not unique / not one per item")
    shells = set()
    for r in reqs:
        it = items[r["item_id"]]
        sc = pb.scaffold(r["prompt"], it)
        bad = pb.find_terms(sc, pb.FORBIDDEN)
        if bad:
            probs.append(f"{r['request_key']}: forbidden scaffold terms {bad}")
        if pb.V_FEEDBACK not in r["prompt"] or "FINAL_ANSWER: <answer>" not in r["prompt"]:
            probs.append(f"{r['request_key']}: V feedback / format line missing")
        if f"Step {it['n_steps']}: " not in r["prompt"] or f"Step {it['n_steps'] + 1}: " in r["prompt"]:
            probs.append(f"{r['request_key']}: step numbering not 1..n")
        shells.add(sc)
    if len(shells) != 1:
        probs.append(f"Stage-1 scaffold is not identical across items ({len(shells)} variants)")
    return probs


def smoke_keys(reqs, order, cfg):
    sm = cfg["stage1"]["smoke"]
    by_key = {r["request_key"]: r for r in reqs}
    longest = sorted(reqs, key=lambda r: (-r["prompt_chars"], r["request_key"]))[: sm["n_longest_traces"]]
    chosen = [r["request_key"] for r in longest]
    want = cfg["dataset"]["domains_used"]
    nxt = 0
    for o in order["order"]:
        if len(chosen) >= sm["n_requests"]:
            break
        k = o["request_key"]
        if k not in chosen and by_key[k]["domain"] == want[nxt % len(want)]:
            chosen.append(k)
            nxt += 1
    for o in order["order"]:                    # fill if one domain runs out
        if len(chosen) >= sm["n_requests"]:
            break
        if o["request_key"] not in chosen:
            chosen.append(o["request_key"])
    return chosen


def frozen_inputs(cfg=None):
    """Verify and load the frozen Stage-1 request set. -> (by_key, order, hashes)"""
    rels = [C.rel(C.ITEMS_FILE), C.rel(C.STAGE1_REQUESTS), C.rel(C.STAGE1_ORDER)]
    h = C.verify_frozen(rels + C.AMENDED_PROTOCOL_FILES)
    by_key, order = rc.load_requests(C.STAGE1_REQUESTS, C.STAGE1_ORDER)
    return by_key, order, {"items": h[rels[0]], "requests": h[rels[1]], "order": h[rels[2]], "upstream": None}


def run(solver_kind, run_id, output_root, mode="full", keys=None, resume_from=None, force_unlock=False,
        limit=None, log=print, mock_profile=None):
    from . import solver_loader as sl
    cfg = C.load_config()
    by_key, order, hashes = frozen_inputs(cfg)
    keys = keys or [o["request_key"] for o in order["order"]]
    backend, bname, test_only, sc, sha, ov = sl.make_solver(solver_kind, cfg, list(by_key.values()), mock_profile)
    root = Path(output_root) if output_root else (C.TEST_RUNS_ROOT if test_only else C.RUNS_ROOT)
    run_dir = root / run_id
    man = rc.run_manifest(run_id, 1, mode, test_only, bname, cfg, sc, sha, ov, hashes, keys)
    if resume_from:
        n, note = rc.import_previous(run_dir, resume_from, man, by_key)
        log(f"imported {n} completed rows from {resume_from} {note or ''}")
    stats = rc.execute(run_dir, by_key, order, keys, backend, man, cfg["runner"], log, force_unlock, limit)
    diag, _ = rc.score_run(run_dir)
    return run_dir, stats, diag, backend
