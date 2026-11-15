"""Stage 4: build exactly 10 M3 (arithmetic calculation slip) candidates for MANUAL REVIEW.

M3 keeps the operands, the operator, the semantic interpretation and the units of one calculation,
and replaces only its RESULT with a plausible wrong value (e.g. 33/3 = 12). The wrong value is shown
in both the visible text and the <<expr=result>> annotation, and is propagated through every later
calculation that depends on it (recomputed correctly from the wrong value).

Reused unchanged from build_m1_candidates.py: parsing, strict eligibility filter, explicit dependency
graph with ambiguity rejection, exact Fraction arithmetic, number formatting, span rewriting.
New here: the M3 corruption, an M3-aware validator, the wording check, and the reviewer semantic gate.

No language model is called. No M1 files are touched.

Usage (from the project root):
    PYTHONIOENCODING=utf-8 python scripts/build_m3_candidates.py
"""
import json
import random
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_m1_candidates as s2  # noqa: E402  (structural pipeline, unchanged)

ROOT = s2.ROOT
OUT_JSON = ROOT / "data" / "pilot" / "m3_candidates_10.json"
OUT_REVIEW = ROOT / "outputs" / "m3_candidates_10_review.txt"
OUT_STATS = ROOT / "outputs" / "m3_candidates_10_stats.json"
OUT_SAMPLE_AUDIT = ROOT / "outputs" / "m3_reviewed_sample_audit.txt"

SEED = 42
N_CANDIDATES = 10
MIN_ERROR_STEP = 2
DELTAS = [1, -1, 2, -2, 10, -10]          # tried in this order; +-10 models a tens-digit slip
MAX_RELATIVE_DIFFERENCE = Fraction(1, 4)  # |wrong - correct| / correct
FINAL_RATIO_BAND = (Fraction(1, 2), Fraction(2))
DIVERSITY_CAPS = {"error_step": 3, "n_steps": 4, "operator": 4, "delta": 4}
N_PENDING_TO_LIST = 40

# ------------------------------------------------------------------ reviewer semantic gate
# Sentence-level judgement: does the wrong value make the STORY impossible (101 correct answers out of
# 100, 9 days in a week, more money spent than exists)? Merely being arithmetically checkable is NOT a
# failure; that is the intended error. Keyed by (source_index, calc_id, corrupted_result).
# Recorded by Claude (coding assistant) during dataset construction; human review is still pending.
def _ok(reason):
    return {"pass": True, "reason": reason}


def _no(reason):
    return {"pass": False, "reason": reason}


REVIEWER_SEMANTIC_CHECK = {
    (6867, 1, 112): _ok("a $112 total for socks and shoes is an ordinary addition slip; the story stays coherent"),
    (6553, 1, 10): _ok("10 loaves instead of 9 is a plausible addition slip; no story constraint is violated"),
    (5356, 1, 5): _ok("5 yards instead of 6 is a plausible division slip; the total of 15 yards is unremarkable"),
    (1564, 2, 248): _ok("248 minutes instead of 247 is a plausible three-term addition slip; weekend minutes stay positive"),
    (6243, 1, 57): _ok("$57 instead of $56 is a plausible addition slip; nothing in the story bounds it"),
    (5640, 3, 94): _ok("$94 instead of $93 is a plausible three-term addition slip; still more than the $50 he has"),
    (1587, 1, 85): _ok("85 instead of 84 hospitalized (70% of 120) is plausible; still fewer than the 120 smokers"),
    (7393, 1, 5): _ok("5 cookies a day instead of 6 is a plausible division slip; 20 over four days is still feasible"),
    (3903, 1, 11): _ok("11 masks an hour instead of 10 is a plausible division slip; no constraint is violated"),
    (5798, 1, 27): _ok("27 bruised apples instead of 26 is a plausible addition slip; 41 left to eat raw is still feasible"),
    (6649, 1, 2676): _ok("2676 instead of 2675 steps for half the field is a plausible halving slip; the scale is unchanged"),
    (4104, 2, 90): _ok("90 candies instead of 80 is a carry slip in 68 + 12; the story stays coherent"),
    (251, 2, 5): _ok("a $5 cost instead of $6 for 12 pies at $0.50 is a plausible slip; profit stays positive"),
    (2245, 1, 50): _ok("50 instead of 48 extra lions (4 per month for 12 months) is plausible; 150 lions is unremarkable"),
    (6303, 1, 43): _ok("43 instead of 42 red-and-green marbles is a plausible subtraction slip; 39 red stays feasible"),
    (3040, 1, 18): _ok("Jenny at 18 instead of 17 is a plausible addition slip; the age gap stays sensible"),
    (3141, 1, 626): _ok("626 instead of 625 lunch calories is a plausible addition slip"),
    (2012, 1, 120): _ok("120 instead of 110 stickers is a carry-type slip; the story stays coherent"),
    (3139, 1, 18): _ok("18 instead of 17 white beads (a third of 51) is plausible; still fewer than 51"),
    (5151, 1, 19): _ok("19 instead of 18 cars arriving is a plausible addition slip; 86 cars is feasible"),
    (3327, 1, 5010): _no("20% of 25,000 giving 5,010 is not a realistic slip: a round percentage of a round number "
                         "is computed mentally, and the irregular tens digit stands out without any arithmetic"),
    (2212, 4, 24210): _ok("24,210 instead of 24,200 is a tens-digit slip in a multi-digit product; the scale is unchanged"),
    (2324, 1, 152): _ok("$152 instead of $150 a week is a plausible multiplication slip"),
    (2647, 1, 150): _ok("150 instead of 160 pythons and boas is a tens slip; 50 rattlesnakes out of 200 is still feasible"),
    (3303, 3, 50): _ok("50 instead of 49 damaged pages (20% of 245) is a plausible rounding-type slip; 195 pages stays feasible"),
    (5501, 1, 373): _ok("373 instead of 372 green apples is a plausible addition slip"),
    (3228, 1, 11): _ok("$11 instead of $9 for a three-item lunch with cents is a plausible addition slip"),
    (4412, 3, 151): _no("25 + 50 + 75 are all multiples of 25; a total of 151 stands out without doing the sum"),
    (856, 1, 98): _no("8 x 12 = 96 is a recalled times-table fact (12s), so it is not a realistic slip site"),
    (2463, 3, 294): _ok("294 instead of 293 athletes is a plausible addition slip; still fewer than 300"),
    (3824, 1, 451): _no("half of 900 is a recalled round-number fact; an odd 451 is visibly impossible"),
    (6118, 1, 734): _ok("734 instead of 732 passengers (2 x 366) is a plausible doubling slip and keeps parity"),
    (3127, 1, 45): _no("'in 2 years, 44 + 2 = 45' is counting forward, not a calculation; the slip is trivially exposed"),
    (671, 1, 145): _ok("145 instead of 144 mangoes eaten (60% of 240) is plausible; 95 remain"),
    (6085, 0, 43): _ok("43 instead of 42 gallons from a four-term sum is a plausible addition slip"),
    (6474, 1, 1226): _ok("$1226 instead of $1225 (350 x 3.5) is a plausible multiplication slip"),
    (3650, 2, 730): _ok("$730 instead of $720 is a tens slip in 900 - 180; still under the $1000 budget"),
    (4526, 2, 150): _ok("$150 instead of $140 out of pocket is a tens slip in 700 - 560; still less than the $700 bill"),
    (5744, 1, 170): _ok("$170 instead of $168 for six $28 passes is a plausible multiplication slip; $81 left is feasible"),
    (5630, 1, 922): _no("11.50 x 80 must end in 0 (a factor of 80); $922 stands out without computing"),
    (6008, 1, 490): _ok("$490 instead of $480 a day is a tens slip in 96 x 5 and keeps the visible multiple of 10"),
    (3861, 1, 690): _no("doubling the round number 340 is mental arithmetic; 690 is not a realistic slip"),
    (2595, 1, 9): _ok("9 instead of 8 fifteen-minute blocks in 120 minutes is a plausible division slip"),
    (4197, 1, 674): _ok("$674 instead of $672 (112 x 6) is a plausible multiplication slip; the gain stays positive"),
    (1196, 1, 150): _ok("150 instead of 160 years is a tens slip in 120 + 40; ages stay plausible"),
    (1546, 1, 66): _ok("66 instead of 65 minutes (a third of 195) is a plausible division slip"),
    (2120, 4, 253): _ok("253 instead of 252 pins is a plausible addition slip"),
    (3465, 2, 34): _ok("$34 instead of $33 (25 + 8, with a carry) is a plausible addition slip; $40 left is feasible"),
    (3680, 2, 19): _ok("19 instead of 18 genuine handbags (24 - 6, with a borrow) is plausible; still fewer than 24"),
    (3257, 1, 43): _ok("43 instead of 44 passengers (66 - 22) is a plausible subtraction slip"),
    (4658, 1, 96): _ok("$96 instead of $98 (2 x 49) is a plausible doubling slip and keeps parity"),
    (3996, 1, 49): _ok("49 instead of 48 slices (8/13 of 78) is a plausible fraction slip; still fewer than 78"),
    (1267, 3, 1634): _ok("$1634 instead of $1633 (1850 - 217, with a borrow) is a plausible subtraction slip"),
    (4234, 1, 16): _ok("16 instead of 15 cats (60% of 25) is a plausible percentage slip; still fewer than 25"),
    (7191, 1, 55): _no("30 + 15 is mental arithmetic; 55 is not a realistic slip"),
    (7410, 1, 19): _ok("19 instead of 18 books (24 - 6, with a borrow) is plausible; still fewer than 24"),
    (5100, 1, 53): _ok("53 instead of 54 lights from a three-term sum is a plausible addition slip"),
    (5738, 2, 34): _ok("34 instead of 33 players (20 + 13) is a plausible addition slip; 6 strikers out of 40 is feasible"),
    (2454, 2, 45): _ok("45 instead of 46 students in shorts (27 + 19, with a carry) is plausible; still fewer than 81"),
    (5284, 3, 57): _ok("57 instead of 56 dozen (672 / 12) is a plausible long-division slip"),
    (1644, 2, 220): _ok("220 instead of 219 memory cards (10950 / 50) is a plausible division slip; the scale is unchanged"),
    (3841, 2, 241): _ok("$241 instead of $240 (108 + 132, with carries) is plausible; still under the $250 paid"),
    (189, 1, 17): _ok("17 instead of 16 (112 / 7) is a plausible division slip; ages stay sensible"),
    (7268, 2, 90): _no("35 + 45 is mental arithmetic; 90 is not a realistic slip"),
    (1836, 4, 27): _ok("27 instead of 26 candies (9 + 17, with a carry) is a plausible addition slip"),
    (3380, 3, 457): _ok("$457 instead of $456 (475 - 19, with a borrow) is a plausible subtraction slip"),
    (6858, 3, 1123): _ok("$1123 instead of $1122 (1320 - 198) is a plausible subtraction slip"),
    (4244, 1, 23): _ok("23 instead of 21 chickens (16 + 5, with a carry) is a plausible addition slip"),
    (2999, 2, 42): _ok("42 instead of 43 marbles (25 + 12 + 6) is plausible; 8 red out of 50 is feasible"),
    (2764, 2, 108): _ok("108 instead of 106 animals (69 + 37, with carries) is a plausible addition slip; still even"),
    (3100, 2, 145): _ok("145 instead of 135 apples (3 x 45, with a carry) is a tens slip and keeps the visible multiple of 5"),
    (3252, 2, 28): _ok("28 instead of 29 choir members (12 + 17) is a plausible addition slip"),
    (2080, 3, 58): _no("the GOLD trace already mislabels '$59 x 7 = $413 per month' (it is per week), a pre-existing "
                       "wording error after the injection point, so the trace would not contain exactly one error"),
    (6725, 1, 55): _no("15 x 3 is a recalled multiple of 15; 55 is not a realistic slip"),
    (5190, 2, 73): _ok("73 instead of 72 homes (60% of 120) is plausible; still fewer than the 120 remaining"),
    (6166, 1, 602): _no("2/9 of 2700 is exactly 600 (2700 / 9 = 300); 602 stands out against round operands"),
    (1769, 2, 22): _ok("22 instead of 21 students (15 + 6, with a carry) is plausible; 8 for yellow out of 30 is feasible"),
    (2016, 3, 111): _ok("$111 instead of $110 in deductions (84 + 21 + 5) is a plausible addition slip"),
    (6141, 1, 190): _no("45 x 4 is a recalled multiple (like 15 x 3); 190 is not a realistic slip"),
    (1780, 3, 145): _ok("$145 instead of $135 (27 x 5, with a carry) is a tens slip and keeps the visible multiple of 5"),
    (2976, 1, 17): _ok("17 instead of 16 fences (480 / 30) is a plausible division slip"),
    (1112, 2, 36): _ok("36 instead of 35 berries (21 + 14) is plausible; 6 blueberries out of 42 is feasible"),
    (2483, 1, 44): _ok("$44 instead of $43 (18 + 25, with a carry) is plausible; still under the $50 gift"),
    (3143, 2, 260): _ok("260 instead of 250 candies (5 + 35 + 210) is a tens slip in a three-term sum"),
    (3448, 2, 8): _no("28 - 18 has matching units digits, so the result is visibly a multiple of 10; 8 stands out"),
    (7268, 1, 55): _no("3 x 15 is a recalled multiple of 15; 55 is not a realistic slip"),
    (436, 3, 19): _ok("19 instead of 18 pencils (60 - 42, with a borrow) is a plausible subtraction slip"),
    (4211, 1, 80): _no("15 + 30 + 20 + 25 are small multiples of 5 summed mentally; 80 is not a realistic slip"),
    (5705, 1, 38): _ok("$38 instead of $36 (90 - 54, with a borrow) is plausible; 19 scarves at $2 is feasible"),
    (3155, 4, 35): _no("50 - 5 is mental arithmetic; 35 is not a realistic slip"),
    (7145, 1, 4106): _ok("4106 instead of 4104 grams (216 x 19) is a plausible multi-digit multiplication slip"),
    (2119, 2, 5): _no("72 / 12 is a recalled 12-times-table fact; 5 is not a realistic slip"),
    (7145, 2, 246250): _ok("$246,250 instead of $246,240 (60 x 4104) is a tens slip in a long product and keeps the trailing 0"),
    (4571, 2, 80): _no("18 x 5 is mental arithmetic (half of 18 x 10); 80 is not a realistic slip"),
    (3291, 2, 80): _no("115 - 30 - 15 is mental arithmetic on round-ish values; 80 is not a realistic slip"),
    (2280, 2, 9): _no("two-thirds of 15 is mental arithmetic; 9 is not a realistic slip"),
    (6955, 3, 230): _no("90 + 45 + 55 + 50 pairs into round numbers mentally; 230 is not a realistic slip"),
    (1836, 3, 18): _ok("18 instead of 17 candies for Andy (36 - 19, with a borrow) is plausible; still fewer than 36"),
    (2660, 4, 37): _ok("37 instead of 36 oranges (63 - 27, with a borrow) is a plausible subtraction slip"),
    (3900, 1, 106): _ok("$106 instead of $105 a day (9450 / 90) is a plausible long-division slip"),
    (3876, 2, 835): _no("210 + 420 + 130 + 65 is mostly round addends summed mentally; 835 is not a realistic slip"),
    (5763, 2, 226): _ok("$226 instead of $225 (147 + 78, with carries) is a plausible addition slip"),
    (376, 1, 28): _no("12 + 15 has no carry and small values; 28 is not a realistic slip"),
    (2789, 1, 230): _no("round-number sum, and it makes the required fourth score 110, impossible on a 100-point test"),
    (6479, 1, 49): _ok("49 instead of 48 sellable pots (80 - 32, with a borrow) is plausible; still fewer than 80"),
    (5575, 2, 42): _ok("$42 instead of $40 from the aunt (97 - 57, with a borrow) is plausible; $21 each is feasible"),
    (6939, 2, 226): _ok("226 instead of 224 episodes (198 + 26, with carries) is a plausible addition slip"),
    (1049, 0, 22): _ok("a least score of 22 instead of 23 (98 - 75, with a borrow) is a plausible subtraction slip"),
    (63, 1, 10): _ok("10 instead of 11 situps short (30 - 19, with a borrow) is a plausible subtraction slip"),
    (5967, 1, 26): _no("12 + 16 has no carry and small values; 26 is not a realistic slip"),
    (5519, 3, 1030): _no("360 + 480 + 180 are round addends summed mentally; 1030 is not a realistic slip"),
    (6020, 3, 190): _ok("$190 instead of $188 (32 + 36 + 120) is a plausible addition slip; Carrie's $95 half is feasible"),
    (6296, 3, 130): _no("15 x 8 is a recalled multiple; 130 is not a realistic slip"),
    (4932, 3, 132): _ok("132 instead of 130 customers (32 + 40 + 58, with a carry) is plausible and stays even for halving"),
    (4127, 3, 240): _no("45 + 65 + 120 pairs into round numbers mentally; 240 is not a realistic slip"),
    (2048, 3, 218): _no("the gold solution breaks a sentence across two lines ('per' / 'hour.'), so the numbered steps "
                        "are malformed; data quirk recorded, not fixed"),
}


# ------------------------------------------------------------------ corruption
def make_m3(g, k, delta):
    """Return (record, None) or (None, rejection_reason)."""
    calcs = g["calcs"]
    c = calcs[k]
    if c["step"] < MIN_ERROR_STEP:
        return None, "error_step_is_step_1"
    r = c["value"]
    if r.denominator != 1:
        return None, "non_integer_correct_result"
    wrong = r + delta
    if wrong == r:
        return None, "unchanged_result"
    if wrong < 0:
        return None, "negative_value"
    if wrong == 0:
        return None, "zero_value"
    if r == 0 or abs(Fraction(delta)) / abs(r) > MAX_RELATIVE_DIFFERENCE:
        return None, "relative_difference_too_large"
    affected = s2.descendants(calcs, k)
    if len(affected) < 2:
        return None, "no_dependent_calculation"

    new_vals = {k: wrong}
    for d in calcs[k + 1:]:
        if d["calc_id"] not in affected:
            continue
        leaves = [new_vals[s] if isinstance(s, int) and s in new_vals else v for v, s in zip(d["leaves"], d["operand_sources"])]
        try:
            v = s2.eval_expr(s2._expr_with_leaves(d, leaves, use_visible=False))["value"]
        except ZeroDivisionError:
            return None, "downstream_division_by_zero"
        old = d["value"]
        if v < 0:
            return None, "negative_value"
        if v == 0:
            return None, "zero_value"
        if old.denominator == 1 and v.denominator != 1:
            return None, "non_integer_value_where_integer_expected"
        if s2.decimals_needed(v) is None or s2.decimals_needed(v) > s2.MAX_DECIMALS_NONINT:
            return None, "non_terminating_or_too_many_decimals"
        new_vals[d["calc_id"]] = v

    last = calcs[-1]["calc_id"]
    new_final = new_vals.get(last, g["final"])
    if new_final == g["final"]:
        return None, "final_equals_gold"
    if new_final.denominator != 1:
        return None, "non_integer_final_answer"
    lo, hi = FINAL_RATIO_BAND
    if not (lo <= new_final / g["final"] <= hi):
        return None, "final_scale_change_too_large"
    # no corrupted value (wrong result or any propagated value) may coincide with the gold final answer
    if any(v == g["final"] for v in new_vals.values()):
        return None, "corrupted_value_equals_gold_final"

    # same ambiguity guards as Stage 2: changed values must not be restated in prose, and new values
    # must not collide with any other number in the trace or question
    prose_nums, other_nums = [], set(g["q_nums"]) | s2.IMPLICIT_CONSTANTS
    for li, line in enumerate(g["lines"]):
        prose_nums += s2.prose_numbers(s2.free_prose(line, s2.calc_on_line(g, li)))
    if any(calcs[cid]["value"] in prose_nums for cid in affected):
        return None, "affected_value_restated_in_prose"
    for d in calcs:
        if d["calc_id"] not in affected:
            other_nums.add(d["value"])
        other_nums.update(lv for lv, s in zip(d["leaves"], d["operand_sources"]) if not isinstance(s, int))
    other_nums.update(prose_nums)
    nv = list(new_vals.values())
    if any(v in other_nums for v in nv) or len(set(nv)) != len(nv):
        return None, "corrupted_value_collides_with_existing_number"

    # rewrite only calculation spans: error step -> result only; dependents -> operand(s) + result
    new_lines = list(g["lines"])
    for cid in sorted(affected):
        d = calcs[cid]
        repl = []
        if cid != k:
            off = d["ann_expr_offset"]
            ann_nums = [t for t in d["ann_tokens"] if t[0] == "num"]
            vis_nums = [t for t in d["vis_tokens"] if t[0] == "num"]
            for i, s in enumerate(d["operand_sources"]):
                if isinstance(s, int) and s in new_vals:
                    repl.append(((off + ann_nums[i][2], off + ann_nums[i][3]), s2.fmt(new_vals[s], ann_nums[i][1])))
                    repl.append(((vis_nums[i][2], vis_nums[i][3]), s2.fmt(new_vals[s], vis_nums[i][1])))
        rs = d["ann_result_offset"]
        repl.append(((rs, rs + len(d["ann_result_text"])),
                     s2._keep_ws(d["ann_result_text"], s2.fmt(new_vals[cid], d["ann_result_text"].strip()))))
        repl.append((d["vis_result_span"], s2.fmt(new_vals[cid], d["vis_result_text"])))
        new_lines[d["line_index"]] = s2.replace_spans(new_lines[d["line_index"]], repl)
    final_tmpl = g["answer_text"].rsplit(s2.FINAL_SEP, 1)[1].strip()
    text = "\n".join(new_lines) + s2.FINAL_SEP + s2.fmt(new_final, final_tmpl)
    return {"calc_id": k, "error_step": c["step"], "correct_result": r, "corrupted_result": wrong, "delta": delta,
            "affected": sorted(affected), "new_values": new_vals, "corrupted_final": new_final,
            "corrupted_answer_text": text, "corrupted_lines": new_lines}, None


# ------------------------------------------------------------------ automatic semantic check
def digit_pattern_problem(c, wrong):
    """Reject slips that break a property visible WITHOUT doing the arithmetic (last-digit / parity checks).
    Found on manual review: 3*600=1801, 50*9=451, 20*2=41 are not realistic slips; a glance exposes them."""
    r = c["value"]
    ints = [v for v in c["leaves"] if v.denominator == 1]
    all_int = len(ints) == len(c["leaves"])
    # visible divisibility: 25+50+75 must be a multiple of 25; 500+125 must end in 25/50/75/00; 1200*11 must end in 00
    def visible_d(v):
        return next((d for d in (100, 50, 25, 10, 5) if v % d == 0), 1)
    if all_int and set(c["ops"]) <= {"+", "-"}:
        d = min(visible_d(v) for v in ints)
        while d > 1 and any(v % d for v in ints):   # common visible divisor of all addends
            d = {100: 50, 50: 25, 25: 5, 10: 5, 5: 1}[d]
    elif all_int and set(c["ops"]) == {"*"}:
        d = 1
        for v in ints:                               # 320*60 -> 10*10 = 100
            d *= visible_d(v)
    else:
        d = 1
    if d > 1 and r % d == 0 and wrong % d != 0:
        return f"operands make the result visibly a multiple of {d}"
    if "*" in c["ops"] and "/" not in c["ops"] and any(v % 2 == 0 for v in ints) and r % 2 == 0 and wrong % 2 != 0:
        return "a product with an even factor must be even"
    return None


def elementary_fact(c):
    """Recalled facts (times tables, single-digit sums, round-number versions of them, x10/x100) are not
    realistic slip sites: 3*3=10, 6*8=46, 10+30=50, 1800*10=18010 were all found on manual review."""
    ops, leaves = c["ops"], c["leaves"]
    if "/" in ops and Fraction(100) in leaves[1:] and any(v % 100 == 0 for v in leaves if v != 100 and v.denominator == 1):
        return "round percentage of a multiple of 100 (e.g. 400*5/100)"
    if set(ops) == {"+"} and all(v.denominator == 1 and v <= 10 for v in leaves):
        return "sum of single-digit numbers"
    if ops == ["*", "/"] and len(leaves) == 3 and leaves[1] == 1 and leaves[0].denominator == 1 \
            and leaves[2] in (2, 3, 4, 5) and leaves[0] % 10 == 0:
        return "simple fraction (x*1/n) of a round number"
    if len(ops) != 1:
        return None
    if ops[0] == "*" and Fraction(2) in leaves:
        return "doubling is mental arithmetic"
    if ops[0] == "*" and len(leaves) == 2:
        frac_leaf = [v for v in leaves if v.denominator != 1]
        int_leaf = [v for v in leaves if v.denominator == 1]
        if len(frac_leaf) == 1 and int_leaf and frac_leaf[0].denominator in (2, 4, 5, 10) and int_leaf[0] % 10 == 0:
            return "simple fraction/percentage (x0.5, x0.75, x0.2, ...) of a round number"
    if any(v.denominator != 1 for v in leaves):
        return None
    a, b = (int(v) for v in leaves)
    op = ops[0]
    if op in "*/" and any(v in (10, 100, 1000) for v in (a, b) if op == "*" or v == b):
        return f"{op} by a power of ten (digits shift only)"
    if op in "+-*":
        x, y = a, b
        while x and y and x % 10 == 0 and y % 10 == 0:   # 90+20 -> 9+2
            x, y = x // 10, y // 10
        if op == "*":
            x, y = (x // 10 ** (len(str(x)) - len(str(x).rstrip("0"))) if x else x,
                    y // 10 ** (len(str(y)) - len(str(y).rstrip("0"))) if y else y)  # 3*600 -> 3*6
        if x <= 10 and y <= 10:
            return f"single-digit {op} fact (reduces to {x}{op}{y})"
        if op == "*" and x <= 12 and y <= 12:
            return f"times-table fact up to 12 (reduces to {x}*{y})"
        if op == "*" and min(x, y) <= 9 and all(int(d) * min(x, y) < 10 for d in str(max(x, y))):
            return "multiplication with no carries (e.g. 22*3)"
    if op in "+-" and min(a, b) <= 3:
        return f"counting step ({op}{min(a, b)}), not a calculation"
    if op in "+-" and b in (10, 100, 1000):
        return f"{op}{b} only changes one digit"
    if op in "+-" and a % 10 == 0 and b % 10 == 0:
        return "addition/subtraction of round numbers is mental arithmetic"
    if op == "*" and 2 in (a, b):
        return "doubling is mental arithmetic"
    if op == "/" and b == 2:
        return "halving is mental arithmetic"
    if (op == "+" and (a % 10 == 0 or b % 10 == 0)) or (op == "-" and (b % 10 == 0 or (a % 10 == 0 and b <= 9))):
        return "round-number addition/subtraction leaves the ones digit trivial"
    if op in "+-" and max(a, b) <= 20 and min(a, b) <= 9:
        return "small-number fact (both values within 20)"
    if op == "+" and min(a, b) <= 9 and max(a, b) % 10 + min(a, b) < 10:
        return "single-digit addition without a carry"
    if op == "-" and b <= 9 and a % 10 >= b:
        return "single-digit subtraction without a borrow"
    if op == "/" and b in (2, 3, 4, 5) and a % 10 == 0:
        return f"simple fraction (1/{b}) of a round number"
    if op == "/":
        x, y = a, b
        while x and y and x % 10 == 0 and y % 10 == 0:   # 240/60 -> 24/6
            x, y = x // 10, y // 10
        if y <= 10 and x % y == 0 and x // y <= 10:
            return f"times-table division fact (reduces to {x}/{y})"
    return None


# ------------------------------------------------------------------ wording check
def result_revealed(g, k):
    """Flag only wording that hands over the numerical result, making the slip trivial without reasoning."""
    c = g["calcs"][k]
    r = c["value"]
    reasons = []
    if r in g["q_nums"]:
        reasons.append(f"the correct result {s2.num_out(r)} is stated (as a number or number word) in the question")
    ops, leaves = c["ops"], c["leaves"]
    if len(ops) == 1 and ((ops[0] in "*/" and leaves[1] == 1) or (ops[0] == "*" and leaves[0] == 1)
                          or (ops[0] in "+-" and 0 in leaves)):
        reasons.append("identity operation (x*1, x/1, x+0): the result is visibly one of the operands")
    if reasons:
        return True, "; ".join(reasons)
    return False, (f"the correct result {s2.num_out(r)} does not appear in the question or step wording, and the "
                   "operation is not an identity; detecting the slip requires redoing the arithmetic")


# ------------------------------------------------------------------ M3 validator
def validate_m3(rec, g, cor):
    chk = {}
    try:
        G = s2.parse_trace(rec["answer"])            # strict: every gold annotation must be exact
        chk["A_gold_parses_with_all_calculations_correct"] = True
    except s2.ParseError as e:
        return {"A_gold_parses_with_all_calculations_correct": False, "detail": e.reason, "ALL_PASS": False}
    chk["A_gold_final_equals_last_calc"] = G["calcs"][-1]["value"] == G["final"]
    chk["A_gold_final_equals_dataset_answer"] = G["final"] == s2.frac(rec["answer"].rsplit(s2.FINAL_SEP, 1)[1].strip())
    try:
        C = s2.parse_trace(cor["corrupted_answer_text"], strict_arithmetic=False)
        chk["B_corrupted_parses"] = True
    except s2.ParseError as e:
        chk.update(B_corrupted_parses=False, detail=f"{e.reason}: {e.detail}", ALL_PASS=False)
        return chk
    chk["B_same_step_and_calc_layout"] = (len(G["lines"]) == len(C["lines"]) and len(G["calcs"]) == len(C["calcs"]) and
                                         all(x["line_index"] == y["line_index"] for x, y in zip(G["calcs"], C["calcs"])))
    if not chk["B_same_step_and_calc_layout"]:
        chk["ALL_PASS"] = False
        return chk
    k, aff = cor["calc_id"], set(cor["affected"])
    gl, cl = G["lines"], C["lines"]
    gk, ck = G["calcs"][k], C["calcs"][k]
    e_li = gk["line_index"]
    chk["B_steps_before_error_unchanged"] = gl[:e_li] == cl[:e_li]
    chk["B_error_step_operands_unchanged"] = gk["leaves"] == ck["leaves"]
    chk["B_error_step_operator_unchanged"] = gk["ops"] == ck["ops"]
    chk["B_error_step_expression_text_unchanged"] = (gk["ann_expr"] == ck["ann_expr"] and
                                                     gl[e_li][slice(*gk["vis_span"])] == cl[e_li][slice(*ck["vis_span"])])
    chk["B_error_step_claims_recorded_wrong_result"] = ck["value"] == cor["corrupted_result"]
    chk["B_error_step_is_mathematically_wrong"] = ck["value"] != ck["evaluated"]
    chk["B_error_step_true_value_equals_gold"] = ck["evaluated"] == gk["value"]
    inexact = [c["calc_id"] for c in C["calcs"] if c["value"] != c["evaluated"]]
    chk["B_only_the_M3_step_is_arithmetically_wrong"] = inexact == [k]
    ok_down, ok_unrel, op_changes = True, True, 0
    for gc, cc in zip(G["calcs"], C["calcs"]):
        op_changes += gc["ops"] != cc["ops"]
        if gc["calc_id"] == k:
            continue
        if gc["calc_id"] in aff:
            if gc["ops"] != cc["ops"] or cc["value"] != cc["evaluated"]:
                ok_down = False
            for i, s in enumerate(g["calcs"][gc["calc_id"]]["operand_sources"]):
                want = C["calcs"][s]["value"] if isinstance(s, int) and s in aff else gc["leaves"][i]
                if cc["leaves"][i] != want:
                    ok_down = False
        elif gl[gc["line_index"]] != cl[cc["line_index"]] or gc["value"] != cc["value"]:
            ok_unrel = False
    chk["B_downstream_dependents_recomputed_from_corrupted_value"] = ok_down
    chk["B_unrelated_calcs_unchanged"] = ok_unrel
    chk["B_no_operator_changed_anywhere"] = op_changes == 0
    chk["B_corrupted_final_equals_last_calc"] = C["calcs"][-1]["value"] == C["final"]
    chk["B_corrupted_final_differs_from_gold"] = C["final"] != G["final"]
    chk["B_prose_outside_calc_spans_unchanged"] = all(
        s2.free_prose(gl[i], s2.calc_on_line(G, i)) == s2.free_prose(cl[i], s2.calc_on_line(C, i)) for i in range(len(gl)))
    chk["B_lines_changed_only_where_graph_says"] = all(
        (gl[i] != cl[i]) == any(c["line_index"] == i for c in G["calcs"] if c["calc_id"] in aff) for i in range(len(gl)))
    chk["C_visible_expression_matches_annotation"] = True   # parse_trace raises otherwise
    chk["C_visible_result_matches_annotation_claim"] = True  # parse_trace raises otherwise
    chk["D_no_review_markers_in_trace"] = ">>>" not in cor["corrupted_answer_text"] and "INJECTED" not in cor["corrupted_answer_text"]
    chk["ALL_PASS"] = all(v for kk, v in chk.items() if kk != "detail")
    return chk


# ------------------------------------------------------------------ records and review text
def build_record(i, rec, g, cor, checks, revealed, rev, cand_id):
    calcs = g["calcs"]
    k = cor["calc_id"]
    C = s2.parse_trace(cor["corrupted_answer_text"], strict_arithmetic=False)
    chain = []
    for c, cc in zip(calcs, C["calcs"]):
        chain.append({
            "calc_id": c["calc_id"], "step_id": c["step"], "operators": c["ops"],
            "operands": [s2.num_out(v) for v in c["leaves"]],
            "operand_sources": [f"calc_{s}" if isinstance(s, int) else s for s in c["operand_sources"]],
            "dependencies_on_previous_calc_ids": c["depends_on"],
            "gold_expression": c["ann_expr"], "gold_result": s2.num_out(c["value"]),
            "role": "INJECTED_M3_ERROR" if c["calc_id"] == k else ("propagated" if c["calc_id"] in cor["affected"] else "unaffected"),
            "corrupted_expression": cc["ann_expr"], "corrupted_claimed_result": s2.num_out(cc["value"]),
        })
    r, w = cor["correct_result"], cor["corrupted_result"]
    record = {
        "candidate_id": cand_id, "source": "gsm8k/train.jsonl", "source_index": i, "question": rec["question"],
        "gold_steps": [s2.clean(l) for l in g["lines"]], "gold_steps_annotated": g["lines"],
        "gold_answer": s2.num_out(g["final"]), "gold_answer_text": rec["answer"],
        "corrupted_steps": [s2.clean(l) for l in cor["corrupted_lines"]], "corrupted_steps_annotated": cor["corrupted_lines"],
        "corrupted_answer": s2.num_out(cor["corrupted_final"]), "corrupted_answer_text": cor["corrupted_answer_text"],
        "n_steps": len(g["lines"]), "n_calcs": len(calcs),
        "error_type": "M3_calculation_slip", "error_step": cor["error_step"], "calc_id": k,
        "expression": calcs[k]["ann_expr"], "operator": calcs[k]["ops"], "operands": [s2.num_out(v) for v in calcs[k]["leaves"]],
        "correct_result": s2.num_out(r), "corrupted_result": s2.num_out(w),
        "difference": s2.num_out(w - r), "relative_difference": round(float((w - r) / r), 4),
        "propagated_calc_ids": [c for c in cor["affected"] if c != k],
        "dependency_chain": chain,
        "semantic_plausibility": {"pass": rev["pass"], "reason": rev["reason"],
                                  "reviewer": "Claude (coding assistant) during dataset construction; human review pending"},
        "result_revealed_by_wording": {"revealed": revealed[0], "reason": revealed[1]},
        "automatic_validation": checks,
        "manual_review": {"approved": None, "notes": ""},
    }
    record["propagation_explanation"] = propagation_explanation(record)
    return record


def propagation_explanation(r):
    chain = r["dependency_chain"]
    changed = {c["calc_id"] for c in chain if c["role"] != "unaffected"}
    out = []
    for c in chain:
        if c["role"] == "INJECTED_M3_ERROR":
            out.append(f"Step {c['step_id']} (calc_{c['calc_id']}): INJECTED M3. Same expression {c['gold_expression']}, "
                       f"claimed result {c['gold_result']} -> {c['corrupted_claimed_result']}.")
        elif c["role"] == "propagated":
            ups = [d for d in c["dependencies_on_previous_calc_ids"] if d in changed]
            via = ", ".join(f"calc_{d} (step {chain[d]['step_id']}: {chain[d]['gold_result']} -> {chain[d]['corrupted_claimed_result']})" for d in ups)
            out.append(f"Step {c['step_id']} (calc_{c['calc_id']}): RECOMPUTED because it uses {via}; correct given that value: "
                       f"{c['gold_expression']}={c['gold_result']} -> {c['corrupted_expression']}={c['corrupted_claimed_result']}.")
        else:
            deps = c["dependencies_on_previous_calc_ids"]
            why = ("it precedes the error" if c["step_id"] < r["error_step"] else
                   "its operands come only from the question" if not deps else
                   f"it depends only on unchanged {', '.join(f'calc_{d}' for d in deps)}")
            out.append(f"Step {c['step_id']} (calc_{c['calc_id']}): unchanged, {why}: {c['gold_expression']}={c['gold_result']}.")
    return out


def review_block(n, r):
    out = ["=" * 78, f"CANDIDATE {n}   ({r['candidate_id']})", f"SOURCE INDEX: train[{r['source_index']}]",
           f"QUESTION: {r['question']}", "", "GOLD TRACE:"]
    out += [f"  Step {s}: {t}" for s, t in enumerate(r["gold_steps"], 1)]
    out += ["", "CORRUPTED TRACE:"]
    roles = {c["step_id"]: c["role"] for c in r["dependency_chain"] if c["role"] != "unaffected"}
    for s, t in enumerate(r["corrupted_steps"], 1):
        mark = {"INJECTED_M3_ERROR": "   >>> INJECTED M3 ERROR HERE <<<", "propagated": "   [recomputed from corrupted value]"}.get(roles.get(s), "")
        out.append(f"  Step {s}: {t}{mark}")
    out += ["", "INJECTED M3 ERROR:", f"  error step: {r['error_step']}  (calc_id {r['calc_id']})",
            f"  expression: {r['expression']}   (operator {' '.join(r['operator'])}, operands {r['operands']})",
            f"  correct result: {r['correct_result']}", f"  corrupted result: {r['corrupted_result']}",
            f"  difference: {r['difference']:+}   relative difference: {r['relative_difference']:+.2%}", "",
            "DEPENDENCY PROPAGATION:"] + [f"  {x}" for x in r["propagation_explanation"]]
    sp, rw, v = r["semantic_plausibility"], r["result_revealed_by_wording"], r["automatic_validation"]
    out += ["", f"GOLD ANSWER: {r['gold_answer']}", f"CORRUPTED ANSWER: {r['corrupted_answer']}", "",
            f"SEMANTIC PLAUSIBILITY: {'PASS' if sp['pass'] else 'FAIL' if sp['pass'] is False else 'NOT REVIEWED'}",
            f"  {sp['reason']}", "",
            f"RESULT REVEALED BY WORDING? {'yes' if rw['revealed'] else 'no'}", f"  {rw['reason']}", "",
            f"AUTOMATIC VALIDATION: {'PASS' if v['ALL_PASS'] else 'FAIL'}"]
    out += [f"  [{'PASS' if val is True else 'FAIL'}] {kk}" for kk, val in v.items() if kk != "ALL_PASS"]
    out.append("=" * 78)
    return "\n".join(out)


# ------------------------------------------------------------------ main
def main():
    records = [json.loads(l) for l in open(s2.RAW_TRAIN, encoding="utf-8")]
    eligible = {}
    for i, rec in enumerate(records):
        g, _ = s2.build_graph(rec)
        if g is not None:
            eligible[i] = g

    generated, rejected = 0, Counter()
    structural = []
    for i, g in eligible.items():
        for c in g["calcs"][:-1]:
            for delta in DELTAS:
                generated += 1
                cor, why = make_m3(g, c["calc_id"], delta)
                if cor is None:
                    rejected[why] += 1
                    continue
                checks = validate_m3(records[i], g, cor)
                if not checks["ALL_PASS"]:
                    rejected["automatic_validation_failed"] += 1
                    continue
                structural.append((i, cor, checks))

    wording_rejected, pool, digit_rejected = 0, [], Counter()
    for i, cor, checks in structural:
        calc = eligible[i]["calcs"][cor["calc_id"]]
        dp = elementary_fact(calc) or digit_pattern_problem(calc, cor["corrupted_result"])
        if dp:
            digit_rejected[dp.split(" (")[0]] += 1
            continue
        rv = result_revealed(eligible[i], cor["calc_id"])
        if rv[0]:
            wording_rejected += 1
        else:
            pool.append((i, cor, checks, rv))

    # one preferred corruption per (problem, calc): smallest |delta| that survived, +1 before -1
    best = {}
    for e in pool:
        key = (e[0], e[1]["calc_id"])
        if key not in best or DELTAS.index(e[1]["delta"]) < DELTAS.index(best[key][1]["delta"]):
            best[key] = e
    by_item = {}
    for e in best.values():
        by_item.setdefault(e[0], []).append(e)

    rng = random.Random(SEED)
    order = sorted(by_item)
    rng.shuffle(order)
    counts = {k: Counter() for k in DIVERSITY_CAPS}
    selected, pending, reviewed = [], [], []
    for i in order:
        if len(selected) >= N_CANDIDATES:
            break
        opts = sorted(by_item[i], key=lambda e: (abs(e[1]["delta"]), -e[1]["error_step"]))
        for e in opts:
            cor = e[1]
            kv = {"error_step": cor["error_step"], "n_steps": len(eligible[i]["lines"]),
                  "operator": "".join(eligible[i]["calcs"][cor["calc_id"]]["ops"]), "delta": cor["delta"]}
            if not all(counts[k][kv[k]] < DIVERSITY_CAPS[k] for k in DIVERSITY_CAPS):
                continue
            rev = REVIEWER_SEMANTIC_CHECK.get((i, cor["calc_id"], s2.num_out(cor["corrupted_result"])))
            if rev is None:
                pending.append(e)
                break
            reviewed.append((e, rev))
            if not rev["pass"]:
                continue
            selected.append((e, rev))
            for k in DIVERSITY_CAPS:
                counts[k][kv[k]] += 1
            break

    sem_fail = [x for x in reviewed if not x[1]["pass"]]
    # pass rate over EVERY recorded verdict whose corruption is still in the automatic pool (for scaling estimates)
    pool_keys = {(e[0], e[1]["calc_id"], s2.num_out(e[1]["corrupted_result"])) for e in best.values()}
    in_pool = {k: v for k, v in REVIEWER_SEMANTIC_CHECK.items() if k in pool_keys}
    stats = {
        "seed": SEED,
        "structurally_eligible_problems": len(eligible),
        "eligible_problems_with_a_non_final_calc_at_step_2_or_later": len({i for i, g in eligible.items()
                                                                            if any(c["step"] >= MIN_ERROR_STEP for c in g["calcs"][:-1])}),
        "m3_corruptions_generated": generated,
        "m3_corruptions_rejected_automatic": sum(rejected.values()),
        "automatic_rejection_reasons": dict(rejected.most_common()),
        "m3_corruptions_structurally_valid": len(structural),
        "rejected_semantic_automatic_digit_pattern": sum(digit_rejected.values()),
        "semantic_automatic_digit_pattern_reasons": dict(digit_rejected.most_common()),
        "rejected_result_revealed_by_wording": wording_rejected,
        "pool_after_wording_check_corruptions": len(pool),
        "pool_after_wording_check_problem_calc_pairs": len(best),
        "pool_after_wording_check_distinct_problems": len(by_item),
        "reviewer_verdicts_recorded_total": len(REVIEWER_SEMANTIC_CHECK),
        "reviewer_verdicts_on_items_still_in_automatic_pool": len(in_pool),
        "reviewer_passes_on_items_still_in_automatic_pool": sum(v["pass"] for v in in_pool.values()),
        "reviewer_semantic_checks_done": len(reviewed),
        "reviewer_semantic_failures": len(sem_fail),
        "reviewer_semantic_failure_reasons": [{"key": [e[0], e[1]["calc_id"], s2.num_out(e[1]["corrupted_result"])], "reason": r["reason"]}
                                              for e, r in sem_fail],
        "pending_reviewer_checks": len(pending),
    }

    if pending:
        print(f"{len(pending)} candidate(s) need a reviewer semantic check:\n")
        for i, cor, checks, rv in pending[:N_PENDING_TO_LIST]:
            g = eligible[i]
            print("-" * 78)
            print(f"KEY ({i}, {cor['calc_id']}, {s2.num_out(cor['corrupted_result'])})   step {cor['error_step']}   "
                  f"{g['calcs'][cor['calc_id']]['ann_expr']} = {s2.num_out(cor['correct_result'])} -> {s2.num_out(cor['corrupted_result'])}")
            print("Q:", g["question"])
            for s, (gl, cl) in enumerate(zip(g["lines"], cor["corrupted_lines"]), 1):
                print(f"  {s}. {s2.clean(gl)}" + (f"\n  {s}*{s2.clean(cl)}" if gl != cl else ""))
            print(f"  final {s2.num_out(g['final'])} -> {s2.num_out(cor['corrupted_final'])}")
    if len(selected) < N_CANDIDATES:
        stats["status"] = f"INCOMPLETE: {len(selected)} of {N_CANDIDATES} selected"
        # never leave a previous run's candidate file looking current
        for stale in (OUT_JSON, OUT_REVIEW, OUT_SAMPLE_AUDIT):
            if stale.exists():
                stale.unlink()
        with open(OUT_STATS, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, default=str)
        print(json.dumps(stats, indent=2, default=str))
        return 2

    out = [build_record(e[0], records[e[0]], eligible[e[0]], e[1], e[2], e[3], rev, f"m3_c{n:02d}")
           for n, (e, rev) in enumerate(selected, 1)]
    assert all(r["automatic_validation"]["ALL_PASS"] and r["semantic_plausibility"]["pass"]
               and not r["result_revealed_by_wording"]["revealed"] for r in out)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"seed": SEED, "generator": "scripts/build_m3_candidates.py",
                   "structural_pipeline": "scripts/build_m1_candidates.py (reused)", "candidates": out}, f, indent=2, ensure_ascii=False)
    with open(OUT_REVIEW, "w", encoding="utf-8") as f:
        f.write("M3 CANDIDATES FOR MANUAL REVIEW  (markers exist ONLY in this review file)\n\n")
        f.write("\n\n".join(review_block(n, r) for n, r in enumerate(out, 1)) + "\n")
    # every reviewed-and-rejected item is also written out in full, so each verdict can be audited
    rejected_records = [build_record(e[0], records[e[0]], eligible[e[0]], e[1], e[2], e[3], rev, f"m3_rejected_{n:02d}")
                        for n, (e, rev) in enumerate(sem_fail, 1)]
    with open(OUT_SAMPLE_AUDIT, "w", encoding="utf-8") as f:
        f.write("M3 ITEMS REJECTED BY THE REVIEWER SEMANTIC CHECK (full audit)\n\n")
        f.write("\n\n".join(review_block(n, r) for n, r in enumerate(rejected_records, 1)) + "\n")
    stats["status"] = "COMPLETE"
    stats["selected"] = {
        "source_indices": [r["source_index"] for r in out],
        "error_step_distribution": dict(sorted(Counter(r["error_step"] for r in out).items())),
        "operator_distribution": dict(Counter("".join(r["operator"]) for r in out)),
        "difference_distribution": dict(Counter(r["difference"] for r in out)),
        "n_steps_distribution": dict(sorted(Counter(r["n_steps"] for r in out).items())),
    }
    with open(OUT_STATS, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, default=str)
    print(open(OUT_REVIEW, encoding="utf-8").read())
    print(json.dumps(stats, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
