"""Stage 2: locations of the frozen failure set / Stage-2 requests, smoke selection and run orchestration.

REAL artifacts live in data/processed + data/manifests (registered in FROZEN_HASHES.json). TEST (mock) flows use
a work directory under outputs/test_runs/ and can never write into data/.
"""
from pathlib import Path

from . import common as C
from . import runner_core as rc


def paths(work_dir=None):
    if work_dir is None:
        return {"real": True, "failures": C.STAGE1_FAILURES,
                "responses": C.DATA_PROC / "stage1_failure_responses.jsonl", "lock": C.STAGE1_FAILURES_LOCK,
                "requests": C.STAGE2_REQUESTS, "order": C.STAGE2_ORDER, "manifest": C.STAGE2_MANIFEST,
                "review_dir": C.DATA_PROC / "annotation_review", "review_export_lock": C.DATA_MAN / "ANNOTATION_REVIEW_EXPORT.json",
                "review_decisions": C.DATA_PROC / "annotation_review_decisions.json",
                "review_lock": C.DATA_MAN / "ANNOTATION_REVIEW_FROZEN.json"}
    w = Path(work_dir).resolve()
    if C.TEST_RUNS_ROOT.resolve() not in w.parents:
        raise SystemExit(f"STOP: test work dirs must be under {C.TEST_RUNS_ROOT}")
    return {"real": False, "failures": w / "stage1_failures.json", "responses": w / "stage1_failure_responses.jsonl",
            "lock": w / "STAGE1_FAILURES_FROZEN.json", "requests": w / "stage2_requests.jsonl",
            "order": w / "stage2_execution_order.json", "manifest": w / "stage2_request_manifest.json",
            "review_dir": w / "annotation_review", "review_export_lock": w / "ANNOTATION_REVIEW_EXPORT.json",
            "review_decisions": w / "annotation_review_decisions.json", "review_lock": w / "ANNOTATION_REVIEW_FROZEN.json"}


def verify_lock(P):
    """The failure set must be locked and unchanged. -> lock dict"""
    if not P["lock"].exists():
        raise SystemExit("STOP: the Stage-1 failure set is not frozen (run scripts/07_freeze_stage1_failures.py)")
    lock = C.read_json(P["lock"])
    for k in ("failures", "responses"):
        got = C.sha256_file(P[k])
        if got != lock[f"{k}_sha256"]:
            raise SystemExit(f"STOP: {P[k].name} changed after freezing ({got} != {lock[f'{k}_sha256']})")
    return lock


def verify_review_lock(P):
    """The blinded annotation-review decisions must be frozen, unchanged, and tied to the frozen failure set."""
    lock = verify_lock(P)
    if not P["review_lock"].exists():
        raise SystemExit("STOP: annotation-review decisions are not frozen (scripts/07a + manual review + 07b)")
    rl = C.read_json(P["review_lock"])
    if rl["failures_sha256"] != lock["failures_sha256"]:
        raise SystemExit("STOP: annotation review belongs to a different failure set")
    for key, path in (("decisions_sha256", P["review_decisions"]),
                      ("filled_form_sha256", P["review_dir"] / "annotation_review_form_FILLED.csv")):
        if C.sha256_file(path) != rl[key]:
            raise SystemExit(f"STOP: {path.name} changed after the review was frozen")
    return C.read_json(P["review_decisions"]), rl


def frozen_inputs(P):
    lock = verify_lock(P)
    verify_review_lock(P)
    if P["real"]:
        C.verify_frozen([C.rel(P["requests"]), C.rel(P["order"]), C.rel(C.ITEMS_FILE)] + C.AMENDED_PROTOCOL_FILES)
    by_key, order = rc.load_requests(P["requests"], P["order"])
    hashes = {"items": C.sha256_file(C.ITEMS_FILE), "requests": C.sha256_file(P["requests"]),
              "order": C.sha256_file(P["order"]), "upstream": lock["failures_sha256"]}
    return by_key, order, hashes, lock


def smoke_keys(by_key, order, cfg):
    """n_items genuine Stage-1 failures x all conditions: the item with the longest Stage-1 response first,
    then items in Stage-2 execution order."""
    n = cfg["stage2"]["smoke"]["n_items"]
    items = []
    longest = max(by_key.values(), key=lambda r: (r["prompt_chars"], r["request_key"]))["item_id"]
    items.append(longest)
    for o in order["order"]:
        iid = by_key[o["request_key"]]["item_id"]
        if len(items) >= n:
            break
        if iid not in items:
            items.append(iid)
    return [o["request_key"] for o in order["order"] if by_key[o["request_key"]]["item_id"] in items]


SMOKE_SELECTION = C.DATA_MAN / "stage2_smoke_selection.json"
SMOKE_GO = C.DATA_MAN / "STAGE2_SMOKE_GO.json"
SMOKE_NOGO_V9 = C.DATA_MAN / "STAGE2_SMOKE_V9_NOGO.json"


def require_smoke_go():
    """A7 full-run lock: the real full Stage-2 run needs a registered record of a NEW Stage-2 smoke archive that
    received GO under the A7 pipeline/integrity gate. -> that record. Refuses otherwise (bundle v12 has none)."""
    from . import smoke_gate as sg
    if not SMOKE_GO.exists():
        raise SystemExit("STOP: full Stage 2 is locked - no Stage-2 smoke GO record (data/manifests/STAGE2_SMOKE_GO.json)")
    C.verify_frozen([C.rel(SMOKE_GO), C.rel(SMOKE_NOGO_V9)])
    rec = C.read_json(SMOKE_GO)
    if rec.get("gate_decision") != "GO" or rec.get("gate_version") != sg.A7_GATE:
        raise SystemExit("STOP: full Stage 2 is locked - the registered smoke record is not a GO under the A7 gate")
    if rec.get("archive_sha256") == C.read_json(SMOKE_NOGO_V9)["archive_sha256"]:
        raise SystemExit("STOP: full Stage 2 is locked - that is the preserved v9 NO-GO smoke archive")
    return rec


TRUNCATION_RULE = ("researcher request 2026-10-14 (before any Stage-2 inference): the smoke must contain at least one "
                   "item whose Stage-1 response hit max_new_tokens (stage1_truncated), because Stage 2 embeds that "
                   "response verbatim. If the base selection has none, the first execution-order pick (never the "
                   "longest-prompt context probe) is replaced by the request-set item with stage1_truncated whose "
                   "longest Stage-2 prompt is largest (prompt_chars, then item_id). Requests / order are unchanged.")


def smoke_keys_with_truncation(by_key, order, cfg, failures):
    """-> (keys, info). Base smoke_keys selection + the truncated-Stage-1 edge case (TRUNCATION_RULE)."""
    base = smoke_keys(by_key, order, cfg)
    trunc = {f["item_id"] for f in failures if f.get("stage1_truncated")}
    items = list(dict.fromkeys(by_key[k]["item_id"] for k in base))
    longest = max(by_key.values(), key=lambda r: (r["prompt_chars"], r["request_key"]))["item_id"]
    info = {"base_items": sorted(items), "replaced": None, "replacement": None}
    if not trunc & set(items):
        cands = {}
        for r in by_key.values():
            if r["item_id"] in trunc:
                cands[r["item_id"]] = max(cands.get(r["item_id"], 0), r["prompt_chars"])
        if not cands:
            raise SystemExit("STOP: no Stage-1-truncated item in the Stage-2 request set")
        repl = max(cands, key=lambda i: (cands[i], i))
        out = next(i for i in (by_key[o["request_key"]]["item_id"] for o in order["order"])
                   if i in items and i != longest)
        items[items.index(out)] = repl
        info.update(replaced=out, replacement=repl)
    keys = [o["request_key"] for o in order["order"] if by_key[o["request_key"]]["item_id"] in items]
    info["items"] = sorted(items)
    info["stage1_truncated_items"] = sorted(trunc & set(items))
    return keys, info


def rule_is_frozen(cfg):
    import json
    if not C.RULE_LOCK.exists():
        return False
    lock = C.read_json(C.RULE_LOCK)
    content = {"interpretation_rule": cfg["interpretation_rule"], "analysis": cfg["analysis"]}
    return lock.get("rule_sha256") == C.sha256_text(json.dumps(content, sort_keys=True))


def run(solver_kind, run_id, output_root, P, mode="full", keys=None, resume_from=None, force_unlock=False,
        limit=None, log=print, mock_profile=None):
    from . import solver_loader as sl
    cfg = C.load_config()
    by_key, order, hashes, _ = frozen_inputs(P)
    keys = keys or [o["request_key"] for o in order["order"]]
    backend, bname, test_only, sc, sha, ov = sl.make_solver(solver_kind, cfg, list(by_key.values()), mock_profile)
    if not test_only and not P["real"]:
        raise SystemExit("STOP: the real solver only runs on the frozen (real) Stage-2 request set")
    if not test_only and mode == "full" and not rule_is_frozen(cfg):
        raise SystemExit("STOP: the Stage-B interpretation rule must be approved and frozen "
                         "(scripts/freeze_interpretation_rule.py) BEFORE full Stage-2 inference")
    root = Path(output_root) if output_root else (C.TEST_RUNS_ROOT if test_only else C.RUNS_ROOT)
    run_dir = root / run_id
    man = rc.run_manifest(run_id, 2, mode, test_only, bname, cfg, sc, sha, ov, hashes, keys)
    if resume_from:
        n, note = rc.import_previous(run_dir, resume_from, man, by_key)
        log(f"imported {n} completed rows from {resume_from} {note or ''}")
    stats = rc.execute(run_dir, by_key, order, keys, backend, man, cfg["runner"], log, force_unlock, limit)
    diag, _ = rc.score_run(run_dir)
    return run_dir, stats, diag, backend
