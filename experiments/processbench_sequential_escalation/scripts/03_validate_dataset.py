"""Independent validation of the frozen Stage-B pool (re-derives every field from the raw sources).

    python scripts/03_validate_dataset.py
Checks, for every pool and reserve item: frozen hashes; source record exists and is unchanged; label + 1 ==
true_error_step; final_answer_correct is False; true_error_step >= 2; gold re-derived from the source benchmark;
original answer re-extracted and != gold; false step == true_error_step - 1 and >= 1 (strictly before the error);
no step-numbering conflict; unique problems; no overlap with the pilot/Stage-A GSM8K train problems; pool size and
all eligible items in the pool (no reserve); deterministic selection (rebuild byte-identical). Writes data/manifests/dataset_validation.json.
"""
import json
import sys
from decimal import Decimal

import _bootstrap  # noqa: F401
from src import common as C
from src import dataset_builder as db
from src import frozen_sources as fs
from src import processbench_adapter as pa


def main():
    cfg = C.load_config()
    C.verify_frozen([C.rel(C.CANDIDATES_FILE), C.rel(C.ITEMS_FILE), C.rel(C.DATASET_MANIFEST)])
    doc = C.read_json(C.ITEMS_FILE)
    d = C.STAGE_ROOT / cfg["sources"]["processbench"]["local_dir"]
    src = {r["id"]: r for s in cfg["dataset"]["domains_used"] for r in pa.load_split(d / f"{s}.json", s)}
    gsm = pa.gsm8k_reference_index(fs.pilot_path(cfg["sources"]["gsm8k_reference"]["pilot_file"], cfg))
    mth = pa.math_reference_index(C.STAGE_ROOT / cfg["sources"]["math_reference"]["rows_dump"])
    train = {pa.ws(json.loads(l)["question"]) for l in
             fs.pilot_path("data/raw/train.jsonl", cfg).read_text(encoding="utf-8").splitlines() if l.strip()}
    problems = set()
    fails = []
    for it in doc["pool"] + doc["reserve"]:
        def bad(msg):
            fails.append(f"{it['item_id']}: {msg}")
        r = src.get(it["processbench_id"])
        if r is None:
            bad("source record missing")
            continue
        if r["problem"] != it["question"] or [s.strip() for s in r["steps"]] != it["steps"]:
            bad("question/steps differ from source")
        if r["label"] + 1 != it["true_error_step"]:
            bad("true_error_step != label + 1")
        if r["final_answer_correct"] is not False:
            bad("final_answer_correct is not False")
        if it["true_error_step"] < 2:
            bad("no valid step before the first error")
        status, ref = pa.lookup_reference(r["problem"], gsm if it["domain"] == "gsm8k" else mth)
        if status != "ok" or pa.normalise_numeric(ref["raw"]) != it["gold_answer"]:
            bad("gold answer does not re-derive")
        orig = pa.normalise_numeric(pa.last_boxed("\n".join(r["steps"])))
        if orig != it["original_answer"] or orig is None or Decimal(orig) == Decimal(it["gold_answer"]):
            bad("original answer missing / differs / equals gold")
        fl = it["false_location"]
        if not fl or fl["false_step"] != it["true_error_step"] - 1 or not (1 <= fl["false_step"] < it["true_error_step"]):
            bad("false step is not the correct step immediately before the first error")
        if any(db.STEP_REF.search(s) for s in it["steps"]):
            bad("step numbering conflict")
        key = pa.ws(it["question"])
        if key in problems:
            bad("duplicate problem")
        problems.add(key)
        if key in train:
            bad("overlaps the pilot / Stage-A GSM8K train problems")
    pool = doc["pool"]
    if doc["reserve"]:
        fails.append("reserve must be empty (selection = all_eligible)")
    if len(pool) != cfg["dataset"]["target_items"]:
        fails.append("pool size != target_items")
    rows, p2, r2 = db.build(cfg)
    if [i["item_id"] for i in p2] != [i["item_id"] for i in pool] or \
            [i["item_id"] for i in r2] != [i["item_id"] for i in doc["reserve"]]:
        fails.append("selection is not deterministic (rebuild differs)")
    out = {"validated": C.now(), "n_pool": len(pool), "n_reserve": len(doc["reserve"]),
           "checks_failed": fails, "passed": not fails}
    C.write_json(C.DATA_MAN / "dataset_validation.json", out)
    print(json.dumps(out, indent=1))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
