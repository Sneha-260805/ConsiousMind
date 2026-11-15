"""Stage 6: build the five feedback conditions (L0, V, L, L_false, A) for the 40-item M3 pilot.

No model is called. Reads data/pilot/m3_pilot_40.json (never modified) and writes:
  data/pilot/m3_pilot_40_conditions.jsonl        one row per item x condition (200 rows)
  outputs/m3_pilot_40_conditions_review.txt      human-readable review
  outputs/m3_pilot_40_condition_manifest.json    distributions + audit results

Prompts use only the question and the student-facing corrupted steps. All analysis metadata lives in
separate row fields and is audited to be absent from the prompt text.
"""
import difflib
import hashlib
import itertools
import json
import random
import re
import statistics
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "pilot" / "m3_pilot_40.json"
OUT_JSONL = ROOT / "data" / "pilot" / "m3_pilot_40_conditions.jsonl"
OUT_REVIEW = ROOT / "outputs" / "m3_pilot_40_conditions_review.txt"
OUT_MANIFEST = ROOT / "outputs" / "m3_pilot_40_condition_manifest.json"

SEED = 42
METADATA_VERSION = "m3_pilot40_conditions_v1"
CONDITIONS = ["L0", "V", "L", "L_false", "A"]
LOCATION_CONDITIONS = ("L", "L_false", "L_false_far")   # L_false_far: Stage 7 sensitivity file

# ------------------------------------------------------------------ prompt templates
HEADER = "Here is a problem and a proposed step-by-step solution."
INSTRUCTION = ("Assess the proposed solution carefully and give your best final answer.\n\n"
               "Return the final answer on the last line exactly as:\n\n"
               "FINAL_ANSWER: <answer>")
VERIFY = "The final answer in the proposed solution is wrong."
LOCATE = "The first mistake is in Step {k}."
ORACLE = "The correct final answer is {a}."


def feedback(condition, true_step, false_step, gold_answer):
    if condition == "L0":
        return None
    if condition == "V":
        return VERIFY
    if condition == "L":
        return VERIFY + "\n" + LOCATE.format(k=true_step)
    if condition == "L_false":
        return VERIFY + "\n" + LOCATE.format(k=false_step)
    if condition == "A":
        return ORACLE.format(a=gold_answer)
    raise ValueError(condition)


def build_prompt(question, steps, fb):
    trace = "\n".join(f"Step {i}: {s}" for i, s in enumerate(steps, 1))
    parts = [HEADER, f"Problem:\n{question}", f"Proposed solution:\n{trace}"]
    if fb is not None:          # L0 has no feedback paragraph at all (no empty placeholder line)
        parts.append(fb)
    parts.append(INSTRUCTION)
    return "\n\n".join(parts)


# ------------------------------------------------------------------ false-step assignment
def histogram(values):
    return Counter(values)


def assign_false_steps(items):
    """Choose false_step < true_step for every item so that the aggregate false-step histogram is as
    close as possible to the true-step histogram.

    The assignment only matters through how many items of each true-step group get each false step,
    so all count compositions are enumerated exhaustively. Lexicographic objective:
      1. L1 distance between the two histograms (equivalently total variation x 2*N)
      2. sum of squared histogram differences (penalises piling mass on one step)
      3. smaller mean step distance (keeps the false pointer at a comparable depth in the trace)
    Within a group that gets split, a seeded shuffle (seed 42) of the item ids decides which items
    receive which false step."""
    true_hist = histogram(it["error_step"] for it in items)
    groups = {}
    for it in items:
        groups.setdefault(it["error_step"], []).append(it)
    group_keys = sorted(groups)

    def compositions(n, k):  # all ways to split n items over k bins
        for cuts in itertools.combinations(range(n + k - 1), k - 1):
            prev, out = -1, []
            for c in cuts + (n + k - 1,):
                out.append(c - prev - 1)
                prev = c
            yield tuple(out)

    per_group_options = []
    for t in group_keys:
        per_group_options.append([(t, comp) for comp in compositions(len(groups[t]), t - 1)])

    best, best_key, n_evaluated = None, None, 0
    for combo in itertools.product(*per_group_options):
        n_evaluated += 1
        fh, dist_sum = Counter(), 0
        for t, comp in combo:
            for f, cnt in enumerate(comp, 1):
                fh[f] += cnt
                dist_sum += cnt * (t - f)
        steps = set(fh) | set(true_hist)
        l1 = sum(abs(fh[s] - true_hist[s]) for s in steps)
        l2 = sum((fh[s] - true_hist[s]) ** 2 for s in steps)
        key = (l1, l2, dist_sum)
        if best_key is None or key < best_key:
            best, best_key = combo, key

    rng = random.Random(SEED)
    assignment = {}
    for t, comp in best:
        ids = sorted(it["id"] for it in groups[t])
        rng.shuffle(ids)
        pos = 0
        for f in range(t - 1, 0, -1):          # highest false steps first
            for _ in range(comp[f - 1]):
                assignment[ids[pos]] = f
                pos += 1
    return assignment, {"objective": ["L1 histogram distance", "sum of squared histogram differences",
                                      "total step distance"],
                        "best_objective_value": list(best_key),
                        "assignments_evaluated": n_evaluated,
                        "group_compositions": {str(t): {str(f): c for f, c in enumerate(comp, 1) if c}
                                               for t, comp in best},
                        "l1_lower_bound": lower_bound_l1(true_hist)}


def lower_bound_l1(true_hist):
    """Every item whose true step is 2 can only be pointed at Step 1, and no true error is at Step 1,
    so the false histogram has at least n(true=2) surplus at Step 1 and the same deficit elsewhere."""
    return 2 * true_hist.get(2, 0)


# ------------------------------------------------------------------ validity of the false step
ANN = re.compile(r"<<([^=<>]+)=([^<>]+)>>")


def safe_eval(expr):
    e = expr.replace("x", "*").replace("X", "*").replace("×", "*").replace("÷", "/")
    toks = re.findall(r"\d+\.?\d*|\.\d+|[-+*/()]", e)
    if "".join(toks) != e.replace(" ", ""):
        return None
    return eval("".join(f"Fraction('{t}')" if t[0].isdigit() or t[0] == "." else t for t in toks))


def false_step_checks(it, f):
    t = it["error_step"]
    gi, ci = it["gold_steps_internal"], it["corrupted_steps_internal"]
    prop_steps = {c["step_id"] for c in it["dependency_chain"] if c["calc_id"] in it["propagated_calc_ids"]}
    anns = ANN.findall(ci[f - 1])
    err_calc = next(c for c in it["dependency_chain"] if c["step_id"] == t)
    f_calc_ids = {c["calc_id"] for c in it["dependency_chain"] if c["step_id"] == f}
    checks = {
        "exists_in_student_trace": 1 <= f <= len(it["corrupted_steps_student"]),
        "before_true_error": f < t,
        "not_true_error_step": f != t,
        "identical_to_gold_step": gi[f - 1] == ci[f - 1],
        "not_a_propagated_step": f not in prop_steps,
        "contains_a_calculation": len(anns) >= 1,
        "all_calculations_correct": all(safe_eval(e) == Fraction(r.replace(",", "")) for e, r in anns),
    }
    checks["ALL_PASS"] = all(checks.values())
    # analysis-only: does the false step's result feed the true error step directly?
    checks["feeds_true_error_step_directly"] = bool(f_calc_ids & set(err_calc["dependencies_on_previous_calc_ids"]))
    return checks


# ------------------------------------------------------------------ audits
L0_TERMS = ["wrong", "mistake", "error", "incorrect", "corrupt", "gold", "inject", "true step", "false step"]
ALWAYS_FORBIDDEN = ["inject", "corrupt", "gold", "m3", "calc_", "calculation_slip", "dependency", "propagat",
                    "validation", "manual_review", "approved", "source_index", "train[", "gsm8k", "<<", ">>",
                    "####", "error_step", "false_step", "true_step", "l_false", "placebo", "m3p_"]


def find_terms(text, terms):
    low = text.lower()
    return [t for t in terms if t in low]


def leakage_audit(rows, items_by_id):
    violations, natural = [], []
    for r in rows:
        it = items_by_id[r["item_id"]]
        p = r["prompt"]
        fb = r["_feedback"]
        # 1. the prompt must be exactly template(question, corrupted student steps, feedback)
        expected = build_prompt(it["question"], it["corrupted_steps_student"], fb)
        if p != expected:
            violations.append((r["item_id"], r["condition"], "prompt differs from template rebuild"))
        # 2. everything except the question and the intended feedback must be free of diagnostic terms
        rest = p.replace(it["question"], "", 1)
        if fb is not None:
            rest = rest.replace(fb, "", 1)
        for t in find_terms(rest, L0_TERMS + ALWAYS_FORBIDDEN):
            violations.append((r["item_id"], r["condition"], f"term '{t}' outside question/feedback"))
        for t in find_terms(it["question"], L0_TERMS + ALWAYS_FORBIDDEN):
            natural.append((r["item_id"], r["condition"], f"term '{t}' occurs naturally in the GSM8K question"))
        # 3. feedback content is exactly the condition's intended message
        if r["condition"] == "L0" and fb is not None:
            violations.append((r["item_id"], "L0", "L0 has feedback"))
        if r["condition"] == "V" and fb != VERIFY:
            violations.append((r["item_id"], "V", "V feedback is not the verification sentence"))
        if r["condition"] in LOCATION_CONDITIONS:
            m = re.fullmatch(re.escape(VERIFY) + r"\nThe first mistake is in Step (\d+)\.", fb or "")
            if not m or int(m.group(1)) != r["pointed_step"]:
                violations.append((r["item_id"], r["condition"], "location feedback malformed"))
        if r["condition"] == "A" and fb != ORACLE.format(a=it["gold_answer"]):
            violations.append((r["item_id"], "A", "A feedback is not the oracle sentence"))
        # 4. no gold reasoning, no correct intermediate value, no metadata
        for k, (g, c) in enumerate(zip(it["gold_steps_student"], it["corrupted_steps_student"]), 1):
            if g != c and g in p:
                violations.append((r["item_id"], r["condition"], f"gold step {k} text present"))
        if fb is not None:
            nums = re.findall(r"\d+(?:\.\d+)?", fb)
            allowed = {str(r["pointed_step"])} if r["condition"] in LOCATION_CONDITIONS else set()
            if r["condition"] == "A":
                allowed = {str(it["gold_answer"])}
            for n in nums:
                if n not in allowed:
                    violations.append((r["item_id"], r["condition"], f"unexpected number {n} in feedback"))
            if r["condition"] != "A" and str(it["correct_result"]) in nums:
                violations.append((r["item_id"], r["condition"], "correct intermediate value in feedback"))
        for meta in (it["id"], f"train[{it['source_index']}]", "M3_calculation_slip"):
            if meta.lower() in p.lower():
                violations.append((r["item_id"], r["condition"], f"metadata '{meta}' in prompt"))
    return violations, natural


def matching_audit(rows_by_item, false_condition="L_false", check_v=True):
    lf_checked, lf_mismatch, v_checked, v_mismatch, details = 0, 0, 0, 0, []
    for iid, rows in rows_by_item.items():
        L, LF = rows["L"]["prompt"], rows[false_condition]["prompt"]
        lf_checked += 1
        # (a) replacing only the location step number makes them byte-identical
        pat = r"(The first mistake is in Step )\d+(\.)"
        same_after_mask = (re.sub(pat, r"\1#\2", L) == re.sub(pat, r"\1#\2", LF)
                           and len(re.findall(pat, L)) == 1 == len(re.findall(pat, LF)))
        # (b) the character-level diff touches only digits inside that sentence
        sm = difflib.SequenceMatcher(None, L, LF, autojunk=False)
        loc_start = L.index("The first mistake is in Step ") + len("The first mistake is in Step ")
        diff_ok = True
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if not (L[i1:i2].isdigit() or i1 == i2) or not (LF[j1:j2].isdigit() or j1 == j2) or i1 < loc_start:
                diff_ok = False
        differs = L != LF
        if not (same_after_mask and diff_ok and differs):
            lf_mismatch += 1
            details.append((iid, f"L vs {false_condition} differ beyond the step number (or are identical)"))
        # V == L with only the location sentence (and its newline) removed
        if not check_v:
            continue
        V = rows["V"]["prompt"]
        v_checked += 1
        loc_sentence = "\n" + LOCATE.format(k=rows["L"]["pointed_step"])
        if L.replace(loc_sentence, "", 1) != V or L.count(loc_sentence) != 1:
            v_mismatch += 1
            details.append((iid, "V is not L minus the location sentence"))
    return {"L_vs_L_false_pairs_checked": lf_checked, "L_vs_L_false_differing_beyond_step_number": lf_mismatch,
            "V_vs_L_pairs_checked": v_checked, "V_not_equal_L_minus_location_sentence": v_mismatch,
            "details": details}


# ------------------------------------------------------------------ main
def summary(values):
    return {"mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4),
            "min": round(min(values), 4), "max": round(max(values), 4)}


def main():
    raw_bytes = SRC.read_bytes()
    src_sha = hashlib.sha256(raw_bytes).hexdigest()
    items = json.loads(raw_bytes.decode("utf-8"))["items"]
    items_by_id = {it["id"]: it for it in items}
    assert len(items) == 40 and len(items_by_id) == 40

    assignment, assign_info = assign_false_steps(items)
    step_checks = {}
    for it in items:
        c = false_step_checks(it, assignment[it["id"]])
        if not c["ALL_PASS"]:
            sys.exit(f"invalid false step for {it['id']}: {c}")
        step_checks[it["id"]] = c

    rows = []
    for it in items:
        t, f, n = it["error_step"], assignment[it["id"]], it["n_steps"]
        assert n == len(it["corrupted_steps_student"])
        for cond in CONDITIONS:
            fb = feedback(cond, t, f, it["gold_answer"])
            pointed = t if cond == "L" else f if cond == "L_false" else None
            rows.append({
                "item_id": it["id"],
                "condition": cond,
                "prompt": build_prompt(it["question"], it["corrupted_steps_student"], fb),
                "true_error_step": t,
                "false_step": f,
                "gold_answer": it["gold_answer"],
                "corrupted_answer": it["corrupted_answer"],
                "trace_length": n,
                "pointed_step": pointed,
                "step_distance": t - f,
                "pointed_relative_position": round(pointed / n, 4) if pointed else None,
                "true_relative_position": round(t / n, 4),
                "false_relative_position": round(f / n, 4),
                "false_step_feeds_true_error_step": step_checks[it["id"]]["feeds_true_error_step_directly"],
                "metadata_version": METADATA_VERSION,
                "_feedback": fb,
            })

    violations, natural = leakage_audit(rows, items_by_id)
    rows_by_item = {}
    for r in rows:
        rows_by_item.setdefault(r["item_id"], {})[r["condition"]] = r
    matching = matching_audit(rows_by_item)

    # ------------- write outputs
    with open(OUT_JSONL, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")

    true_steps = [it["error_step"] for it in items]
    false_steps = [assignment[it["id"]] for it in items]
    th, fh_ = Counter(true_steps), Counter(false_steps)
    all_steps = sorted(set(th) | set(fh_))
    manifest = {
        "metadata_version": METADATA_VERSION,
        "source_file": "data/pilot/m3_pilot_40.json",
        "source_sha256": src_sha,
        "seed": SEED,
        "number_of_items": len(items),
        "number_of_prompts": len(rows),
        "condition_counts": dict(Counter(r["condition"] for r in rows)),
        "feedback_templates": {"L0": None, "V": VERIFY, "L": VERIFY + "\\n" + LOCATE,
                               "L_false": VERIFY + "\\n" + LOCATE, "A": ORACLE},
        "true_step_distribution": {str(s): th[s] for s in all_steps},
        "false_step_distribution": {str(s): fh_[s] for s in all_steps},
        "histogram_L1_distance": sum(abs(th[s] - fh_[s]) for s in all_steps),
        "histogram_total_variation": round(sum(abs(th[s] - fh_[s]) for s in all_steps) / (2 * len(items)), 4),
        "false_step_assignment": assign_info,
        "true_x_false_crosstab": {f"{t}->{f}": c for (t, f), c in
                                  sorted(Counter(zip(true_steps, false_steps)).items())},
        "distance_distribution": {str(k): v for k, v in sorted(Counter(t - f for t, f in zip(true_steps, false_steps)).items())},
        "false_step_feeds_true_error_step_directly": sum(c["feeds_true_error_step_directly"] for c in step_checks.values()),
        "trace_length_distribution": {str(k): v for k, v in sorted(Counter(it["n_steps"] for it in items).items())},
        "relative_position_summary": {
            "true": summary([t / it["n_steps"] for t, it in zip(true_steps, items)]),
            "false": summary([f / it["n_steps"] for f, it in zip(false_steps, items)]),
        },
        "false_step_validity": {"items_checked": len(step_checks),
                                "all_pass": all(c["ALL_PASS"] for c in step_checks.values())},
        "prompt_leakage": {"prompts_checked": len(rows), "violations": len(violations),
                           "violation_details": violations,
                           "natural_question_occurrences": natural},
        "prompt_matching": matching,
    }
    OUT_MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with open(OUT_REVIEW, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("M3 PILOT (40 items): FIVE-CONDITION PROMPT REVIEW\n")
        fh.write("Metadata lines (TRUE/FALSE step, answers) are analysis-only and are NOT part of any prompt.\n")
        fh.write("The full prompt is printed once per item (condition L); the other conditions differ only in\n")
        fh.write("the feedback paragraph shown below (L0 has no feedback paragraph).\n\n")
        for it in items:
            r = rows_by_item[it["id"]]
            fh.write("=" * 78 + "\n")
            fh.write(f"ITEM ID: {it['id']}   trace length {it['n_steps']}\n")
            fh.write(f"TRUE ERROR STEP: {it['error_step']}   FALSE STEP: {assignment[it['id']]}   "
                     f"distance {it['error_step'] - assignment[it['id']]}   "
                     f"false step feeds error step directly: {step_checks[it['id']]['feeds_true_error_step_directly']}\n")
            fh.write(f"GOLD ANSWER: {it['gold_answer']}   CORRUPTED ANSWER: {it['corrupted_answer']}\n\n")
            for cond in CONDITIONS:
                fbk = r[cond]["_feedback"]
                fh.write(f"[{cond}] feedback: " + ("<none>" if fbk is None else fbk.replace("\n", " / ")) + "\n")
            fh.write("\n----- full prompt (condition L) -----\n")
            fh.write(r["L"]["prompt"] + "\n")
            fh.write("----- end prompt -----\n\n")

    print(json.dumps({k: manifest[k] for k in ("number_of_items", "number_of_prompts", "condition_counts",
                                               "true_step_distribution", "false_step_distribution",
                                               "histogram_L1_distance", "histogram_total_variation",
                                               "false_step_assignment", "true_x_false_crosstab",
                                               "distance_distribution", "false_step_feeds_true_error_step_directly",
                                               "relative_position_summary", "false_step_validity")}, indent=2))
    print("leakage violations:", len(violations), "| natural question occurrences:", len(natural))
    for v in violations + natural:
        print("  ", v)
    print("matching:", {k: v for k, v in matching.items() if k != "details"}, matching["details"])
    if violations or matching["L_vs_L_false_differing_beyond_step_number"] or matching["V_not_equal_L_minus_location_sentence"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
