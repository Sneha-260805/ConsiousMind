"""Stage-B candidate pool: eligibility rules, false-step selection, seeded selection. No model outcome is used.

Eligibility (checked in this order; the FIRST failing rule is the recorded exclusion reason):
  E0  domain_not_used              split is olympiadbench / omnimath (answers mostly symbolic; see config note)
  E1  no_error_annotated           label == -1
  E2  final_answer_marked_correct  final_answer_correct == True (process error but correct final answer: the
                                   Stage-1 statement "the final answer is wrong" would be false)
  E3  error_at_step_1              label == 0: no valid step before the first error
  E4  reference_not_found / reference_ambiguous     no unique exact source problem
  E5  reference_not_numeric        gold not machine-checkable by the frozen numeric parser
  E6  original_answer_unextractable  no \\boxed{} final answer in the trace, or not numeric
  E7  annotation_inconsistent      trace's final answer == gold although final_answer_correct is False
  E8  step_numbering_conflict      a step's own text contains 'Step <n>' / 'step <n>' (would make "Step k" ambiguous)
  E9  empty_step                   an empty / whitespace-only step
  E10 diagram_code                 problem contains [asy] diagram code
  E11 trace_too_long               rendered trace > dataset.max_trace_chars
  E12 overlaps_prior_experiment    problem text appears in the GSM8K TRAIN file used by the pilot / Stage A
  E13 duplicate_problem            another eligible ProcessBench record has the same problem (one item per problem:
                                   the record with the smallest sha256(seed:id) is kept)
False step (L_false): Step k-1, the step immediately before the annotated first error k (1-indexed). ProcessBench
annotators judged every step before k correct, so it is a genuinely correct, pre-error step.
Selection (researcher decision 2026-10-12): ALL eligible items form the Stage-1 pool (no sampling, no reserve), listed
by domain then sha256(selection_seed:id). Domain, MATH level and subject are kept for stratified secondary analysis.
"""
import collections
import json
import re
from decimal import Decimal

from . import common as C
from . import frozen_sources as fs
from . import processbench_adapter as pa

STEP_REF = re.compile(r"\bstep\s*\(?\s*\d", re.IGNORECASE)
NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
EXCLUSION_ORDER = ["domain_not_used", "no_error_annotated", "final_answer_marked_correct", "error_at_step_1",
                   "reference_not_found", "reference_ambiguous", "reference_not_numeric",
                   "original_answer_unextractable", "annotation_inconsistent", "step_numbering_conflict",
                   "empty_step", "diagram_code", "trace_too_long", "overlaps_prior_experiment",
                   "duplicate_problem"]


def render_trace(steps):
    return "\n".join(f"Step {i}: {s.strip()}" for i, s in enumerate(steps, 1))


def order_key(seed, item_id):
    return C.sha256_text(f"{seed}:{item_id}")


def false_step_record(steps, k):
    """Nearest correct pre-error step + descriptive relation to the true error step."""
    f = k - 1
    if f < 1:
        return None
    nums_f = set(NUM.findall(steps[f - 1]))
    nums_k = set(NUM.findall(steps[k - 1]))
    return {"false_step": f, "true_step": k, "distance": k - f, "n_steps": len(steps),
            "false_relative_position": round(f / len(steps), 4), "true_relative_position": round(k / len(steps), 4),
            "dependency_relation": "immediately_preceding_step",
            "numeric_overlap_with_error_step": sorted(nums_f & nums_k, key=lambda x: (len(x), x)),
            "numeric_overlap_note": "heuristic only: numbers that occur in both the false step and the true error "
                                    "step (a possible data dependency); not an annotation"}


def evaluate(rec, split, refs, train_problems, cfg):
    """-> (exclusion_reason or None, details dict)"""
    det = {"processbench_id": rec["id"], "domain": split, "generator": rec["generator"],
           "label_0indexed": rec["label"], "n_steps": len(rec["steps"]),
           "final_answer_correct": rec["final_answer_correct"]}
    if split not in cfg["dataset"]["domains_used"]:
        return "domain_not_used", det
    if rec["label"] < 0:
        return "no_error_annotated", det
    if rec["final_answer_correct"]:
        return "final_answer_marked_correct", det
    k = pa.first_error_step(rec)
    det["true_error_step"] = k
    if k == 1:
        return "error_at_step_1", det
    status, ref = pa.lookup_reference(rec["problem"], refs[split])
    if status == "no_match":
        return "reference_not_found", det
    if status != "ok":
        return "reference_ambiguous", det
    det.update(reference_source=ref["source"], reference_source_index=ref["source_index"],
               reference_raw=ref["raw"], math_level=ref.get("level"), math_subject=ref.get("subject"))
    gold = pa.normalise_numeric(ref["raw"])
    if gold is None:
        return "reference_not_numeric", det
    det["gold_answer"] = gold
    trace = "\n".join(rec["steps"])
    orig_raw = pa.last_boxed(trace)
    orig = pa.normalise_numeric(orig_raw)
    det.update(original_answer_raw=orig_raw, original_answer=orig)
    if orig is None:
        return "original_answer_unextractable", det
    if Decimal(orig) == Decimal(gold):
        return "annotation_inconsistent", det
    if any(STEP_REF.search(s) for s in rec["steps"]):
        return "step_numbering_conflict", det
    if any(not s.strip() for s in rec["steps"]):
        return "empty_step", det
    if "[asy]" in rec["problem"]:
        return "diagram_code", det
    rendered = render_trace(rec["steps"])
    det["trace_chars"] = len(rendered)
    if len(rendered) > cfg["dataset"]["max_trace_chars"]:
        return "trace_too_long", det
    if pa.ws(rec["problem"]) in train_problems:
        return "overlaps_prior_experiment", det
    return None, det


def build(cfg=None):
    cfg = cfg or C.load_config()
    src = cfg["sources"]["processbench"]
    d = C.STAGE_ROOT / src["local_dir"]
    for name, sha in src["files_sha256"].items():
        if C.sha256_file(d / name) != sha:
            raise SystemExit(f"STOP: ProcessBench file {name} changed")
    mr = cfg["sources"]["math_reference"]
    if C.sha256_file(C.STAGE_ROOT / mr["rows_dump"]) != mr["rows_dump_sha256"]:
        raise SystemExit("STOP: MATH rows dump changed")
    refs = {"gsm8k": pa.gsm8k_reference_index(fs.pilot_path(cfg["sources"]["gsm8k_reference"]["pilot_file"], cfg)),
            "math": pa.math_reference_index(C.STAGE_ROOT / mr["rows_dump"])}
    train_problems = {pa.ws(json.loads(l)["question"]) for l in
                      fs.pilot_path("data/raw/train.jsonl", cfg).read_text(encoding="utf-8").splitlines() if l.strip()}
    seed = cfg["dataset"]["selection_seed"]
    rows, eligible = [], []
    recs_by_id = {}
    for split in pa.SPLITS:
        for rec in pa.load_split(d / f"{split}.json", split):
            reason, det = evaluate(rec, split, refs, train_problems, cfg)
            rows.append({**det, "exclusion_reason": reason})
            recs_by_id[rec["id"]] = rec
            if reason is None:
                eligible.append(rows[-1])
    # E13: one item per problem
    by_problem = collections.defaultdict(list)
    for r in eligible:
        by_problem[pa.ws(recs_by_id[r["processbench_id"]]["problem"])].append(r)
    for group in by_problem.values():
        if len(group) > 1:
            keep = min(group, key=lambda r: order_key(seed, r["processbench_id"]))
            for r in group:
                if r is not keep:
                    r["exclusion_reason"] = "duplicate_problem"
                    r["duplicate_of"] = keep["processbench_id"]
    eligible = [r for r in eligible if r["exclusion_reason"] is None]

    items = []
    for r in eligible:
        rec = recs_by_id[r["processbench_id"]]
        steps = [s.strip() for s in rec["steps"]]
        k = r["true_error_step"]
        items.append({
            "item_id": f"sb_{rec['id']}", "processbench_id": rec["id"], "domain": r["domain"],
            "generator": rec["generator"], "question": rec["problem"], "steps": steps, "n_steps": len(steps),
            "true_error_step": k, "label_0indexed": rec["label"],
            "true_error_relative_position": round(k / len(steps), 4),
            "gold_answer": r["gold_answer"], "gold_answer_type": pa.answer_type(r["gold_answer"]),
            "reference_source": r["reference_source"], "reference_source_index": r["reference_source_index"],
            "reference_raw": r["reference_raw"], "original_answer": r["original_answer"],
            "original_answer_raw": r["original_answer_raw"], "math_level": r.get("math_level"),
            "math_subject": r.get("math_subject"), "trace_chars": r["trace_chars"],
            "false_location": false_step_record(steps, k),
            "selection_key": order_key(seed, rec["id"]),
        })
    if cfg["dataset"].get("selection") != "all_eligible":
        raise SystemExit("STOP: Stage B uses selection = all_eligible (researcher decision 2026-10-12)")
    pool, reserve = list(items), []          # every eligible item; no sampling, no reserve
    pool.sort(key=lambda i: (cfg["dataset"]["domains_used"].index(i["domain"]), i["selection_key"]))
    for n, it in enumerate(pool):
        it["pool_index"] = n
    for n, it in enumerate(reserve):
        it["reserve_rank"] = n + 1
    return rows, pool, reserve


def summarise(rows, pool, reserve, cfg):
    ex = collections.Counter(r["exclusion_reason"] or "ELIGIBLE" for r in rows)
    by_dom = collections.defaultdict(collections.Counter)
    for r in rows:
        by_dom[r["domain"]][r["exclusion_reason"] or "ELIGIBLE"] += 1

    def dist(key, items):
        return dict(sorted(collections.Counter(str(i[key]) for i in items).items(), key=lambda kv: (len(kv[0]), kv[0])))
    return {
        "source_records_inspected": len(rows),
        "eligible_after_dedup": ex["ELIGIBLE"],
        "exclusion_reasons_first_failing_rule": {k: ex[k] for k in EXCLUSION_ORDER if ex[k]},
        "by_domain": {d: {k: c[k] for k in ["ELIGIBLE", *EXCLUSION_ORDER] if c[k]} for d, c in by_dom.items()},
        "pool_size": len(pool), "reserve_size": len(reserve),
        "pool_by_domain": dist("domain", pool),
        "pool_true_error_step": dist("true_error_step", pool),
        "pool_true_error_step_by_domain": {d: dist("true_error_step", [i for i in pool if i["domain"] == d])
                                           for d in cfg["dataset"]["domains_used"]},
        "pool_n_steps": dist("n_steps", pool),
        "pool_gold_answer_type": dist("gold_answer_type", pool),
        "pool_generators": dist("generator", pool),
        "pool_math_level": dist("math_level", [i for i in pool if i["domain"] == "math"]),
        "pool_math_subject": dist("math_subject", [i for i in pool if i["domain"] == "math"]),
        "pool_trace_chars": {"min": min(i["trace_chars"] for i in pool), "max": max(i["trace_chars"] for i in pool),
                             "mean": round(sum(i["trace_chars"] for i in pool) / len(pool), 1)},
        "pool_false_step_available": sum(i["false_location"] is not None for i in pool),
        "pool_false_step_numeric_overlap": sum(bool(i["false_location"]["numeric_overlap_with_error_step"])
                                               for i in pool if i["false_location"]),
        "reserve_by_domain": dist("domain", reserve),
    }
