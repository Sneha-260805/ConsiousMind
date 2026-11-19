"""Stage-1 smoke test (real model on Kaggle, or --solver mock locally = TEST ONLY).

    python scripts/05_stage1_smoke_test.py --solver qwen --output-root /kaggle/working/stage_b_runs
    python scripts/05_stage1_smoke_test.py --solver mock                      # local, TEST ONLY

Runs the frozen smoke subset (stage1.smoke: the 4 longest Stage-1 prompts + alternating gsm8k/math, 20 requests)
into <output-root>/<run_label>_stage1_<solver>/ (run_label = stage1.smoke.run_label, "smoke3" since amendment A2:
max_new_tokens 2048, Stage-B parser unchanged; the smoke1 + smoke2 records are preserved unaltered) and checks: model load + protocol (real), 2+2 canary, prompt rendering
(sha), parser, generation length, TRUNCATION, persistence (fsync rows, state, checkpoints), resume (a second pass
generates nothing), deterministic repeat (first 2 smoke prompts regenerated; text must be identical). Writes
smoke_gate.json: GO (>= 19/20 parsed, all technical + truncation checks pass) / REVIEW (18/20 parsed) / NO-GO (exit
codes 0 / 4 / 2; see src/smoke_gate.py). No repair rates are computed. Smoke rows are NOT reused by the full run.
"""
import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from src import common as C
from src import runner_core as rc
from src import smoke_gate as sg
from src import stage1_runner as s1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", choices=["mock", "qwen"], required=True)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--force-unlock", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    by_key, order, _ = s1.frozen_inputs(cfg)
    keys = s1.smoke_keys(list(by_key.values()), order, cfg)
    frozen_smoke = C.read_json(C.STAGE1_MANIFEST)["smoke_request_keys"]
    if keys != frozen_smoke:
        raise SystemExit("STOP: smoke selection differs from the frozen manifest")
    run_id = f"{cfg['stage1']['smoke']['run_label']}_stage1_{a.solver}"
    run_dir, stats, diag, backend = s1.run(a.solver, run_id, a.output_root, mode="smoke", keys=keys,
                                           force_unlock=a.force_unlock)
    ddir = run_dir / "diagnostics"
    ddir.mkdir(exist_ok=True)
    test_only = a.solver == "mock"
    canary = None
    if not test_only:
        from src import solver_loader as sl
        canary = sl.canary(backend)
        C.write_json(ddir / "canary.json", canary)
        C.write_json(ddir / "effective_settings.json", {"effective": backend.effective_settings(),
                                                        "versions": backend.versions()})
    raw = {r["request_key"]: r for r in C.read_jsonl(run_dir / "raw_outputs.jsonl")}
    det = []
    for k in keys[: cfg["stage1"]["smoke"]["determinism_repeats"]]:
        again = backend.generate(by_key[k]["prompt"])
        det.append({"request_key": k, "identical_text": again.text == raw[k]["raw_response"],
                    "repeat_new_tokens": again.metadata.get("new_tokens"),
                    "first_new_tokens": raw[k]["model_metadata"]["response"].get("new_tokens")})
    C.write_json(ddir / "determinism.json", {"DIAGNOSTIC_ONLY": True, "repeats": det})
    man = C.read_json(run_dir / "run_manifest.json")
    again = rc.execute(run_dir, by_key, order, keys, backend, man, cfg["runner"], log=lambda m: None)
    ckpts = sorted(p.name for p in (run_dir / "checkpoints").glob("checkpoint_*.zip"))
    limit = man["max_new_tokens"]
    toks = [r["model_metadata"]["response"].get("new_tokens") or 0 for r in raw.values()]
    sm = cfg["stage1"]["smoke"]
    technical = {
        "all_completed": diag["completed"] == len(keys) == stats["completed"],
        "no_persistent_technical_failures": stats["skipped_after_transport_failures"] == 0
        and not list(run_dir.glob("runtime_failure_*.json")),
        "no_duplicate_keys": diag["duplicate_keys"] == 0,
        "prompts_match_frozen_sha": diag["prompts_match_sha"]
        and all(raw[k]["prompt_sha256"] == by_key[k]["prompt_sha256"] for k in keys),
        "no_visible_thinking": not diag["visible_think_tags"],
        "deterministic_repeat_identical": all(d["identical_text"] for d in det),
        "resume_generates_nothing": again["generated"] == 0,
        "checkpoint_final_written": "checkpoint_FINAL.zip" in ckpts,
        "state_json_complete": C.read_json(run_dir / "state.json")["completed"] == len(keys),
    }
    if canary is not None:
        technical["canary_passed"] = canary["passed"]
    truncation = {"no_truncation_at_max_new_tokens": not diag["truncated_at_max_new_tokens"],
                  "no_response_over_90pct_of_limit": max(toks) <= sm["headroom_max_fraction"] * limit}
    n_parsed = diag["numeric_parse_status"].get("ok", 0)
    verdict = sg.decide(technical, truncation, len(keys), n_parsed, len(keys) - sm["parse_go_min"],
                        len(keys) - sm["parse_review_min"])
    gate = {"TEST_ONLY": test_only, "banner": C.TEST_ONLY_BANNER if test_only else "REAL MODEL SMOKE TEST",
            "smoke_run_label": cfg["stage1"]["smoke"]["run_label"],
            "protocol_amendments": man.get("protocol_amendments"), "parser_version": man.get("parser_version"),
            "parser_sha256": man.get("parser_sha256"),
            "headroom_limit_tokens": sm["headroom_max_fraction"] * limit,
            "LABEL": "SMOKE GATE - integrity only; no repair rates", "run_dir": str(run_dir),
            "n_smoke_requests": len(keys), "technical_checks": technical, "truncation_checks": truncation,
            **verdict, "transport_failures_retried": stats["transport_failures"],
            "unparsed_request_keys": [s["request_key"] for s in C.read_jsonl(run_dir / "scored_outputs.jsonl")
                                      if s["numeric_parse_status"] != "ok"],
            "max_new_tokens": limit, "generated_tokens_max": max(toks), "generated_tokens_mean": diag["generated_tokens_mean"],
            "generation_headroom_tokens": limit - max(toks), "prompt_tokens_max": diag["prompt_tokens_max"],
            "truncated": diag["truncated_at_max_new_tokens"], "numeric_parse_status": diag["numeric_parse_status"],
            "checkpoints": ckpts, "gate_rule": sg.__doc__}
    C.write_json(run_dir / "smoke_gate.json", gate)
    print(json.dumps(gate, indent=1))
    return sg.EXIT_CODES[gate["decision"]]


if __name__ == "__main__":
    sys.exit(main())
