"""Build and FREEZE the Stage-B item pool (+ ordered reserve) from ProcessBench. No model is involved.

    python scripts/02_build_dataset.py [--rebuild "<approved reason>"]
Writes (frozen; a re-run must be byte-identical):
  data/processed/candidates_all.jsonl     every inspected source record + its first failing eligibility rule
  data/processed/stage_b_items.json       pool = all eligible items (reserve empty)
  data/processed/stage_b_items_review.txt human-readable listing of every pool item (question, numbered trace,
                                          gold, original answer, true/false step)
  data/manifests/dataset_manifest.json    source provenance, eligibility rules, exclusion counts, distributions
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import dataset_builder as db
from src import frozen_sources as fs


def review_text(pool):
    out = [C.TEST_ONLY_BANNER.replace("TEST ONLY", "DATASET REVIEW LISTING (no model output)"), ""]
    for it in pool:
        fl = it["false_location"]
        out += [f"=== {it['item_id']}  [{it['domain']}"
                + (f" | {it['math_subject']} {it['math_level']}" if it["math_level"] else "") + f" | {it['generator']}]",
                "Problem:", it["question"], "", "Original proposed solution:", db.render_trace(it["steps"]), "",
                f"gold = {it['gold_answer']}  (source {it['reference_source']}[{it['reference_source_index']}], "
                f"raw {it['reference_raw']!r})",
                f"original final answer = {it['original_answer']}  (raw {it['original_answer_raw']!r})",
                f"annotated first error: Step {it['true_error_step']} of {it['n_steps']}  |  L_false step: "
                f"Step {fl['false_step']} (distance {fl['distance']})", ""]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", metavar="REASON", default=None,
                    help="deliberate, researcher-approved rebuild of frozen artifacts (reason is logged)")
    a = ap.parse_args()
    cfg = C.load_config()
    fs.check_solver_matches_stage_a(cfg)
    rows, pool, reserve = db.build(cfg)
    summary = db.summarise(rows, pool, reserve, cfg)
    if len(pool) != cfg["dataset"]["target_items"]:
        raise SystemExit(f"STOP: pool has {len(pool)} items, target {cfg['dataset']['target_items']}")
    items_doc = {"stage": "B", "config_version": cfg["config_version"],
                 "selection_seed": cfg["dataset"]["selection_seed"],
                 "note": "pool = ALL eligible items (Stage 1 runs on every one; researcher decision 2026-10-12). "
                         "reserve is empty by design.",
                 "n_pool": len(pool), "n_reserve": len(reserve), "pool": pool, "reserve": reserve}
    res = {}
    res[C.rel(C.CANDIDATES_FILE)] = C.write_frozen(C.CANDIDATES_FILE, C.write_jsonl, rows, bool(a.rebuild))
    res[C.rel(C.ITEMS_FILE)] = C.write_frozen(C.ITEMS_FILE, C.write_json, items_doc, bool(a.rebuild))
    rv = C.DATA_PROC / "stage_b_items_review.txt"
    rv.write_text(review_text(pool), encoding="utf-8", newline="\n")
    cfg_src = cfg["sources"]
    manifest = {
        "stage": "B", "built_from": "scripts/02_build_dataset.py", "config_dataset_sources_sha256": C.sha256_text(json.dumps([cfg["dataset"], cfg["sources"]], sort_keys=True)),
        "sources": {"processbench": cfg_src["processbench"], "gsm8k_reference": cfg_src["gsm8k_reference"],
                    "math_reference": cfg_src["math_reference"]},
        "eligibility_rules": db.__doc__,
        "summary": summary,
        "artifacts_sha256": {k: v[0] for k, v in res.items()},
        "oracle_status": C.ORACLE_STATEMENT, "limitations": C.LIMITATIONS,
        "validation_status": "Automated eligibility checks only; expert first-error annotations are ProcessBench's. "
                             "No human review of the pool has been done for Stage B.",
    }
    res[C.rel(C.DATASET_MANIFEST)] = C.write_frozen(C.DATASET_MANIFEST, C.write_json, manifest, bool(a.rebuild))
    C.update_frozen_hashes({k: v[0] for k, v in res.items()}, a.rebuild)
    print(json.dumps({k: v[1] for k, v in res.items()}, indent=1))
    print(json.dumps({k: summary[k] for k in ("source_records_inspected", "eligible_after_dedup", "pool_size",
                                              "reserve_size", "exclusion_reasons_first_failing_rule")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
