"""A3 full blinded LLM-assisted review: Pass 1 on all 76 frozen failures, THEN Pass 2. Only after an approved smoke.

    python scripts/16_run_llm_review.py --reviewer qwen_instruct --output-root /kaggle/working/stage_b_review   # Kaggle
    python scripts/16_run_llm_review.py --reviewer mock                                                     # TEST ONLY
Pass 1 runs in the frozen review order; when (and only when) all Pass-1 rows exist, PASS1_COMPLETE.json records the
Pass-1 raw-output sha256 and Pass 2 starts, bound to it (upstream_sha256). Raw outputs are fsync'ed row by row and
never regenerated; rerunning the same command in the same session resumes. No comparison with the ProcessBench
annotation happens here (that is done locally by scripts/17_freeze_llm_review.py after both passes).
Packs <output-root>/stage_b_llm_review_FINAL.zip (+ .sha256.txt).
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", choices=["mock", "qwen_instruct"], required=True)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--mock-profile", default="default", help="TEST ONLY")
    a = ap.parse_args()
    cfg = C.load_config()
    if a.reviewer != "mock" and cfg["llm_review"].get("stopped_by"):
        raise SystemExit(f"STOP: the LLM reviewer was stopped by amendment {cfg['llm_review']['stopped_by']}; the "
                         "annotation review is done by a human (scripts/18-20). Only the mock (TEST ONLY) runs.")
    by_key, order, hashes = lrr.frozen_inputs()
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    if a.reviewer == "mock":
        out = Path(a.output_root or C.TEST_RUNS_ROOT / "llm_review")
        if C.TEST_RUNS_ROOT.resolve() not in out.resolve().parents:
            raise SystemExit(f"STOP: mock output must be under {C.TEST_RUNS_ROOT}")
    else:
        if not a.output_root:
            raise SystemExit("STOP: --output-root is required for the real reviewer")
        out = Path(a.output_root)
    root = out / f"review_full_{a.reviewer}"
    reviewer, bname, test_only = rl.make_reviewer(a.reviewer, cfg, a.mock_profile)
    root.mkdir(parents=True, exist_ok=True)
    C.write_json(root / "reviewer_effective_settings.json", reviewer.effective_settings())
    n = len(order["item_ids"])
    d1, d2 = root / f"review_full_pass1_{a.reviewer}", root / f"review_full_pass2_{a.reviewer}"
    k1 = [lr.request_key(1, i) for i in order["item_ids"]]
    lrr.run_pass(reviewer, bname, test_only, 1, d1, k1, by_key, order, hashes, cfg, "full", upstream=hashes["failures"])
    if not lrr.pass_complete(d1, n):
        raise SystemExit("STOP: Pass 1 is incomplete; rerun the same command to resume. Pass 2 has NOT started.")
    j1 = lrr.judgments(d1, 1, pool)
    p1_sha = C.sha256_file(d1 / "raw_outputs.jsonl")
    done = root / "PASS1_COMPLETE.json"
    if done.exists():
        if C.read_json(done)["pass1_raw_outputs_sha256"] != p1_sha:
            raise SystemExit("STOP: Pass-1 raw outputs changed after Pass 1 was completed")
    else:
        C.write_json(done, {"completed": C.now(), "n": n, "pass1_raw_outputs_sha256": p1_sha,
                            "pass1_judgments_sha256": C.sha256_file(d1 / "judgments_pass1.jsonl"),
                            "note": "Pass 2 starts only after this file exists and is bound to this sha256"})
    k2 = [lr.request_key(2, i) for i in order["item_ids"]]
    lrr.run_pass(reviewer, bname, test_only, 2, d2, k2, by_key, order, hashes, cfg, "full", upstream=p1_sha)
    if not lrr.pass_complete(d2, n):
        raise SystemExit("STOP: Pass 2 is incomplete; rerun the same command to resume")
    j2 = lrr.judgments(d2, 2, pool)
    sha = lrr.package(root, out / "stage_b_llm_review_FINAL.zip", lrr.FROZEN_FILES)
    summary = {"TEST_ONLY": test_only, "n_items": n,
               "pass1_parse_status": {s: sum(x["parse_status"] == s for x in j1) for s in sorted({x["parse_status"] for x in j1})},
               "pass2_parse_status": {s: sum(x["parse_status"] == s for x in j2) for s in sorted({x["parse_status"] for x in j2})},
               "truncated": [x["request_key"] for x in j1 + j2 if x["finish_reason"] != "stop"],
               "archive": str(out / "stage_b_llm_review_FINAL.zip"), "archive_sha256": sha,
               "NEXT": "download the archive; locally: scripts/17_freeze_llm_review.py --source <zip>. STOP."}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
