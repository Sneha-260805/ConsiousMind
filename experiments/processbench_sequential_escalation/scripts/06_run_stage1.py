"""Full Stage-1 run: V on ALL frozen pool items (crash-safe, resumable). Needs explicit researcher approval of the
Stage-1 smoke gate first.

    python scripts/06_run_stage1.py --solver qwen --output-root /kaggle/working/stage_b_runs
    python scripts/06_run_stage1.py --solver qwen ... --resume-from <previous checkpoint/FINAL zip or run dir>
    python scripts/06_run_stage1.py --solver mock                              # local, TEST ONLY
Rerunning the same command resumes; completed requests are never regenerated. After the pass the run is scored
(integrity diagnostics only) and a completeness check is printed. STOP afterwards: freeze failures (07) and report.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import stage1_runner as s1

DEFAULT_RUN_ID = "stage_b_stage1_qwen3_4b"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", choices=["mock", "qwen"], required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--resume-from", default=None)
    ap.add_argument("--force-unlock", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="debugging only: stop after N new requests")
    a = ap.parse_args()
    run_id = a.run_id or (DEFAULT_RUN_ID if a.solver == "qwen" else "test_stage1_mock")
    run_dir, stats, diag, _ = s1.run(a.solver, run_id, a.output_root, "full", None, a.resume_from,
                                     a.force_unlock, a.limit)
    out = {"run_dir": str(run_dir), "stats": stats, "complete": stats["complete"],
           "integrity": {k: diag[k] for k in ("completed", "planned", "duplicate_keys", "transport_failure_records",
                                              "numeric_parse_status", "visible_think_tags", "prompts_match_sha")},
           "truncated_at_max_new_tokens": len(diag["truncated_at_max_new_tokens"]),
           "next": "scripts/07_freeze_stage1_failures.py --source <run_dir or FINAL zip>, then STOP and report"
           if stats["complete"] else "INCOMPLETE: rerun the same command to resume"}
    print(json.dumps(out, indent=1))
    return 0 if stats["complete"] else 3


if __name__ == "__main__":
    sys.exit(main())
