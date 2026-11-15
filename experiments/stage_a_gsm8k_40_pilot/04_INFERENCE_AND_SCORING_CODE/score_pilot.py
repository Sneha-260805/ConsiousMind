"""Score a run: per-row outcome flags, per-condition descriptive rates, and paired item tables.

Per row (answer correctness uses the NUMERIC parser; formatting is a separate diagnostic)
  is_gold_correct        numeric_parsed_answer == gold answer          (Repair, the primary metric)
  is_corrupted_answer    numeric_parsed_answer == corrupted answer
  other_wrong_answer     numeric parse ok, but neither gold nor corrupted
  numeric_parse_failure  numeric parser found no unambiguous number (missing / non_numeric / ambiguous)
  strict_format_failure  response did not follow the exact `FINAL_ANSWER: <number>` last-line format
  strict_is_gold_correct gold under the strict parser (secondary diagnostic only)

Per analysis condition: n, gold_correct, repair_rate, corrupted_answer_rate, other_wrong_rate,
numeric_parse_failure_rate, strict_format_failure_rate, strict_repair_rate. Rates use completed
responses as the denominator; requests without a response (transport failure / not yet run) are
counted separately as not_completed and never scored as wrong.

Paired tables
  paired_primary.csv     40 items x {L0, V, L_false_near, L, A}
  paired_far_subset.csv  the 25 far-eligible items x {L0, V, L_false_near, L, A, L_false_far}
The far-subset rates in summary.json are computed on those same 25 items for every condition, so
L_false_far is never compared with a rate over all 40. Descriptive only: no tests, intervals or
conclusions are computed here by design.

Usage: python scripts/score_pilot.py --run-dir outputs/runs/<run_id>
"""
import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pilot_common as pc
from parse_answers import PARSER_VERSION, parse_run


def equal(parsed, target):
    return parsed is not None and Decimal(parsed) == Decimal(str(target))


def score_row(p):
    ok = p["numeric_parse_status"] == "ok"
    gold = ok and equal(p["numeric_parsed_answer"], p["gold_answer"])
    corr = ok and equal(p["numeric_parsed_answer"], p["corrupted_answer"])
    strict_gold = p["strict_parse_status"] == "ok" and equal(p["strict_parsed_answer"], p["gold_answer"])
    return {**p, "is_gold_correct": gold, "is_corrupted_answer": corr,
            "other_wrong_answer": ok and not gold and not corr, "numeric_parse_failure": not ok,
            "strict_format_failure": not p["strict_format_compliant"], "strict_is_gold_correct": strict_gold}


def rates(rows, planned):
    n = len(rows)
    c = Counter()
    for r in rows:
        c["gold"] += r["is_gold_correct"]
        c["corr"] += r["is_corrupted_answer"]
        c["other"] += r["other_wrong_answer"]
        c["nfail"] += r["numeric_parse_failure"]
        c["sfail"] += r["strict_format_failure"]
        c["sgold"] += r["strict_is_gold_correct"]

    def rate(k):
        return round(c[k] / n, 4) if n else None
    return {"n_planned": planned, "n": n, "not_completed": planned - n, "gold_correct": c["gold"],
            "repair_rate": rate("gold"), "corrupted_answer_rate": rate("corr"),
            "other_wrong_rate": rate("other"), "numeric_parse_failure_rate": rate("nfail"),
            "strict_format_failure_rate": rate("sfail"), "strict_repair_rate": rate("sgold"),
            "numeric_parse_status_counts": dict(Counter(r["numeric_parse_status"] for r in rows)),
            "numeric_parse_source_counts": dict(Counter(str(r["numeric_parse_source"]) for r in rows)),
            "strict_parse_status_counts": dict(Counter(r["strict_parse_status"] for r in rows))}


def paired_table(scored, item_ids, conditions):
    cell = {(r["item_id"], r["analysis_condition"]): int(r["is_gold_correct"]) for r in scored}
    table = []
    for iid in item_ids:
        row = {"item_id": iid}
        for c in conditions:
            row[f"{c}_correct"] = cell.get((iid, c), "")   # blank = no completed response
        table.append(row)
    return table


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def score_run(run_dir):
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    parsed, note = parse_run(run_dir)
    scored = [score_row(p) for p in parsed]
    pc.write_jsonl(run_dir / "scored_outputs.jsonl", scored)

    planned = config["selected_requests"]
    planned_by_cond = Counter(r["analysis_condition"] for r in planned)
    conds = [c for c in pc.PRIMARY_CONDITIONS + pc.SENSITIVITY_CONDITIONS if planned_by_cond[c]]
    by_cond = {c: rates([r for r in scored if r["analysis_condition"] == c], planned_by_cond[c]) for c in conds}

    primary_items = sorted({r["item_id"] for r in planned if r["source_file"] == pc.PRIMARY})
    far_items = sorted({r["item_id"] for r in planned if r["analysis_condition"] == "L_false_far"})
    paired_primary = paired_table(scored, primary_items, pc.PRIMARY_CONDITIONS)
    if paired_primary:
        write_csv(run_dir / "paired_primary.csv", paired_primary)
    far_subset = None
    if far_items:
        far_conds = pc.PRIMARY_CONDITIONS + pc.SENSITIVITY_CONDITIONS
        write_csv(run_dir / "paired_far_subset.csv", paired_table(scored, far_items, far_conds))
        far_planned = Counter(r["analysis_condition"] for r in planned if r["item_id"] in far_items)
        far_subset = {"items": far_items, "n_items": len(far_items),
                      "rates_on_same_items": {c: rates([r for r in scored if r["analysis_condition"] == c
                                                         and r["item_id"] in far_items], far_planned[c])
                                              for c in far_conds if far_planned[c]}}

    test_only = bool(config.get("TEST_ONLY"))
    summary = {
        "BANNER": "TEST ONLY - MOCK/DRY-RUN DATA - NOT EXPERIMENTAL RESULTS" if test_only else "experimental run",
        "TEST_ONLY": test_only,
        "run_id": config["run_id"], "mode": config["mode"], "backend": config["model_config"]["backend"],
        "model": config["model_config"].get("model"),
        "n_planned": len(planned), "n_completed": len(scored),
        "raw_file_note": note,
        "by_analysis_condition": by_cond,
        "far_subset": far_subset,
        "paired_tables": {"primary": "paired_primary.csv" if paired_primary else None,
                          "far_subset": "paired_far_subset.csv" if far_items else None},
        "scoring_policy": "repair = numeric_parsed_answer == gold_answer; strict format compliance is a "
                          "separate diagnostic (see parse_answers.py, parser " + PARSER_VERSION + ")",
        "inference": "not computed at this stage (descriptive counts only)",
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary, scored


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    s, _ = score_run(ap.parse_args().run_dir)
    print(json.dumps({k: s[k] for k in ("BANNER", "n_planned", "n_completed", "by_analysis_condition")}, indent=2))
