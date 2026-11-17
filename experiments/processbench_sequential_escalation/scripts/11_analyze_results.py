"""Stage-B analysis from a complete Stage-1 run + the frozen failure set + the frozen blinded annotation-review
decisions + a complete Stage-2 run.

    python scripts/11_analyze_results.py --stage1-run <dir|zip> --stage2-run <dir|zip> --out-dir <dir>
    python scripts/11_analyze_results.py ... --work-dir outputs/test_runs/<x>/stage2_inputs      # TEST ONLY
Writes stage1_metrics.csv, stage1_failures.csv, stage2_rescue_metrics.csv, stage2_paired_comparisons.csv,
failure_decomposition.csv, cost_and_tokens.csv, truncation_report.csv, item_level_sequential_outcomes.csv,
summary.json, REPORT.md and figures/ (if matplotlib is available). Mock inputs -> every file says
"NO REAL MODEL RESULTS YET - TEST ONLY" and no interpretation label is produced.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import analysis as an
from src import common as C
from src import figures
from src import scorer
from src import stage1_runner as s1
from src import stage2_builder as s2b
from src import stage2_runner as s2r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage1-run", required=True)
    ap.add_argument("--stage2-run", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    lock = s2r.verify_lock(P)
    review, rlock = s2r.verify_review_lock(P)
    by1, order1, h1 = s1.frozen_inputs(cfg)
    rows1, man1, sha1 = s2b.load_stage1_run(a.stage1_run)
    probs = s2b.verify_stage1_complete(rows1, man1, by1, order1, h1)
    if probs:
        raise SystemExit("STOP: Stage-1 run invalid:\n" + "\n".join(probs))
    by2, order2, h2, _ = s2r.frozen_inputs(P)
    rows2, man2, sha2 = s2b.load_stage1_run(a.stage2_run)            # same loader works for any run
    if man2.get("stage") != 2 or man2.get("mode") != "full" or man2.get("requests_sha256") != h2["requests"] \
            or man2.get("upstream_sha256") != lock["failures_sha256"]:
        raise SystemExit("STOP: Stage-2 run does not belong to the frozen Stage-2 request set / failure set")
    if sorted(r["request_key"] for r in rows2) != sorted(by2):
        raise SystemExit("STOP: Stage-2 run incomplete or has unknown keys (no partial analysis)")
    test_only = bool(man1.get("test_only") or man2.get("test_only") or lock.get("TEST_ONLY"))
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    T, summary = an.analyse(pool, scorer.score_rows(rows1), C.read_json(P["failures"]), review,
                            scorer.score_rows(rows2), cfg, test_only)
    summary["inputs"] = {"stage1_source_sha256": sha1, "stage2_source_sha256": sha2,
                         "failures_sha256": lock["failures_sha256"], "stage2_requests_sha256": h2["requests"],
                         "annotation_review_decisions_sha256": rlock["decisions_sha256"]}
    out = an.write_outputs(a.out_dir, T, summary)
    fig = figures.make_all(out, T, summary)
    summary["figures"] = fig
    C.write_json(out / "summary.json", summary)
    print(json.dumps({k: summary[k] for k in ("banner", "n_pool", "n_stage1_failures", "n_review_excluded", "n_primary_stage2", "stage1_repair_rate",
                                              "rescue", "final_repair", "interpretation", "figures")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
