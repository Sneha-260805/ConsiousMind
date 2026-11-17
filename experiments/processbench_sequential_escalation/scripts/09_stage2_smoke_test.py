"""Stage-2 smoke test on genuine Stage-1 failures (3 items x 4 conditions = 12 requests).

    python scripts/09_stage2_smoke_test.py --solver qwen --output-root /kaggle/working/stage_b_runs
    python scripts/09_stage2_smoke_test.py --solver mock --work-dir outputs/test_runs/<x>/stage2_inputs   # TEST ONLY
Checks: previous repair inserted verbatim and identically in all 4 prompts of an item, ORIGINAL-solution step
references unambiguous (prompt audit on the smoke items), parser, truncation / token headroom, persistence, resume,
deterministic repeat. Writes smoke_gate.json. No rescue rates are computed.
Gate = amendment A7 (src/smoke_gate.decide_stage2_a7): pipeline / integrity criteria only. A response that reaches the
frozen 2048-token cap is genuine model behaviour (a failed repair if no valid answer parses), NOT a technical NO-GO,
provided it completed normally and was persisted correctly; truncation / headroom are reported descriptively. The
parse thresholds (GO <= 1 unparsed / REVIEW 2 / NO-GO) apply to the normally finished responses (finish_reason stop).
The preserved v9 NO-GO (pre-A7 gate) is never rescored.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import parser
from src import runner_core as rc
from src import smoke_gate as sg
from src import stage2_builder as s2b
from src import stage2_runner as s2r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", choices=["mock", "qwen"], required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--force-unlock", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    by_key, order, _, _ = s2r.frozen_inputs(P)
    doc = C.read_json(P["failures"])
    keys, sel_info = s2r.smoke_keys_with_truncation(by_key, order, cfg, doc["failures"])
    if P["real"]:                         # the real smoke must run exactly the frozen selection
        C.verify_frozen([C.rel(s2r.SMOKE_SELECTION)])
        if C.read_json(s2r.SMOKE_SELECTION)["request_keys"] != keys:
            raise SystemExit("STOP: smoke selection differs from data/manifests/stage2_smoke_selection.json")
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    smoke_items = {by_key[k]["item_id"] for k in keys}
    resp = [r for r in C.read_jsonl(P["responses"]) if r["item_id"] in smoke_items]
    review, _ = s2r.verify_review_lock(P)
    decisions = {d["item_id"]: d for d in review["decisions"] if d["item_id"] in smoke_items}
    probs = s2b.audit([by_key[k] for k in keys], pool, [f for f in doc["failures"] if f["item_id"] in smoke_items],
                      resp, decisions, cfg["stage2"]["conditions"])
    run_dir, stats, diag, backend = s2r.run(a.solver, f"smoke_stage2_{a.solver}", a.output_root, P, mode="smoke",
                                            keys=keys, force_unlock=a.force_unlock)
    raw = {r["request_key"]: r for r in C.read_jsonl(run_dir / "raw_outputs.jsonl")}
    det = []
    for k in keys[:2]:
        again = backend.generate(by_key[k]["prompt"])
        det.append({"request_key": k, "identical_text": again.text == raw[k]["raw_response"]})
    (run_dir / "diagnostics").mkdir(exist_ok=True)
    C.write_json(run_dir / "diagnostics" / "determinism.json", {"DIAGNOSTIC_ONLY": True, "repeats": det})
    man = C.read_json(run_dir / "run_manifest.json")
    again = rc.execute(run_dir, by_key, order, keys, backend, man, cfg["runner"], log=lambda m: None)
    limit = man["max_new_tokens"]
    toks = [r["model_metadata"]["response"].get("new_tokens") or 0 for r in raw.values()]
    test_only = a.solver == "mock"
    sm = cfg["stage2"]["smoke"]
    if sm.get("gate") != sg.A7_GATE:
        raise SystemExit("STOP: configs/stage_b.json stage2.smoke.gate is not the A7 pipeline/integrity gate")
    technical = {
        "prompt_audit_passed": not probs,
        "all_completed": diag["completed"] == len(keys),
        "no_persistent_technical_failures": stats["skipped_after_transport_failures"] == 0
        and not list(run_dir.glob("runtime_failure_*.json")),
        "prompts_match_frozen_sha": all(raw[k]["prompt_sha256"] == by_key[k]["prompt_sha256"] for k in keys),
        "no_visible_thinking": not diag["visible_think_tags"],
        "deterministic_repeat_identical": all(d["identical_text"] for d in det),
        "resume_generates_nothing": again["generated"] == 0,
    }
    rows = {r["request_key"]: r for r in C.read_jsonl(run_dir / "raw_outputs.jsonl")}       # durable file, re-read
    scored = {r["request_key"]: r for r in C.read_jsonl(run_dir / "scored_outputs.jsonl")}
    per = {}
    for k in keys:
        m = (rows.get(k, {}).get("model_metadata") or {}).get("response") or {}
        per[k] = {"finish_reason": m.get("finish_reason"), "new_tokens": m.get("new_tokens"),
                  "numeric_parse_status": (scored.get(k) or {}).get("numeric_parse_status")}
    persistence = {
        "every_output_persisted_once": sorted(rows) == sorted(keys) and diag["duplicate_keys"] == 0,
        "persisted_text_reparses_identically": all(
            k in scored and isinstance(rows[k].get("raw_response"), str)
            and parser.parse(rows[k]["raw_response"], per[k]["finish_reason"] == "length")["numeric_parse_status"]
            == per[k]["numeric_parse_status"] for k in keys if k in rows),
        "finish_reason_recorded_stop_or_length": all(v["finish_reason"] in ("stop", "length") for v in per.values()),
        "token_count_recorded": all(isinstance(v["new_tokens"], int) for v in per.values()),
        "no_output_over_cap": all((v["new_tokens"] or 0) <= limit for v in per.values()),
        "cap_hits_exactly_at_cap": all(v["new_tokens"] == limit for v in per.values() if v["finish_reason"] == "length"),
        "parse_status_recorded": all(v["numeric_parse_status"] for v in per.values()),
        "effective_cap_is_frozen_2048": limit == 2048 or test_only,
    }
    cap_hit = sorted(k for k, v in per.items() if v["finish_reason"] == "length")
    stop = [k for k in keys if per[k]["finish_reason"] == "stop"]
    truncation_descriptive = {
        "LABEL": "DESCRIPTIVE ONLY under A7 - cap hits are genuine model behaviour and do not decide the gate",
        "cap_hit_request_keys": cap_hit, "n_cap_hit": len(cap_hit),
        "cap_hit_without_valid_answer": [k for k in cap_hit if per[k]["numeric_parse_status"] != "ok"],
        "responses_over_90pct_of_limit": sorted(k for k, v in per.items()
                                                if (v["new_tokens"] or 0) > sm["headroom_max_fraction"] * limit),
        "pre_A7_truncation_checks_would_be": {"no_truncation_at_max_new_tokens": not cap_hit,
                                              "no_response_over_90pct_of_limit": max(toks) <= sm["headroom_max_fraction"] * limit}}
    n_stop_parsed = sum(per[k]["numeric_parse_status"] == "ok" for k in stop)
    verdict = sg.decide_stage2_a7(technical, persistence, len(keys), len(stop), n_stop_parsed,
                                  sm["parse_go_max_failures"], sm["parse_review_max_failures"])
    gate = {"TEST_ONLY": test_only, "banner": C.TEST_ONLY_BANNER if test_only else "REAL MODEL STAGE-2 SMOKE TEST",
            "LABEL": "SMOKE GATE - integrity only; no rescue rates", "smoke_items": sorted(smoke_items), "smoke_selection": sel_info,
            "n_requests": len(keys), "technical_checks": technical, "persistence_checks": persistence,
            "truncation_descriptive": truncation_descriptive, "per_request": per, "protocol_amendment": "A7",
            "audit_problems": probs, **verdict, "max_new_tokens": limit,
            "generated_tokens_max": max(toks), "prompt_tokens_max": diag["prompt_tokens_max"],
            "truncated": diag["truncated_at_max_new_tokens"], "numeric_parse_status": diag["numeric_parse_status"]}
    C.write_json(run_dir / "smoke_gate.json", gate)
    print(json.dumps(gate, indent=1))
    return sg.EXIT_CODES[gate["decision"]]


if __name__ == "__main__":
    sys.exit(main())
