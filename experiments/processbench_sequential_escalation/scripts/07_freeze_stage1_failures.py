"""Freeze the Stage-1 failure set from a COMPLETE Stage-1 run, BEFORE any Stage-2 prompt exists.

    python scripts/07_freeze_stage1_failures.py --source <run dir | checkpoint_FINAL.zip | *_FINAL.zip>
    python scripts/07_freeze_stage1_failures.py --source <mock run> --work-dir outputs/test_runs/<x>/stage2_inputs
Real runs write data/processed/stage1_failures.json + stage1_failure_responses.jsonl, lock both hashes in
data/manifests/STAGE1_FAILURES_FROZEN.json and register them in FROZEN_HASHES.json. Re-running on the same source
is idempotent; a different result after locking STOPS. Also writes stage1_failures.csv next to the failures file
and prints the pre-specified failure-count gate. STOP afterwards and report to the researcher.
"""
import argparse
import csv
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import stage1_runner as s1
from src import stage2_builder as s2b
from src import stage2_runner as s2r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--work-dir", default=None, help="TEST ONLY location (under outputs/test_runs)")
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(a.work_dir)
    by_key, order, hashes = s1.frozen_inputs(cfg)
    rows, man, src_sha = s2b.load_stage1_run(a.source)
    if P["real"] and man.get("test_only"):
        raise SystemExit("STOP: a TEST-ONLY (mock) run can never be frozen into data/processed; use --work-dir")
    probs = s2b.verify_stage1_complete(rows, man, by_key, order, hashes)
    if probs:
        raise SystemExit("STOP: Stage-1 run is not complete/valid; never freeze from a partial run:\n" + "\n".join(probs))
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    fails, resp, gate = s2b.failures_from_rows(rows, len(pool), cfg)
    doc = s2b.failures_doc(fails, gate, src_sha, man, bool(man.get("test_only")))
    P["failures"].parent.mkdir(parents=True, exist_ok=True)
    if P["lock"].exists():
        lock = C.read_json(P["lock"])
        tmp_f = P["failures"].with_name("_recheck_failures.json")
        tmp_r = P["responses"].with_name("_recheck_responses.jsonl")
        C.write_json(tmp_f, doc)
        C.write_jsonl(tmp_r, resp)
        same = C.sha256_file(tmp_f) == lock["failures_sha256"] and C.sha256_file(tmp_r) == lock["responses_sha256"]
        tmp_f.unlink()
        tmp_r.unlink()
        if not same:
            raise SystemExit("STOP: the Stage-1 failure set is already FROZEN and this source gives a different set. "
                             "The frozen set is never modified.")
        print("failure set already frozen; recomputation is byte-identical")
    else:
        f_sha = C.write_json(P["failures"], doc)
        r_sha = C.write_jsonl(P["responses"], resp)
        lock = {"frozen": C.now(), "TEST_ONLY": doc["TEST_ONLY"], "failures_file": P["failures"].name,
                "failures_sha256": f_sha, "responses_file": P["responses"].name, "responses_sha256": r_sha,
                "source": str(a.source), "source_sha256": src_sha, "source_run_id": man["run_id"],
                "n_failures": len(fails), "failure_gate": gate,
                "rule": "Stage-2 eligibility = Stage-1 V failure. Frozen before any Stage-2 prompt is built; never "
                        "modified after Stage-2 outcomes are seen."}
        C.write_json(P["lock"], lock)
        if P["real"]:
            C.update_frozen_hashes({C.rel(P["failures"]): f_sha, C.rel(P["responses"]): r_sha,
                                    C.rel(P["lock"]): C.sha256_file(P["lock"])})
    with open(P["failures"].with_name("stage1_failures.csv"), "w", encoding="utf-8", newline="") as fh:
        if doc["TEST_ONLY"]:
            fh.write(f"# {C.TEST_ONLY_BANNER}\n")
        w = csv.DictWriter(fh, fieldnames=list(fails[0].keys()) if fails else ["item_id"])
        w.writeheader()
        w.writerows(fails)
    print(json.dumps({"TEST_ONLY": doc["TEST_ONLY"], "n_stage1_items": len(pool), "n_failures": len(fails),
                      "failures_by_domain": {d: sum(f["domain"] == d for f in fails) for d in cfg["dataset"]["domains_used"]},
                      "failures_by_outcome": {o: sum(f["stage1_outcome"] == o for f in fails)
                                              for o in ("original_answer_retention", "new_wrong_answer", "parse_failure")},
                      "failure_gate": gate, "lock": str(P["lock"]), "failures_sha256": C.read_json(P["lock"])["failures_sha256"],
                      "NEXT": "STOP - report the Stage-1 failure count to the researcher before building Stage 2"},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
