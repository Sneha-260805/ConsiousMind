"""A3 reviewer smoke test (reviewer per A4: Qwen2.5-7B-Instruct): 5 frozen items, Pass 1 + Pass 2, each repeated once for determinism. No full review.

    python scripts/15_llm_review_smoke.py --reviewer qwen_instruct --output-root /kaggle/working/stage_b_review   # Kaggle
    python scripts/15_llm_review_smoke.py --reviewer mock                                                     # TEST ONLY
Items: llm_review_request_manifest.json -> smoke_item_ids (longest Pass-1 prompt, then alternating gsm8k / math in
review order). Checks (ALL must pass -> GO, exit 0; otherwise NO-GO, exit 2): model loads at the pinned revision in
4-bit NF4; Pass-1 JSON parses; earliest_error_step valid; confidence valid; Pass-2 JSON valid; no hidden annotation
in Pass 1 (whitelist + mutation audit of the exact prompts sent); the repeated run gives the same structured
judgments; no truncation; prompt + max_new_tokens within the registered context; a resume pass generates nothing.
A4: also requires every output (first run and repeat) to be exactly one JSON object with exactly the allowed keys.
Packs <output-root>/stage_b_llm_review_smoke_RESULTS.zip (+ .sha256.txt). Smoke rows are NOT reused by the full run.
"""
import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from src import common as C
from src import llm_review as lr
from src import llm_review_runner as lrr
from src import reviewer_loader as rl

EXIT = {"GO": 0, "NO-GO": 2}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", choices=["mock", "qwen_instruct"], required=True)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--mock-profile", default="good", help="TEST ONLY")
    a = ap.parse_args()
    cfg = C.load_config()
    if a.reviewer != "mock" and cfg["llm_review"].get("stopped_by"):
        raise SystemExit(f"STOP: the LLM reviewer was stopped by amendment {cfg['llm_review']['stopped_by']}; the "
                         "annotation review is done by a human (scripts/18-20). Only the mock (TEST ONLY) runs.")
    by_key, order, hashes = lrr.frozen_inputs()
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    smoke_ids = C.read_json(lrr.REQUEST_MANIFEST)["smoke_item_ids"]
    if smoke_ids != lr.smoke_items(list(by_key[1].values()), cfg["llm_review"]["smoke"]["n_items"]):
        raise SystemExit("STOP: smoke selection differs from the frozen manifest")
    if a.reviewer == "mock":
        out = Path(a.output_root or C.TEST_RUNS_ROOT / "llm_review")
        if C.TEST_RUNS_ROOT.resolve() not in out.resolve().parents and out.resolve() != C.TEST_RUNS_ROOT.resolve():
            raise SystemExit(f"STOP: mock output must be under {C.TEST_RUNS_ROOT}")
    else:
        if not a.output_root:
            raise SystemExit("STOP: --output-root is required for the real reviewer")
        out = Path(a.output_root)
    root = out / f"review_smoke_{a.reviewer}"
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"STOP: {root} already exists; the smoke test runs once into a fresh folder")
    root.mkdir(parents=True, exist_ok=True)
    reviewer, bname, test_only = rl.make_reviewer(a.reviewer, cfg, a.mock_profile)
    settings = reviewer.effective_settings()
    C.write_json(root / "reviewer_effective_settings.json", settings)
    dirs, js = {}, {}
    for p in (1, 2):
        keys = [lr.request_key(p, i) for i in smoke_ids]
        up = C.sha256_file(dirs[1, "a"] / "raw_outputs.jsonl") if p == 2 else hashes["failures"]
        for rep, mode in (("a", "smoke"), ("b", "smoke_repeat")):
            d = root / f"review_smoke_pass{p}{'' if rep == 'a' else '_repeat'}_{a.reviewer}"
            lrr.run_pass(reviewer, bname, test_only, p, d, keys, by_key, order, hashes, cfg, mode, upstream=up)
            dirs[p, rep] = d
            js[p, rep] = lrr.judgments(d, p, pool)
            if p == 1 and rep == "a":       # resume check: a second pass over the same folder generates nothing
                again = lrr.run_pass(reviewer, bname, test_only, 1, d, keys, by_key, order, hashes, cfg, mode,
                                     upstream=up)
    raw = lambda d: [(r["request_key"], r["raw_response"]) for r in C.read_jsonl(d / "raw_outputs.jsonl")]
    raw_identical = all(raw(dirs[p, "a"]) == raw(dirs[p, "b"]) for p in (1, 2))
    gate = lrr.smoke_checks(pool, cfg, js[1, "a"], js[1, "b"], js[2, "a"], js[2, "b"],
                            C.read_jsonl(dirs[1, "a"] / "raw_outputs.jsonl"), by_key, smoke_ids, settings,
                            again["generated"], raw_identical)
    gate = {"TEST_ONLY": test_only, "LABEL": "A3 REVIEWER SMOKE TEST - integrity only; not review results",
            "STATUS": cfg["llm_review"]["provisional_statement"], "reviewer": cfg["llm_review"]["reviewer"]["reviewer_tag"],
            "smoke_item_ids": smoke_ids, **gate,
            "judgments": {f"pass{p}": [{k: x[k] for k in ("item_id", "parse_status", "judgment", "confidence",
                                                           "new_tokens", "prompt_tokens", "finish_reason")}
                                       for x in js[p, "a"]] for p in (1, 2)},
            "n_generations": sum(len(v) for v in js.values())}
    C.write_json(root / "review_smoke_gate.json", gate)
    sha = lrr.package(root, out / "stage_b_llm_review_smoke_RESULTS.zip", lrr.FROZEN_FILES)
    print(json.dumps({k: gate[k] for k in ("decision", "reason", "checks", "deterministic_repeat_identical_raw_text",
                                           "max_prompt_plus_max_new_tokens", "judgments")}, indent=1))
    print("REVIEW SMOKE DECISION:", gate["decision"], "| archive", out / "stage_b_llm_review_smoke_RESULTS.zip",
          "| sha256", sha)
    return EXIT[gate["decision"]]


if __name__ == "__main__":
    sys.exit(main())
