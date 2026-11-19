"""Freeze the Stage-B interpretation rule (configs/stage_b.json -> interpretation_rule). Run ONLY after the
researcher has approved the rule, and BEFORE any Stage-2 inference. Full Stage-2 inference refuses to start
without this lock; once written, a changed rule makes the lock invalid (and Stage 2 refuses again).

    python scripts/freeze_interpretation_rule.py --approved-by "<researcher>"
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C


def locked_content(cfg):
    return {"interpretation_rule": cfg["interpretation_rule"], "analysis": cfg["analysis"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--replace-reason", default=None,
                    help="only before any real model output: replace an existing lock (logged in FROZEN_HASHES)")
    a = ap.parse_args()
    cfg = C.load_config()
    rule = locked_content(cfg)
    sha = C.sha256_text(json.dumps(rule, sort_keys=True))
    if C.RULE_LOCK.exists():
        old = C.read_json(C.RULE_LOCK)
        if old["rule_sha256"] == sha:
            print("rule already frozen (identical)")
            return 0
        if not a.replace_reason or C.real_runs_exist():
            raise SystemExit("STOP: a DIFFERENT interpretation rule is already frozen; it is never changed")
    if any(p.exists() for p in [C.STAGE2_REQUESTS]) and (C.RUNS_ROOT / "stage_b_stage2_qwen3_4b").exists():
        raise SystemExit("STOP: Stage-2 outputs already exist; the rule must be frozen before Stage-2 inference")
    C.write_json(C.RULE_LOCK, {"frozen": C.now(), "approved_by": a.approved_by, "rule_sha256": sha,
                               "locked": "interpretation_rule + analysis settings (bootstrap resamples/seed/CI level, "
                                         "primary and secondary comparisons)", **rule})
    C.update_frozen_hashes({C.rel(C.RULE_LOCK): C.sha256_file(C.RULE_LOCK)}, a.replace_reason)
    print(json.dumps({"frozen": True, "rule_sha256": sha}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
