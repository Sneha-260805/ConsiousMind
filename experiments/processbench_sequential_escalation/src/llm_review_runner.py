"""A3 LLM-assisted review: frozen request files, pass runs (crash-safe runner_core.execute: a completed request is
never regenerated), judgments and the 5-item reviewer smoke checks.

Frozen files (written once by scripts/14_build_llm_review_requests.py, registered in FROZEN_HASHES.json):
  data/processed/llm_review/pass1_requests.jsonl, pass2_requests.jsonl, review_order.json
  data/manifests/llm_review_request_manifest.json
Pass 2 is bound to the completed Pass-1 raw outputs (upstream_sha256) and refuses to start before Pass 1 is complete.
"""
import json
from pathlib import Path

from . import common as C
from . import llm_review as lr
from . import runner_core as rc
from . import stage2_runner as s2r

REVIEW_DIR = C.DATA_PROC / "llm_review"
PASS_FILES = {1: REVIEW_DIR / "pass1_requests.jsonl", 2: REVIEW_DIR / "pass2_requests.jsonl"}
ORDER_FILE = REVIEW_DIR / "review_order.json"
REQUEST_MANIFEST = C.DATA_MAN / "llm_review_request_manifest.json"
FROZEN_FILES = [PASS_FILES[1], PASS_FILES[2], ORDER_FILE, REQUEST_MANIFEST]
CODE_FILES = ["src/llm_review.py", "src/reviewer_loader.py", "src/llm_review_runner.py"]


def frozen_inputs():
    """-> (by_key {pass: {key: req}}, order, hashes); everything verified against FROZEN_HASHES.json."""
    lock = s2r.verify_lock(s2r.paths(None))
    C.verify_frozen([C.rel(p) for p in FROZEN_FILES] + C.AMENDED_PROTOCOL_FILES + CODE_FILES
                    + [C.rel(C.ITEMS_FILE), C.rel(C.STAGE1_FAILURES)])
    order = C.read_json(ORDER_FILE)
    by_key = {p: {r["request_key"]: r for r in C.read_jsonl(PASS_FILES[p])} for p in (1, 2)}
    for p in (1, 2):
        for r in by_key[p].values():
            if C.sha256_text(r["prompt"]) != r["prompt_sha256"]:
                raise SystemExit(f"STOP: prompt hash mismatch {r['request_key']}")
        if [by_key[p][k]["item_id"] for k in (o["request_key"] for o in lr.runner_order(order, p)["order"])] \
                != order["item_ids"]:
            raise SystemExit("STOP: review order and request files disagree")
    if C.read_json(REQUEST_MANIFEST)["failures_sha256"] != lock["failures_sha256"]:
        raise SystemExit("STOP: review requests were built for a different failure set")
    hashes = {"requests": {p: C.sha256_file(PASS_FILES[p]) for p in (1, 2)}, "order": C.sha256_file(ORDER_FILE),
              "items": C.sha256_file(C.ITEMS_FILE), "failures": lock["failures_sha256"]}
    return by_key, order, hashes


def manifest(run_id, pass_no, mode, test_only, backend_name, cfg, hashes, keys, upstream):
    r = cfg["llm_review"]["reviewer"]
    return {
        "run_id": run_id, "stage": f"review_pass{pass_no}", "mode": mode, "test_only": test_only,
        "backend": backend_name, "solver_tag": r["reviewer_tag"], "model": r["model"],
        "model_revision": r["model_revision"], "frozen_solver_config_sha256": None,
        "solver_overrides": {"quantization": r["quantization"]}, "max_new_tokens": r["max_new_tokens"],
        "enable_thinking": None, "decoding": r["decoding"], "repetition_penalty": r["repetition_penalty"],
        "requests_sha256": hashes["requests"][pass_no], "execution_order_sha256": hashes["order"],
        "items_sha256": hashes["items"], "upstream_sha256": upstream,
        "planned_keys_sha256": C.sha256_text("\n".join(keys)), "n_planned": len(keys),
        "parser_version": lr.PROMPT_VERSION, "parser_sha256": C.sha256_file(C.STAGE_ROOT / "src" / "llm_review.py"),
        "protocol_amendments": [a["id"] for a in cfg.get("protocol_amendments", [])],
        "system_prompt_sha256": C.sha256_text(lr.SYSTEM_PROMPT), "failures_sha256": hashes["failures"],
        "request_protocol": "fixed system message + one user message; no history; batch size 1; no tools; no web",
        "retry_policy": "transport errors only; reviewer outputs are NEVER regenerated, whatever they say",
        "STATUS": cfg["llm_review"]["provisional_statement"], "created": C.now(),
    }


def run_pass(reviewer, backend_name, test_only, pass_no, run_dir, keys, by_key, order, hashes, cfg, mode,
             upstream=None, log=print):
    ro = lr.runner_order(order, pass_no)
    keyset = set(keys)
    sub = {"order": [o for o in ro["order"] if o["request_key"] in keyset], "order_sha256": ro["order_sha256"]}
    keys = [o["request_key"] for o in sub["order"]]
    man = manifest(Path(run_dir).name, pass_no, mode, test_only, backend_name, cfg, hashes, keys, upstream)
    stats = rc.execute(run_dir, by_key[pass_no], sub, keys, reviewer, man, cfg["runner"], log=log)
    return stats


def judgments(run_dir, pass_no, pool):
    """Parse every raw row with the fixed rules -> judgments_pass<p>.jsonl (no comparison with annotations)."""
    items = {i["item_id"]: i for i in pool}
    rows = C.read_jsonl(Path(run_dir) / "raw_outputs.jsonl")
    out = []
    for r in rows:
        meta = r["model_metadata"]["response"] or {}
        p = lr.parse_output(r["raw_response"], pass_no, items[r["item_id"]]["n_steps"])
        exact, why = lr.exact_schema(r["raw_response"], pass_no, items[r["item_id"]]["n_steps"])
        out.append({"request_key": r["request_key"], "item_id": r["item_id"], "domain": r["domain"],
                    "review_pass": pass_no, **p, "exact_schema": exact, "exact_schema_detail": why,
                    "raw_response_sha256": C.sha256_text(r["raw_response"]),
                    "prompt_sha256": r["prompt_sha256"], "finish_reason": meta.get("finish_reason"),
                    "new_tokens": meta.get("new_tokens"), "prompt_tokens": meta.get("prompt_tokens")})
    C.write_jsonl(Path(run_dir) / f"judgments_pass{pass_no}.jsonl", out)
    return out


def pass_complete(run_dir, n_planned):
    st = Path(run_dir) / "state.json"
    if not st.exists():
        return False
    s = C.read_json(st)
    rows = C.read_jsonl(Path(run_dir) / "raw_outputs.jsonl")
    return s["completed"] == s["planned"] == n_planned == len({r["request_key"] for r in rows})


def smoke_checks(pool, cfg, j1, j1r, j2, j2r, raw1, by_key, smoke_ids, reviewer_settings, resume_generated,
                 raw_identical):
    """5-item reviewer smoke gate. GO only if every check passes (no REVIEW tier); else NO-GO.
    A4: the *_exact_schema checks require every output (first run AND repeat) to be exactly one JSON object with
    exactly the allowed keys and valid values (lr.exact_schema); parse_output itself is unchanged."""
    rcfg = cfg["llm_review"]["reviewer"]
    items = {i["item_id"]: i for i in pool}
    p1 = [by_key[1][lr.request_key(1, i)] for i in smoke_ids]
    struct = lambda j: [(x["item_id"], x["parse_status"], x["judgment"], x["confidence"]) for x in j]
    p2 = [by_key[2][lr.request_key(2, i)] for i in smoke_ids]
    blind = lr.audit(p1, p2, pool) + [
        f"{r['request_key']}: Pass-1 request carries annotation fields" for r in p1
        if any(f in r for f in ("true_error_step", "false_location", "label_0indexed", "pointed_step"))]
    for x in raw1:
        it = items[x["item_id"]]
        if x["prompt"] != lr.pass1_prompt(lr.pass1_view(it)):
            blind.append(f"{x['request_key']}: sent Pass-1 prompt differs from the whitelisted build")
    toks = [x["prompt_tokens"] + rcfg["max_new_tokens"] for x in j1 + j2 if x["prompt_tokens"] is not None]
    checks = {
        "model_loaded_pinned_revision_nf4": reviewer_settings.get("TEST_ONLY") is True or (
            reviewer_settings.get("hub_resolved_revision") == rcfg["model_revision"]
            and reviewer_settings.get("is_loaded_in_4bit") is True
            and "nf4" in str(reviewer_settings.get("applied_quantization", {}).get("bnb_4bit_quant_type"))),
        "all_smoke_requests_completed": len(j1) == len(j2) == len(j1r) == len(j2r) == len(smoke_ids),
        "pass1_json_parses": all(x["parse_status"] in ("ok", "invalid_judgment", "invalid_confidence") for x in j1),
        "pass1_exact_schema": all(x["exact_schema"] for x in j1 + j1r),
        "pass1_earliest_error_step_valid": all(x["judgment"] is not None for x in j1),
        "pass1_confidence_valid": all(x["confidence"] in lr.CONFIDENCE for x in j1),
        "pass2_json_parses_and_valid": all(x["parse_status"] == "ok" for x in j2),
        "pass2_exact_schema": all(x["exact_schema"] for x in j2 + j2r),
        "pass2_candidate_step_verdict_valid": all(x["judgment"] in lr.PASS2_VERDICTS for x in j2),
        "pass1_no_hidden_annotation_exposed": not blind,
        "deterministic_repeat_same_structured_judgment": struct(j1) == struct(j1r) and struct(j2) == struct(j2r),
        "no_truncation": all(x["finish_reason"] == "stop" for x in j1 + j2 + j1r + j2r),
        "within_context_window": all(t <= rcfg["max_context_tokens"] for t in toks),
        "resume_generates_nothing": resume_generated == 0,
    }
    decision = "GO" if all(checks.values()) else "NO-GO"
    return {"decision": decision, "checks": checks,
            "reason": "all checks passed" if decision == "GO" else
            "failed: " + ", ".join(k for k, v in checks.items() if not v),
            "deterministic_repeat_identical_raw_text": raw_identical,
            "blinding_problems": blind, "max_prompt_plus_max_new_tokens": max(toks) if toks else None}


def package(root, zpath, extra_files=()):
    """Zip a review output folder (+ frozen request files) with ARCHIVE_INDEX.json (sha256 of every member) and a
    .sha256.txt sidecar. -> archive sha256"""
    import zipfile
    root, zpath = Path(root), Path(zpath)
    members = {f"{root.name}/{f.relative_to(root).as_posix()}": f.read_bytes()
               for f in sorted(root.rglob("*")) if f.is_file() and f.name != "run.lock"}
    for f in extra_files:
        members[f"frozen_inputs/{C.rel(f)}"] = Path(f).read_bytes()
    index = {"root": root.name, "created": C.now(), "amendment": "A3 (reviewer per A4)",
             "members_sha256": {k: C.sha256_bytes(v) for k, v in sorted(members.items())}}
    members["ARCHIVE_INDEX.json"] = json.dumps(index, indent=2).encode()
    from .persistence import atomic_zip
    atomic_zip(zpath, sorted(members.items()))
    with zipfile.ZipFile(zpath) as z:
        if z.testzip() is not None:
            raise SystemExit("STOP: archive corrupt")
    sha = C.sha256_file(zpath)
    Path(str(zpath) + ".sha256.txt").write_text(f"{sha}  {zpath.name}\n", encoding="utf-8")
    return sha


def read_archive(zpath):
    """-> {member: bytes} after checking every member against ARCHIVE_INDEX.json (and the sidecar if present)."""
    import zipfile
    zpath = Path(zpath)
    with zipfile.ZipFile(zpath) as z:
        if z.testzip() is not None:
            raise SystemExit("STOP: archive corrupt")
        data = {n: z.read(n) for n in z.namelist()}
    idx = json.loads(data["ARCHIVE_INDEX.json"])
    bad = [n for n, h in idx["members_sha256"].items() if C.sha256_bytes(data.get(n, b"")) != h]
    side = zpath.with_name(zpath.name + ".sha256.txt")
    if bad or (side.exists() and side.read_text().split()[0] != C.sha256_file(zpath)):
        raise SystemExit(f"STOP: archive integrity failed (bad members {bad[:5]}, sidecar)")
    return data
