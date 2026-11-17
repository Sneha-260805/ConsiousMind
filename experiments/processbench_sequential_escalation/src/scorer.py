"""Scoring contract (fixed before any Stage-B model output exists).

Every completed response gets the Stage-B numeric parse (parser.py, stage_b_numeric_v1; amendment A1) and exactly
ONE outcome:
    repair                    numeric answer == gold                                       (primary)
    original_answer_retention numeric answer == the ORIGINAL trace's (wrong) final answer
    stage1_answer_retention   Stage 2 only: numeric answer == that item's Stage-1 numeric answer (and not the above)
    new_wrong_answer          a parsed number that is none of the above
    parse_failure             no unambiguous numeric answer (missing / non_numeric / ambiguous / incomplete_boxed /
                              truncated_field)
Comparison is numeric (Decimal). Wrong, malformed, refused, empty or truncated outputs are observed outcomes:
scored (repair = False) and never regenerated. Diagnostics: strict_format_compliant, truncated
(finish_reason == "length", i.e. generation hit max_new_tokens), truncated_without_valid_answer, new_tokens.
Stage-1 FAILURE := outcome != "repair" (this is what triggers Stage 2).
"""
from decimal import Decimal

from . import parser

SCORER_VERSION = "stage_b_scorer_v2 (repair = Stage-B numeric parser v1 == gold)"
OUTCOMES_STAGE1 = ["repair", "original_answer_retention", "new_wrong_answer", "parse_failure"]
OUTCOMES_STAGE2 = ["repair", "original_answer_retention", "stage1_answer_retention", "new_wrong_answer",
                   "parse_failure"]


def equal(parsed, target):
    return parsed is not None and target is not None and Decimal(parsed) == Decimal(str(target))


def classify(numeric_status, numeric_answer, gold, original, stage1_answer=None):
    if numeric_status != "ok":
        return "parse_failure"
    if equal(numeric_answer, gold):
        return "repair"
    if equal(numeric_answer, original):
        return "original_answer_retention"
    if equal(numeric_answer, stage1_answer):
        return "stage1_answer_retention"
    return "new_wrong_answer"


def score_raw_row(raw):
    meta = (raw.get("model_metadata") or {}).get("response") or {}
    truncated = meta.get("finish_reason") == "length"
    p = parser.parse(raw["raw_response"], truncated)
    s1 = raw.get("stage1_numeric_answer")
    outcome = classify(p["numeric_parse_status"], p["numeric_parsed_answer"], raw["gold_answer"],
                       raw["original_answer"], s1 if raw["stage"] == 2 else None)
    return {
        "request_key": raw["request_key"], "stage": raw["stage"], "item_id": raw["item_id"],
        "domain": raw["domain"], "condition": raw["condition"], "pointed_step": raw.get("pointed_step"),
        "gold_answer": raw["gold_answer"], "original_answer": raw["original_answer"],
        "stage1_numeric_answer": s1, "test_only": raw.get("test_only", False),
        "outcome": outcome, "repair": outcome == "repair",
        "retains_original_answer": equal(p["numeric_parsed_answer"], raw["original_answer"])
        if p["numeric_parse_status"] == "ok" else False,
        "retains_stage1_answer": (equal(p["numeric_parsed_answer"], s1) if p["numeric_parse_status"] == "ok"
                                  else False) if raw["stage"] == 2 else None,
        "parse_failure": outcome == "parse_failure",
        "strict_format_compliant": p["strict_format_compliant"],
        "truncated": truncated,
        "truncated_without_valid_answer": truncated and outcome == "parse_failure",
        "new_tokens": meta.get("new_tokens"), "prompt_tokens": meta.get("prompt_tokens"),
        "scorer_version": SCORER_VERSION,
        **{k: p[k] for k in ("parser_version", "pilot_strict_parser_version", "numeric_parse_status", "numeric_parsed_answer",
                             "numeric_parse_source", "terminal_region_rule", "strict_parse_status",
                             "strict_parsed_answer")},
    }


def score_rows(raw_rows):
    return [score_raw_row(r) for r in sorted(raw_rows, key=lambda r: r["run_index"])]
