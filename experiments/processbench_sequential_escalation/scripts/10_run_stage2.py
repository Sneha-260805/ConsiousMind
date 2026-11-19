"""Full Stage-2 run on EXACTLY the frozen failure set (all conditions; crash-safe, resumable). Requires researcher
approval of the Stage-1 report and Stage-2 smoke gate, and a frozen interpretation rule. The real run refuses unless
data/manifests/STAGE2_SMOKE_GO.json (registered) records a NEW smoke archive with decision GO under the A7 gate.

    python scripts/10_run_stage2.py --solver qwen --output-root /kaggle/working/stage_b_runs [--resume-from ...]
    python scripts/10_run_stage2.py --solver mock --work-dir outputs/test_runs/<x>/stage2_inputs     # TEST ONLY
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import stage2_runner as s2r

DEFAULT_RUN_ID = "stage_b_stage2_qwen3_4b"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", choices=["mock", "qwen"], required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--resume-from", default=None)
    ap.add_argument("--force-unlock", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    P = s2r.paths(a.work_dir)
    if a.solver == "qwen":                       # A7: locked until a NEW smoke archive received GO (registered record)
        s2r.require_smoke_go()
    run_id = a.run_id or (DEFAULT_RUN_ID if a.solver == "qwen" else "test_stage2_mock")
    run_dir, stats, diag, _ = s2r.run(a.solver, run_id, a.output_root, P, "full", None, a.resume_from,
                                      a.force_unlock, a.limit)
    print(json.dumps({"run_dir": str(run_dir), "stats": stats,
                      "integrity": {k: diag[k] for k in ("completed", "planned", "duplicate_keys",
                                                         "transport_failure_records", "numeric_parse_status",
                                                         "completed_by_condition")},
                      "truncated_at_max_new_tokens": len(diag["truncated_at_max_new_tokens"])}, indent=1))
    return 0 if stats["complete"] else 3


if __name__ == "__main__":
    sys.exit(main())
