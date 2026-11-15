"""Stage 5: build the 40-item M3 (calculation slip) pilot set for MANUAL REVIEW.

Reuses, unchanged, the validated M3 machinery in build_m3_candidates.py (corruption, realism rules,
wording check, M3 validator) and the Stage 2 structural pipeline underneath it. New here:

  * one corruption per GSM8K problem, 40 problems;
  * error-direction balancing (target 20 over / 20 under) and magnitude/operator/step diversity,
    applied only as a SELECTION preference; no quality filter is relaxed to meet a target;
  * a gold-text quality gate (automatic flags + a recorded per-problem reading of the gold trace);
  * clean student-facing traces (no <<...>> annotations, no metadata) kept separate from the
    internal annotated traces;
  * an automatic leakage audit of everything a student model would see.

No language model is called. No feedback prompts are built. Earlier stage files are not touched.

Usage (from the project root):
    PYTHONIOENCODING=utf-8 python scripts/build_m3_pilot40.py
"""
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_m1_candidates as s2  # noqa: E402  (structural pipeline)
import build_m3_candidates as m3  # noqa: E402  (validated M3 machinery)

ROOT = s2.ROOT
OUT_JSON = ROOT / "data" / "pilot" / "m3_pilot_40.json"
OUT_REVIEW = ROOT / "outputs" / "m3_pilot_40_review.txt"
OUT_STATS = ROOT / "outputs" / "m3_pilot_40_stats.json"

SEED = 42
N_ITEMS = 40
TARGET_PER_DIRECTION = 20
MAX_PER_DIRECTION = 22            # small imbalance tolerated only if strict filtering forces it
MAGNITUDE_CAPS = {1: 18, 2: 14, 10: 10}
OPERATOR_CAP = 15
ERROR_STEP_CAP = 15            # was 14; raised by 1 after the stricter gold-text gate exhausted the reviewed pool at 39
N_PENDING_TO_LIST = 45

# Words a student model must never see as an experimental label. If one occurs NATIVELY in a GSM8K
# problem (e.g. "gold coins"), the problem is excluded anyway: an alternative exists, and the word
# could prime the model.
LEAK_TERMS = ["injected", "inject", "error step", "error", "corrupted", "corrupt", "gold", "wrong",
              "mistake", "incorrect", "m3", "calc_", "train[", "source", "validation", "pass]", "fail]",
              "approve", "reject", "manual review", "slip", "<<", ">>", "####"]

# ------------------------------------------------------------------ recorded human-style checks
# Gold-text reading per problem: is every gold sentence well-formed, typo-free and correctly reasoned?
# Recorded by Claude (coding assistant) while building the set; the human review gate is still pending.
GOLD_TEXT_CHECK = {
    2660: (False, "gold step 3 has the typo 'Ha had' (excluded by user decision)"),
    2080: (False, "gold step 5 mislabels a weekly total as 'per month'"),
    2048: (False, "gold solution breaks one sentence across two lines ('per' / 'hour.')"),
    4648: (False, "gold step 3 has a grammar error ('his monthly expenses is')"),
    5733: (False, "question contains the typo 'multiples the number' (for 'multiplies')"),
    6382: (False, "question grammar error ('comprising of 4 groups')"),
    1963: (True, "gold sentences well-formed; reasoning correct"),
    6427: (True, "gold sentences well-formed; reasoning correct (6 full days = 144 hours)"),
    3708: (True, "gold sentences well-formed; reasoning correct"),
    6061: (True, "gold sentences well-formed; reasoning correct"),
    1049: (True, "gold sentences well-formed; reasoning correct"),
    3092: (True, "gold sentences well-formed; reasoning correct"),
    825: (True, "gold sentences well-formed; reasoning correct"),
    6942: (True, "gold sentences well-formed; reasoning correct"),
    376: (True, "gold sentences well-formed; reasoning correct"),
    5284: (False, "question contains a sentence fragment ('If Kelly sells these eggs for $5 a dozen.'), cf. train[161]"),
    5640: (True, "gold sentences well-formed; reasoning correct"),
    3405: (True, "gold sentences well-formed; reasoning correct"),
    7040: (True, "gold sentences well-formed; reasoning correct"),
    146: (True, "gold sentences well-formed; reasoning correct"),
    2766: (True, "gold sentences well-formed; reasoning correct"),
    3567: (True, "gold sentences well-formed; reasoning correct"),
    4234: (True, "gold sentences well-formed; reasoning correct"),
    489: (True, "gold sentences well-formed; reasoning correct"),
    6111: (True, "gold sentences well-formed; reasoning correct"),
    63: (True, "gold sentences well-formed; reasoning correct (30 + 18 + 11 matches the question)"),
    4694: (False, "gold reasoning error: the question says 3/4 of the SECOND week's shed leaves, but gold takes 3/4 of the remaining 360"),
    4571: (False, "gold names the wrong animal ('then Rover has 18*5=90 spots' should be Granger)"),
    1521: (False, "gold renames Selene as 'Selena' and has 'Two instant camera cost'"),
    5190: (True, "gold sentences well-formed; reasoning correct"),
    4009: (False, "gold says 'to his friends' where the question has one friend"),
    3389: (True, "gold sentences well-formed; reasoning correct"),
    3435: (True, "gold sentences well-formed; reasoning correct"),
    337: (True, "gold sentences well-formed; reasoning correct"),
    6303: (True, "gold sentences well-formed; reasoning correct"),
    6728: (True, "gold sentences well-formed; reasoning correct"),
    6141: (False, "gold has the typo 'Mallory gammy's house'"),
    6777: (True, "gold sentences well-formed; reasoning correct"),
    861: (True, "gold sentences well-formed; reasoning correct"),
    1066: (True, "gold sentences well-formed; reasoning correct"),
    5149: (True, "gold sentences well-formed ('fishes' is acceptable usage); reasoning correct"),
    2458: (False, "gold step 4 has a grammar error ('his total expenses last month was')"),
    7208: (True, "gold sentences well-formed; reasoning correct"),
    4402: (True, "gold sentences well-formed; reasoning correct"),
    2622: (True, "gold sentences well-formed; reasoning correct"),
    1375: (False, "gold step 5 is garbled ('Therefore, the earn the farmer')"),
    3228: (True, "gold sentences well-formed; reasoning correct"),
    6599: (False, "question grammar error ('twice as much potatoes')"),
    3305: (False, "gold uses the malformed phrase 'Du Chin remains with'"),
    7365: (True, "gold sentences well-formed; reasoning correct"),
    6928: (True, "gold sentences well-formed; reasoning correct"),
    1295: (True, "gold sentences well-formed; reasoning correct"),
    4836: (True, "gold sentences well-formed; reasoning correct"),
    6093: (False, "problem statement is ambiguous (yesterday/today/'5 more than the previous day'); gold interpretation is debatable"),
    2752: (True, "gold sentences well-formed; reasoning correct"),
    6479: (True, "gold sentences well-formed; reasoning correct"),
    2890: (True, "gold sentences well-formed; reasoning correct"),
    3257: (True, "gold sentences well-formed; reasoning correct"),
    5847: (True, "gold sentences well-formed; reasoning correct"),
    161: (False, "question contains a sentence fragment ('If half of the budget goes ... education.')"),
    646: (True, "gold sentences well-formed; reasoning correct"),
    4366: (True, "gold sentences well-formed; reasoning correct"),
    2728: (True, "gold sentences well-formed; reasoning correct"),
    6887: (True, "gold sentences well-formed; reasoning correct"),
    2631: (False, "gold grammar error ('for making pair of pants')"),
    3619: (True, "gold sentences well-formed; reasoning correct"),
    2847: (False, "gold reasons with fractional mushrooms (38 / 4 = 9.5); suspicious gold reasoning"),
    6652: (True, "gold sentences well-formed; reasoning correct"),
    3061: (False, "gold grammar error ('her total expenses was')"),
    5748: (True, "gold sentences well-formed; reasoning correct"),
    2764: (True, "gold sentences well-formed; reasoning correct"),
    3252: (False, "gold calls the musicians 'students' ('number of students in the choir')"),
    6888: (True, "gold sentences well-formed; reasoning correct"),
    3695: (True, "gold sentences well-formed; reasoning correct"),
    3738: (False, "question typo ('the price of a back had increased')"),
    3680: (True, "gold sentences well-formed; reasoning correct"),
    6730: (True, "gold sentences well-formed; reasoning correct"),
    3249: (True, "gold sentences well-formed; reasoning correct"),
    806: (True, "gold sentences well-formed; reasoning correct"),
    4284: (True, "gold sentences well-formed; reasoning correct"),
    1408: (True, "gold sentences well-formed; reasoning correct"),
    6085: (False, "gold steps 1-4 are verbless fragments ('8 gallons of water added during the first hour')"),
    2454: (True, "gold sentences well-formed; reasoning correct"),
    3108: (True, "gold sentences well-formed; reasoning correct"),
    4870: (False, "question wording is unidiomatic ('the concert organizer made his accounts')"),
    1836: (True, "gold sentences well-formed; reasoning correct"),
    5481: (True, "gold sentences well-formed; reasoning correct"),
    4304: (True, "gold sentences well-formed; reasoning correct"),
    134: (True, "gold sentences well-formed; reasoning correct"),
    7046: (False, "question opens with a comma splice ('The rainy season is here, Jan collected'), cf. train[7261]"),
    3303: (False, "question/gold grammar error ('a 500 pages book')"),
    658: (False, "question mixes currencies ($950 vs a non-dollar symbol before 920) while gold uses $ throughout"),
    1274: (False, "question has a mid-sentence capital ('ate Pizza'), cf. train[1245]"),
    59: (False, "gold steps 1-4 are verbless fragments ('15 coins collected in hour one')"),
    6020: (True, "gold sentences well-formed; reasoning correct"),
    2016: (False, "gold unit error ('10 × $42' puts the dollar sign on the 42 hours) and the non-standard 'earnt'"),
    4932: (True, "gold sentences well-formed; reasoning correct"),
    457: (False, "gold formatting typo ('108 +54 = 162')"),
    4627: (False, "gold uses the non-standard phrase 'The total she remains with'"),
    3380: (True, "gold sentences well-formed; reasoning correct"),
    2150: (False, "gold formatting typo ('4 x 7= 28'); question ambiguous ('son and daughter eat 2 eggs')"),
    6858: (False, "question typo ('Few weeks had past')"),
    1780: (True, "gold sentences well-formed; reasoning correct"),
    1245: (False, "question has a mid-sentence capital ('today, How many')"),
    6010: (False, "gold says 'for 10 feet' in a problem stated in meters"),
    4018: (False, "gold formatting typo ('18 x $14= $252')"),
    436: (False, "gold uses the non-standard phrase 'She remained with'"),
    1267: (True, "gold sentences well-formed; reasoning correct"),
    4838: (False, "gold reasoning error: question says SIX sections, gold multiplies by 4 ('the four sections')"),
    3100: (False, "gold muddles apples and blueberries ('135 apples which are 3 times as many blueberries')"),
    6946: (True, "gold sentences well-formed; reasoning correct"),
    4104: (False, "gold grammar error ('Annie spend')"),
    3178: (False, "gold mixes 'metres' and 'meters'"),
    5519: (True, "gold sentences well-formed; reasoning correct"),
    4501: (False, "inconsistent capitalisation of school names ('Roosevelt High school' / 'Roosevelt high school')"),
    751: (False, "gold says 'Tuesday to Wednesday' where the question says Tuesday to Thursday"),
    3340: (True, "gold sentences well-formed; reasoning correct"),
    6327: (True, "gold sentences well-formed; reasoning correct"),
    3612: (True, "gold sentences well-formed; reasoning correct"),
    3747: (True, "gold sentences well-formed; reasoning correct"),
    1075: (True, "gold sentences well-formed; reasoning correct"),
    6363: (True, "gold sentences well-formed; reasoning correct"),
    2809: (True, "gold sentences well-formed; reasoning correct"),
    3297: (False, "question typo ('Tom's puts 30 miles')"),
    1019: (True, "gold sentences well-formed; reasoning correct"),
    7057: (True, "gold sentences well-formed; reasoning correct"),
    7261: (False, "question grammar error ('What the volume of fuel oil that was used')"),
    2999: (False, "question calls the marbles 'balls' ('50% fewer green balls than yellow balls')"),
    183: (True, "gold sentences well-formed; reasoning correct"),
    6296: (True, "gold sentences well-formed; reasoning correct"),
    5435: (False, "gold mislabels protein POWDER as 'grams of protein per day' in step 2"),
    287: (True, "gold sentences well-formed; reasoning correct"),
    5834: (False, "awkward gold sentence ('Since half of the spaces are filled then, 38 x 1/2 ...')"),
    1587: (True, "gold sentences well-formed; reasoning correct"),
    7410: (True, "gold sentences well-formed; reasoning correct"),
    6725: (True, "gold sentences well-formed; reasoning correct"),
    5067: (True, "gold sentences well-formed; reasoning correct"),
    5198: (False, "question typo ('pairs that Sandra bought' for 'brought'); gold 'Three times more than Lisa bought' misstates the operation"),
    7299: (True, "gold sentences well-formed; reasoning correct"),
    6981: (True, "gold sentences well-formed; reasoning correct"),
    5606: (True, "gold sentences well-formed; reasoning correct"),
    2282: (True, "gold sentences well-formed; reasoning correct"),
    996: (True, "gold sentences well-formed; reasoning correct"),
    3309: (True, "gold sentences well-formed; reasoning correct"),
    2483: (True, "gold sentences well-formed; reasoning correct"),
    3900: (False, "question opens with a sentence fragment ('If the wages of 15 workers for 6 days is $9450.')"),
    122: (False, "gold apostrophe error ('the chair's capacity' for the chairs')"),
    4951: (True, "gold sentences well-formed; reasoning correct"),
    2463: (False, "gold reasoning error: gold step 1 says athletes left 'on Saturday morning', but the question says 'the next morning' (Sunday) and gold step 3 says Sunday"),
    7080: (True, "gold sentences well-formed; reasoning correct"),
    5347: (True, "gold sentences well-formed; reasoning correct"),
    2596: (False, "malformed gold sentence ('which is his History score is = total/3 = ...')"),
    2692: (False, "gold grammar error ('The sum of Randy's score' for 'scores')"),
    6088: (True, "gold sentences well-formed; reasoning correct"),
    5100: (False, "gold step 3 justification is vague ('If he still needs to buy more lights' omits the 5)"),
    1544: (True, "gold sentences well-formed; reasoning correct"),
    841: (False, "gold uses the non-standard phrase 'she remains with'"),
    5183: (True, "gold sentences well-formed; reasoning correct"),
    2870: (True, "gold sentences well-formed; reasoning correct"),
    2595: (False, "malformed gold sentence ('there are with 120 / 15 = 8 groups')"),
    3143: (True, "gold sentences well-formed; reasoning correct"),
    5245: (True, "gold sentences well-formed; reasoning correct"),
    300: (True, "gold sentences well-formed; reasoning correct"),
    3465: (True, "gold sentences well-formed; reasoning correct"),
    632: (False, "gold switches from 'watermelon seeds' to 'melon seeds'"),
    5283: (False, "malformed gold sentence ('Since he paid half his price was 34/2')"),
    2520: (False, "garbled question ('equally divided among the 6000 war supplies they have in total')"),
    366: (True, "gold sentences well-formed; reasoning correct"),
    1987: (True, "gold sentences well-formed; reasoning correct"),
    2521: (True, "gold sentences well-formed; reasoning correct"),
    3880: (True, "gold sentences well-formed; reasoning correct"),
    6167: (True, "gold sentences well-formed; reasoning correct"),
    6939: (True, "gold sentences well-formed; reasoning correct"),
    4401: (False, "apostrophe plurals in question and gold ('Adidas's', 'Nike's', 'Reebok's')"),
    1644: (True, "gold sentences well-formed; reasoning correct"),
    7126: (True, "gold sentences well-formed; reasoning correct"),
    3841: (True, "gold sentences well-formed; reasoning correct"),
    4492: (True, "gold sentences well-formed; reasoning correct"),
    2120: (True, "gold sentences well-formed; reasoning correct"),
    4197: (False, "gold grammar error ('Her total earnings ... is'), cf. train[3047]"),
    4811: (True, "gold sentences well-formed; reasoning correct"),
    3706: (True, 'gold sentences well-formed; reasoning correct'),
    2976: (True, 'gold sentences well-formed; reasoning correct'),
    4609: (True, 'gold sentences well-formed; reasoning correct'),
    5705: (True, 'gold sentences well-formed; reasoning correct'),
    3208: (False, "gold typo 'Saah' (for Sarah)"),
    5390: (False, "question typo 'A milk teas shop'"),
    251: (False, "question grammar error ('Each pie is having 3 pieces')"),
    3047: (False, "gold grammar error ('the earnings from the play is')"),
    671: (False, "gold formatting typo ('240-144 =96')"),
    4696: (False, "question is an imperative ending in '?' and has the awkward 'pick-up point station'"),
    6367: (False, "question typo ('How Much money')"),
    4824: (False, 'suspicious gold reasoning: treats running/playing/barking dogs as disjoint groups without the question saying so'),
    1178: (False, "question/gold wording ('3/4 times as many', '96* $100')"),
    3154: (False, "gold step 3 is malformed ('She needs to buy this many cans of whipped cream 29 x 2')"),
    2837: (False, "gold formatting ('Leila should pay: 22 * 6', '36 +132')"),
    3367: (False, "gold formatting typo ('372 + 752= $1124')"),
    2237: (False, "gold formatting typo ('100/4= 25')"),
    1532: (False, "question switches tense ('uses ... He also used')"),
    3876: (False, "gold omits the article ('Meat Market sold an extra')"),
    1564: (False, "question contains a sentence fragment ('If she watches 138 minutes of the show on Monday.')"),
    311: (False, "question wording error ('16 people climb' for 'board')"),
    5763: (False, "gold grammar error ('their Saturday earnings was')"),
    4265: (True, 'gold sentences well-formed; reasoning correct'),
    4149: (True, 'gold sentences well-formed; reasoning correct'),
}
# Calculation sites judged unrealistic as slip sites regardless of the delta (mental arithmetic etc.).
SITE_REJECT = {
    (376, 1): "12 + 15: carry-free small sum", (6111, 1): "12 x 3.5: mental", (6111, 3): "12 x 4.5: mental",
    (3435, 1): "1200 / 6: reduces to 12 / 6", (3435, 3): "2000 / 8: round-number division",
    (6728, 1): "21 + 17: carry-free small sum", (6777, 2): "4 x 85: mental", (861, 1): "16 x 5: mental",
    (7208, 1): "3/4 of 20: mental", (2622, 1): "17 + 21: carry-free small sum", (3257, 1): "66 - 22: no borrow",
    (3708, 1): "4 x 9.5: mental", (4402, 1): "16 x 3: mental", (6928, 1): "3/4 of 3000: mental",
    (1295, 1): "4 x 1.25: mental", (4836, 1): "5 x 24: mental", (2752, 1): "24% of 20,000: mental",
    (5847, 1): "20 x 68: mental", (646, 1): "4/5 of 200: mental", (3619, 1): "25% of 16: mental",
    (7040, 2): "2/3 of 15: mental", (5733, 0): "3/4 of 100: mental",
    (6652, 2): "35 x 0.4: mental", (5748, 2): "3/5 of 20,000: mental", (3249, 2): "13.20 x 5: mental",
    (806, 2): "10% of 150: mental", (4284, 3): "3/4 of 600: mental", (1408, 2): "30 x 26: mental (3 x 26)",
    (3108, 2): "32 + 18: units sum to 10, mental",
    (4304, 3): "25 - 12: no borrow", (5519, 3): "360 + 480 + 180: round addends", (64, 4): "15 x 16: mental",
    (6296, 3): "15 x 8: mental", (1500, 3): "80 + 8 + 4: mental", (5050, 3): "100 + 160 + 140 + 120: round addends",
    (6167, 3): "27 x 20: doubling then x10", (4018, 3): "25 x 1.8: mental",
    # consistency audit of the first complete selection: earlier passes that fail the bar applied later
    (2766, 1): "21 + 15: carry-free small sum (cf. 21 + 17)", (146, 2): "22 + 25 + 21: carry-free small sum",
    (6303, 1): "63 - 21: no borrow (cf. 66 - 22)", (1049, 0): "98 - 75: no borrow (cf. 66 - 22)",
    (4234, 1): "60% of 25: mental (cf. 25% of 16)", (5190, 2): "60% of 120: mental (6 x 12)",
    (3389, 2): "20% of 65: mental (65 / 5)", (3228, 1): "3 + 5.25 + 0.75: quarters complete to a whole",
    (1274, 2): "4/5 of 45: mental (45 / 5)", (5519, 2): "15 x 12: times table", (2016, 2): "5% of 420: mental",
    (3340, 2): "20 + 40 + 80: round addends", (3612, 2): "1800 / 50: reduces to 180 / 5",
    (3747, 2): "8 + 16 + 48: small sum, mental", (1075, 2): "110 - 11: mental", (6363, 2): "6 x 12 + 22: mental",
    (2809, 2): "15000 / .02: an odd result on round operands is absurd", (3380, 2): "4% of 475 = 20 reads as rounding",
    (1019, 2): "81 + 41: carry-free sum",
    (7057, 1): "50% of 96: halving", (183, 1): "20 + 8 + 5: small mental sum", (6296, 1): "5 x 16: mental",
    (5435, 1): "160 / .8: round result", (5834, 1): "38 x 1/2: halving", (1587, 1): "70% of 120: 7 x 12 times table",
    (6725, 1): "15 x 3: recalled multiple", (5067, 2): "750000 x 0.02: round", (6327, 1): "20% of 480: mental",
    (5606, 1): "45 + 60 + 30 + 15: round-ish addends", (996, 2): "16 x 6: mental", (3309, 1): "10.5 + 5.5: halves complete",
    (4951, 1): "2/5 of 450: mental",
    (996, 3): "96 + 14: units complete to 10", (7080, 2): "18 + 12 + 20: units complete to 10", (5347, 1): "12 x 1.5: mental",
    (1408, 1): "20 x 26: doubling then x10", (6088, 1): "48 / 3: mental", (1500, 2): "5% of 80: mental",
    (3143, 1): "35 x 6: recalled multiple", (5183, 1): "1/4 of 60: mental", (5640, 1): "3 x 15: recalled multiple",
    (2870, 1): "1/3 of 150: mental", (1780, 1): "5 x 108: mental (half of 1080)",
    (3143, 2): "5 + 35 + 210: units complete to 10", (2870, 2): "1/2 of 150: halving", (5245, 2): "72 x 1.25: mental",
    (300, 3): "17.5 + 10.5: halves complete", (632, 2): "3 x 340: mental (3 x 34)", (2520, 2): "3 x 2000 - 400: round",
    (1987, 2): "5/9 of 72: times table (72 / 9)",
    (3880, 2): "18 x 20: doubling then x10", (6167, 2): "13 x 8: small recalled product (cf. 16 x 3, 15 x 8)",
    (4401, 2): "9 x 35: recalled multiple", (7126, 2): "10% of 170: mental", (3841, 2): "108 + 132: units complete to 10",
    (4811, 2): "50 + 25 + 19: mental",
    (1546, 2): '10 + 20 + 65 + 10: round-number mental sum',
    (2817, 2): '300 - 120 - 30: round-number mental arithmetic',
    (4887, 2): '2/5 of 90: mental fraction',
    (5953, 1): '23 - 4: small mental subtraction',
    (1019, 1): '5 + 36: small mental sum',
    (3401, 1): '21 - 5: small mental subtraction',
    (7068, 1): '150 x 2 + 50: round-number mental arithmetic',
    (1112, 1): '42 / 3: recalled division fact',
    (4093, 1): '2/3 of 15: mental fraction',
    (5769, 1): '24 + 7: small mental sum',
    (6421, 1): '27 + 4: small mental sum',
    (3117, 1): '20% of 45: mental percentage',
    (1075, 1): '10% of 110: mental percentage',
    (4680, 1): '2.5 x 8: mental product',
}
# Semantic verdicts for corruptions not already judged in build_m3_candidates.REVIEWER_SEMANTIC_CHECK.
_ok, _no = m3._ok, m3._no
PILOT_SEMANTIC_CHECK = {
    (6382, 2, 64): _ok("64 instead of 63 children (42 + 21) is a plausible addition slip"),
    (1963, 1, 1729): _ok("$1729 instead of $1728 (60% of 2880) is a plausible percentage slip; savings stay positive"),
    (6427, 1, 10): _ok("10 instead of 9 deliberation days (144 / 16) is a plausible division slip"),
    (3708, 1, 40): _no("4 x 9.5 = 40 reads as rounding 9.5 up to 10, an approximation rather than a calculation slip"),
    (6061, 1, 57): _ok("57 instead of 56 marbles (42 + 14) is plausible; 28 yellow out of 85 is feasible"),
    (1049, 0, 25): _ok("a least score of 25 instead of 23 (98 - 75, with a borrow) is a plausible subtraction slip"),
    (3092, 1, 1130): _ok("$1130 instead of $1120 (14 x 80) is a tens slip that keeps the trailing 0"),
    (825, 2, 166): _ok("$166 instead of $165 off (20% of 825) is a plausible percentage slip"),
    (6942, 2, 278): _ok("278 instead of 276 legs (69 x 4, with carries) is plausible and stays even"),
    (5733, 0, 77): _no("3/4 of 100 is a recalled fact; 77 is not a realistic slip"),
    (3405, 3, 53): _ok("53 instead of 51 hours (15 + 36, with a carry) is a plausible addition slip"),
    (7040, 2, 9): _no("two-thirds of 15 is mental arithmetic; 9 is not a realistic slip"),
    (146, 2, 69): _ok("69 instead of 68 minutes (22 + 25 + 21, with a carry) is a plausible addition slip"),
    (2766, 1, 38): _ok("38 instead of 36 stems (21 + 15) is plausible; 14 left for the vase is feasible"),
    (3567, 1, 113): _ok("113 instead of 112 students (40% of 280) is plausible; still fewer than 280"),
    (489, 1, 94): _ok("$94 instead of $92 (4 x 23) is a plausible multiplication slip; the difference stays positive"),
    (6111, 1, 44): _no("12 x 3.5 = 42 is mental arithmetic; 44 is not a realistic slip"),
    (63, 1, 13): _ok("13 instead of 11 situps short (30 - 19, with a borrow) is a plausible subtraction slip"),
    (1963, 1, 1727): _ok("$1727 instead of $1728 (60% of 2880) is a plausible percentage slip"),
    (3708, 1, 36): _no("4 x 9.5 is mental arithmetic; 36 is not a realistic slip"),
    (6061, 1, 54): _ok("54 instead of 56 marbles (42 + 14) is plausible; 31 yellow out of 85 is feasible"),
    (3092, 1, 1110): _ok("$1110 instead of $1120 (14 x 80) is a tens slip that keeps the trailing 0"),
    (825, 2, 167): _ok("$167 instead of $165 off (20% of 825) is a plausible percentage slip"),
    (6942, 2, 266): _ok("266 instead of 276 legs (69 x 4, with carries) is a tens slip and stays even"),
    (376, 1, 29): _no("12 + 15 has no carry and small values; 29 is not a realistic slip"),
    (5640, 3, 92): _ok("$92 instead of $93 (22 + 45 + 26, with a carry) is a plausible addition slip"),
    (2766, 1, 35): _ok("35 instead of 36 stems (21 + 15) is plausible; 17 left for the vase is feasible"),
    (3567, 1, 122): _ok("122 instead of 112 students (40% of 280) is a tens slip; still fewer than 280"),
    (4234, 1, 14): _ok("14 instead of 15 cats (60% of 25) is plausible; 9 cats after subtracting 5 is feasible"),
    (6111, 1, 40): _no("12 x 3.5 = 42 is mental arithmetic; 40 is not a realistic slip"),
    (3389, 2, 11): _ok("$11 instead of $13 (20% of 65) is a plausible percentage slip; the price stays positive"),
    (3435, 1, 202): _no("1200 / 6 reduces to 12 / 6; 202 is not a realistic slip"),
    (337, 1, 6): _no("22 - 17 is small-number mental arithmetic; 6 is not a realistic slip"),
    (6728, 1, 36): _no("21 + 17 is a carry-free sum of small two-digit numbers; 36 is not a realistic slip"),
    (6777, 2, 330): _no("4 x 85 is mental arithmetic (like 45 x 4); 330 is not a realistic slip"),
    (861, 1, 90): _no("16 x 5 is mental arithmetic (half of 160); 90 is not a realistic slip"),
    (1066, 1, 62): _ok("62 instead of 72 oranges (45 + 27) is the classic dropped-carry slip"),
    (5149, 1, 120): _ok("120 instead of 119 fish sold (four-term sum with carries) is plausible; 197 remain"),
    (7208, 1, 16): _no("three-quarters of 20 is mental arithmetic; 16 is not a realistic slip"),
    # same mental-arithmetic calculation sites offered again with a different delta
    (376, 1, 26): _no("12 + 15 has no carry and small values; not a realistic slip site"),
    (6111, 1, 32): _no("12 x 3.5 = 42 is mental arithmetic; not a realistic slip site"),
    (3435, 1, 201): _no("1200 / 6 reduces to 12 / 6; not a realistic slip site"),
    (6728, 1, 37): _no("21 + 17 is a carry-free sum of small numbers; not a realistic slip site"),
    (6777, 2, 350): _no("4 x 85 is mental arithmetic; not a realistic slip site"),
    (861, 1, 70): _no("16 x 5 is mental arithmetic; not a realistic slip site"),
    (7208, 1, 17): _no("three-quarters of 20 is mental arithmetic; not a realistic slip site"),
    # new items
    (4402, 1, 38): _no("16 x 3 is mental arithmetic; 38 is not a realistic slip"),
    (2622, 1, 40): _no("17 + 21 is a carry-free sum of small numbers; 40 is not a realistic slip"),
    (7365, 1, 8): _ok("$8 instead of $9 (42 - 33, with a borrow) is a plausible subtraction slip"),
    (6928, 1, 2252): _no("3/4 of 3000 is mental arithmetic; 2252 is not a realistic slip"),
    (1295, 1, 6): _no("4 x 1.25 is mental arithmetic (four quarters); 6 is not a realistic slip"),
    (4836, 1, 130): _no("5 x 24 is mental arithmetic; 130 is not a realistic slip"),
    (2752, 1, 4802): _no("24% of 20,000 is mental arithmetic; 4802 is not a realistic slip"),
    (6479, 1, 47): _ok("47 instead of 48 sellable pots (80 - 32, with a borrow) is plausible; still fewer than 80"),
    (2890, 1, 44): _ok("$44 instead of $42 (a third of 126) is a plausible division slip; $82 left is feasible"),
    (3257, 1, 34): _no("66 - 22 has no borrow (digit-wise subtraction); not a realistic slip site"),
    (3257, 1, 43): _no("66 - 22 has no borrow (digit-wise subtraction); not a realistic slip site "
                       "(overrides the Stage 4 pass for consistency with 21 + 17)"),
    (5847, 1, 1370): _no("20 x 68 is doubling then x10 (mental); 1370 is not a realistic slip"),
    (646, 1, 158): _no("4/5 of 200 is mental arithmetic; 158 is not a realistic slip"),
    (4366, 1, 110): _ok("110 instead of 108 songs (36 x 3, with a carry) is a plausible multiplication slip"),
    (2728, 1, 166): _ok("166 instead of 168 cattle (340 - 172, with borrows) is plausible; still fewer than 340"),
    (6887, 1, 47): _ok("47 instead of 45 baby guppies (36 + 9, with a carry) is a plausible addition slip"),
    (3619, 1, 5): _no("25% of 16 is mental arithmetic; 5 is not a realistic slip"),
    (4402, 2, 86): _ok("86 instead of 96 cookies (32 + 16 + 48) is the classic dropped-carry slip"),
    (2764, 2, 96): _ok("96 instead of 106 animals (69 + 37) is the classic dropped-carry slip; still even"),
    (6888, 2, 5): _ok("$5 instead of $6 per pint (324 / 54) is a plausible long-division slip"),
    (3695, 2, 62): _ok("$62 instead of $63 (28 + 35, with a carry) is plausible; still under the $100 paid"),
    (3680, 2, 16): _ok("16 instead of 18 genuine handbags (24 - 6, with a borrow) is plausible"),
    (6730, 2, 110): _ok("110 instead of 108 carrots (3/5 of 180) is plausible; still fewer than 180"),
    (2454, 2, 48): _ok("48 instead of 46 students in shorts (27 + 19, with a carry) is plausible; still fewer than 54"),
    (3303, 3, 50): _no("20% of 245 = 50 lands on a round number and reads as rounding, not a slip "
                       "(overrides the Stage 4 pass; cf. 4 x 9.5 = 40, 4% of 475 = 20)"),
    (2120, 4, 262): _ok("262 instead of 252 pins (85 + 167, with carries) is a tens slip"),
    (6382, 3, 179): _no("3 x 63 has a single carry and is mental arithmetic; 179 is not a realistic slip"),
    (3465, 2, 35): _ok("$35 instead of $33 (25 + 8, with a carry) is plausible; $39 left is feasible"),
    (1274, 3, 11): _ok("11 instead of 9 staff (45 - 36, with a borrow) is plausible; still fewer than 45"),
    (2150, 3, 40): _ok("40 instead of 42 eggs (28 + 14, with a carry) is plausible; 8 uneaten out of 48 is feasible"),
    (3380, 3, 454): _ok("$454 instead of $456 (475 - 19, with a borrow) is a plausible subtraction slip"),
    (6939, 2, 234): _ok("234 instead of 224 episodes (198 + 26, with carries) is a tens slip"),
    (1644, 2, 217): _ok("217 instead of 219 memory cards (10950 / 50) is a plausible division slip"),
    (4492, 2, 46): _ok("$46 instead of $44 (26 + 18, with a carry) is plausible; still under the $100 paid"),
    (2120, 4, 250): _ok("250 instead of 252 pins (85 + 167, with carries) is a plausible addition slip"),
    (4197, 2, 970): _ok("$970 instead of $980 (35 x 28) is a tens slip in a two-digit product"),
    (1544, 2, 188): _ok("$188 instead of $187 (1496 / 8) is a plausible division slip"),
    (5640, 3, 103): _ok("$103 instead of $93 (22 + 45 + 26) is a tens slip in a three-term sum"),
    (6730, 2, 98): _ok("98 instead of 108 carrots (3/5 of 180) is a tens slip; still under 180"),
    (366, 2, 380): _ok("380 instead of 390 feet (15 x 26, with carries) is a tens slip"),
    (1267, 3, 1631): _ok("$1631 instead of $1633 (1850 - 217, with a borrow) is a plausible subtraction slip"),
    (2521, 2, 48): _ok("48 instead of 46 cards for Brenda (55 - 9, with a borrow) is a plausible subtraction slip"),
    (2692, 1, 375): _ok("375 instead of 374 (four-term sum with carries) is plausible; a fifth score of 95 is feasible"),
    (5100, 1, 44): _ok("44 instead of 54 lights (12 + 36 + 6) is the classic dropped-carry slip"),
    (1544, 1, 375): _ok("$375 instead of $374 (a quarter of 1496) is a plausible division slip"),
    (183, 2, 232): _ok("232 instead of 231 birds (33 x 7) is a plausible multiplication slip; still under 580"),
    (6327, 2, 145): _ok("145 instead of 144 employees (48 + 96, with carries) is a plausible addition slip"),
    (7299, 1, 126): _ok("126 instead of 136 leaves (34 x 4, with a carry) is a tens slip; still under 340"),
    (5481, 3, 19): _ok("19 instead of 17 pastries for Frank (25 - 8, with a borrow) is a plausible subtraction slip"),
    (6981, 1, 22): _ok("22 instead of 23 pieces (92 / 4) is a plausible division slip"),
    (2282, 1, 83): _ok("$83 instead of $84 (120 - 36, with a borrow) is a plausible subtraction slip"),
    (2016, 3, 109): _ok("$109 instead of $110 in deductions (84 + 21 + 5) is a plausible addition slip"),
    (2483, 1, 42): _ok("$42 instead of $43 (18 + 25, with a carry) is plausible; still under the $50 gift"),
    (3900, 1, 107): _ok("$107 instead of $105 a day (9450 / 90) is a plausible long-division slip"),
    (6887, 1, 44): _ok("44 instead of 45 baby guppies (36 + 9, with a carry) is a plausible addition slip"),
    (134, 3, 428): _ok("$428 instead of $426 (208 + 218, with a carry) is plausible; still under the $500 paid"),
    (2463, 1, 186): _ok("186 instead of 188 athletes (300 - 112, with borrows) is a plausible subtraction slip"),
    (287, 1, 64): _ok("$64 instead of $63 (90 - 27, with a borrow) is a plausible subtraction slip"),
    (7410, 1, 19): _ok("19 instead of 18 books (24 - 6, with a borrow) is plausible; still fewer than 24"),
    (5198, 2, 42): _ok("42 instead of 44 pairs (36 + 8, with a carry) is a plausible addition slip"),
    (6946, 2, 145): _ok("145 instead of 144 pounds (3/5 of 240) is plausible; still under 240"),
    (3303, 2, 255): _ok("255 instead of 245 pages (350 - 105, with a borrow) is a forgotten-borrow slip; still under 350"),
    (6327, 2, 143): _ok("143 instead of 144 employees (48 + 96, with carries) is a plausible addition slip"),
    (1780, 3, 125): _ok("$125 instead of $135 (27 x 5, with a carry) is a tens slip and keeps the visible multiple of 5"),
    (1267, 3, 1632): _ok("$1632 instead of $1633 (1850 - 217, with a borrow) is a plausible subtraction slip"),
    (5481, 3, 18): _ok("18 instead of 17 pastries for Frank (25 - 8, with a borrow) is a plausible subtraction slip"),
    (134, 3, 424): _ok("$424 instead of $426 (208 + 218, with a carry) is plausible; still under the $500 paid"),
    (7046, 3, 58): _ok("58 instead of 48 gallons (65 - 17) is the classic forgotten-borrow slip; still under 65"),
    (3303, 3, 59): _ok("59 instead of 49 damaged pages (20% of 245) is a tens slip; still under 245"),
    (1274, 3, 8): _ok("8 instead of 9 staff (45 - 36, with a borrow) is a plausible subtraction slip"),
    (6020, 3, 178): _ok("$178 instead of $188 (32 + 36 + 120) is a dropped-carry slip; halving stays an integer"),
    (2016, 3, 112): _ok("$112 instead of $110 in deductions (84 + 21 + 5) is a plausible addition slip"),
    (457, 3, 163): _ok("163 instead of 162 questions (108 + 54, with a carry) is a plausible addition slip"),
    (2150, 3, 43): _ok("43 instead of 42 eggs (28 + 14, with a carry) is plausible; 5 uneaten out of 48 is feasible"),
    (4870, 2, 366): _ok("$366 instead of $364 (28 x 13, with a carry) is a plausible multiplication slip"),
    (3706, 1, 34): _ok("$34 instead of $36 state tax (8% of 450) is a plausible percentage slip; the final amount stays positive"),
    (2976, 1, 15): _ok("15 instead of 16 fences (480 / 30) is a plausible division slip"),
    (4609, 1, 180): _ok("180 instead of 190 (282 - 92, with a borrow) is a plausible tens slip; the two-quiz sum stays below 200"),
    (4932, 3, 128): _ok("128 instead of 130 customers (32 + 40 + 58, with a carry) is a plausible addition slip; halving stays an integer"),
    (4149, 3, 409): _ok("409 instead of 399 spaces (90 + 98 + 110 + 101, with carries) is a plausible carry slip"),
    (3405, 3, 49): _ok("49 instead of 51 hours (15 + 36, with a carry) is a plausible addition slip"),
    (2764, 2, 116): _ok("116 instead of 106 animals (69 + 37, with a carry) is a plausible tens slip; halving stays an integer"),
    (4402, 2, 94): _ok("94 instead of 96 cookies (32 + 16 + 48, with carries) is a plausible addition slip"),
    (4265, 2, 31): _ok("31 instead of 33 coins (17 + 16, with a carry) is a plausible addition slip"),
    (3695, 2, 73): _ok("$73 instead of $63 (28 + 35, with a carry) is a plausible tens slip; change stays positive"),
    (825, 2, 163): _ok("$163 instead of $165 off (20% of 825) is a plausible percentage slip"),
    (183, 2, 233): _ok("233 instead of 231 birds (33 x 7) is a plausible multiplication slip and keeps the product odd"),
    (3380, 3, 455): _ok("$455 instead of $456 (475 - 19, with a borrow) is a plausible subtraction slip"),
    (1644, 2, 218): _ok("218 instead of 219 cards (10950 / 50) is a plausible division slip"),
    (4492, 2, 45): _ok("$45 instead of $44 (26 + 18, with a carry) is a plausible addition slip; change stays positive"),
    (2120, 4, 251): _ok("251 instead of 252 pins (85 + 167, with carries) is a plausible addition slip"),
    (4265, 2, 32): _ok("32 instead of 33 coins (17 + 16, with a carry) is a plausible addition slip"),
    (3695, 2, 64): _ok("$64 instead of $63 (28 + 35, with a carry) is a plausible addition slip"),
    (825, 2, 164): _ok("$164 instead of $165 off (20% of 825) is a plausible percentage slip"),
}


# ------------------------------------------------------------------ pilot-only realism rule
def column_trivial(calc):
    """Additions with no carry in any column and subtractions with no borrow in any column are done
    digit by digit without real calculation. This replaces case-by-case reviewer calls (66 - 22, 21 + 17,
    81 + 41, ...) with one consistent rule. Pilot-only, so Stage 4 output stays reproducible."""
    ops, leaves = calc["ops"], calc["leaves"]
    if any(v.denominator != 1 or v < 0 for v in leaves) or set(ops) - {"+", "-"} or len(set(ops)) != 1:
        return None
    nums = [int(v) for v in leaves]
    width = max(len(str(n)) for n in nums)
    cols = [[(n // 10 ** k) % 10 for n in nums] for k in range(width)]
    if ops[0] == "+" and all(sum(c) < 10 for c in cols):
        return "carry-free addition (digit-by-digit)"
    if ops[0] == "-" and len(nums) == 2 and all(c[0] >= c[1] for c in cols):
        return "borrow-free subtraction (digit-by-digit)"
    return None


def odd_product_parity(calc, wrong):
    """Mirror of Stage 4's 'a product with an even factor must be even': a product of odd integers is
    odd, so an even slip (33*7 = 232) is visible from parity alone. Pilot-only."""
    ops, leaves = calc["ops"], calc["leaves"]
    if set(ops) != {"*"} or any(v.denominator != 1 for v in leaves) or wrong.denominator != 1:
        return None
    if all(int(v) % 2 for v in leaves) and int(wrong) % 2 == 0:
        return "a product of odd factors must be odd"
    return None


# ------------------------------------------------------------------ helpers
def student_text(line):
    """Model-facing step: annotations removed, whitespace normalised. Nothing else is changed."""
    return re.sub(r"[ \t]{2,}", " ", s2.ANNOT_RE.sub("", line)).strip()


def gold_text_flags(rec, g):
    """Automatic gold-text quality flags; any flag excludes the problem."""
    flags = []
    for n, line in enumerate(g["lines"], 1):
        t = student_text(line)
        if re.match(r"^[a-z]", t):
            flags.append(f"step {n} starts in lower case (broken sentence): {t[:40]!r}")
        if len(re.findall(r"[A-Za-z]+", t)) < 3:
            flags.append(f"step {n} is a fragment: {t!r}")
        dup = re.search(r"\b([A-Za-z]+)\s+\1\b", t, re.IGNORECASE)
        if dup:
            flags.append(f"step {n} repeats a word: {dup.group(0)!r}")
    student_view = " ".join([rec["question"]] + [student_text(l) for l in g["lines"]]).lower()
    for term in LEAK_TERMS:
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", student_view):
            flags.append(f"problem text natively contains the label-like term {term!r}")
    return flags


def leak_hits(texts):
    hits = []
    for t in texts:
        low = t.lower()
        for term in LEAK_TERMS:
            if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", low):
                hits.append((term, t[:60]))
    return hits


def op_family(ops):
    fam = set(ops)
    return ops[0] if len(fam) == 1 else "mixed"


def render_steps(steps):
    return "\n".join(f"Step {n}: {t}" for n, t in enumerate(steps, 1))


# ------------------------------------------------------------------ main
def main():
    records = [json.loads(l) for l in open(s2.RAW_TRAIN, encoding="utf-8")]
    semantic = dict(m3.REVIEWER_SEMANTIC_CHECK)
    semantic.update(PILOT_SEMANTIC_CHECK)

    eligible, examined = {}, 0
    for i, rec in enumerate(records):
        examined += 1
        g, _ = s2.build_graph(rec)
        if g is not None:
            eligible[i] = g

    rej = Counter()
    gold_flagged = {}
    options = {}           # problem -> list of automatically valid corruptions
    for i, g in eligible.items():
        flags = gold_text_flags(records[i], g)
        for c in g["calcs"][:-1]:
            for delta in m3.DELTAS:
                cor, why = m3.make_m3(g, c["calc_id"], delta)
                if cor is None:
                    rej["structural: " + why] += 1
                    continue
                calc = g["calcs"][c["calc_id"]]
                dp = m3.elementary_fact(calc) or m3.digit_pattern_problem(calc, cor["corrupted_result"])
                if dp:
                    rej["realism: " + dp.split(" (")[0]] += 1
                    continue
                ct = column_trivial(calc) or odd_product_parity(calc, cor["corrupted_result"])
                if ct:
                    rej["realism (pilot rule): " + ct.split(" (")[0]] += 1
                    continue
                if m3.result_revealed(g, c["calc_id"])[0]:
                    rej["wording reveals result"] += 1
                    continue
                if flags:
                    rej["gold-text flag (automatic)"] += 1
                    gold_flagged[i] = flags
                    continue
                checks = m3.validate_m3(records[i], g, cor)   # expensive: only for survivors
                if not checks["ALL_PASS"]:
                    rej["automatic validation failed"] += 1
                    continue
                options.setdefault(i, []).append((cor, checks))

    # ---------------- seeded selection, one corruption per problem
    rng = random.Random(SEED)
    order = sorted(options)
    rng.shuffle(order)
    direction, magnitude, operator, estep = Counter(), Counter(), Counter(), Counter()
    selected, pending, reviewer_rejected, gold_rejected = [], [], [], []

    # per-problem targets are drawn ONCE from the seed, so reviewer verdicts can only remove options and
    # never reshuffle which corruption a problem is assigned (global counts only enforce caps)
    target = {}
    for pos, i in enumerate(order):
        x = rng.random()
        target[i] = ("over" if pos % 2 == 0 else "under", 1 if x < 0.5 else 2 if x < 0.85 else 10)

    for i in order:
        if len(selected) >= N_ITEMS:
            break
        g = eligible[i]
        gold = GOLD_TEXT_CHECK.get(i)
        if gold is not None and not gold[0]:
            gold_rejected.append((i, gold[1]))
            continue
        want_dir, want_mag = target[i]
        opts = []
        for cor, checks in options[i]:
            d = "over" if cor["delta"] > 0 else "under"
            fam = op_family(g["calcs"][cor["calc_id"]]["ops"])
            step_bucket = min(cor["error_step"], 5)
            mag = abs(cor["delta"])
            if direction[d] >= MAX_PER_DIRECTION or magnitude[mag] >= MAGNITUDE_CAPS[mag]:
                continue
            if operator[fam] >= OPERATOR_CAP or estep[step_bucket] >= ERROR_STEP_CAP:
                continue
            opts.append(((d != want_dir, mag != want_mag, mag, cor["calc_id"]), cor, checks, d, fam, step_bucket, mag))
        opts.sort(key=lambda o: o[0])
        for _, cor, checks, d, fam, sb, mag in opts:
            key = (i, cor["calc_id"], s2.num_out(cor["corrupted_result"]))
            if (i, cor["calc_id"]) in SITE_REJECT:
                reviewer_rejected.append((key, "calculation site: " + SITE_REJECT[(i, cor["calc_id"])]))
                continue
            rev = semantic.get(key)
            if rev is not None and not rev["pass"]:
                reviewer_rejected.append((key, rev["reason"]))
                continue
            if rev is None or gold is None:
                pending.append((i, cor, rev, gold))
                break
            selected.append((i, cor, checks, rev, d))
            direction[d] += 1
            magnitude[mag] += 1
            operator[fam] += 1
            estep[sb] += 1
            break

    stats = {
        "seed": SEED,
        "source_problems_examined": examined,
        "structurally_eligible_problems": len(eligible),
        "problems_with_an_automatically_valid_m3_corruption": len(options),
        "automatic_rejections": dict(sorted(rej.items(), key=lambda x: -x[1])),
        "problems_excluded_gold_text_automatic": len(gold_flagged),
        "problems_excluded_gold_text_reviewer": len(gold_rejected),
        "corruptions_rejected_reviewer_semantic": len(reviewer_rejected),
        "reviewer_semantic_rejections": [{"key": list(k), "reason": r} for k, r in reviewer_rejected],
        "gold_text_rejections_reviewer": [{"source_index": i, "reason": r} for i, r in gold_rejected],
        "pending_reviewer_checks": len(pending),
    }

    if pending:
        print(f"{len(pending)} item(s) need a reviewer check (semantic and/or gold text):\n")
        for i, cor, rev, gold in pending[:N_PENDING_TO_LIST]:
            g = eligible[i]
            print("-" * 78)
            print(f"KEY ({i}, {cor['calc_id']}, {s2.num_out(cor['corrupted_result'])})  step {cor['error_step']}  "
                  f"{g['calcs'][cor['calc_id']]['ann_expr']} = {s2.num_out(cor['correct_result'])} -> "
                  f"{s2.num_out(cor['corrupted_result'])}  [{'semantic done' if rev else 'NEEDS semantic'}; "
                  f"{'gold done' if gold else 'NEEDS gold-text'}]")
            print("Q:", records[i]["question"])
            for n, (gl, cl) in enumerate(zip(g["lines"], cor["corrupted_lines"]), 1):
                print(f"  {n}. {student_text(gl)}" + (f"\n  {n}*{student_text(cl)}" if gl != cl else ""))
            print(f"  final {s2.num_out(g['final'])} -> {s2.num_out(cor['corrupted_final'])}")

    if len(selected) < N_ITEMS:
        stats["status"] = f"INCOMPLETE: {len(selected)} of {N_ITEMS} selected"
        for stale in (OUT_JSON, OUT_REVIEW):
            if stale.exists():
                stale.unlink()
        with open(OUT_STATS, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, default=str)
        print(json.dumps({k: v for k, v in stats.items() if "rejections" not in k or k == "automatic_rejections"},
                         indent=2, default=str))
        return 2

    # ---------------- build records
    items = []
    for n, (i, cor, checks, rev, d) in enumerate(selected, 1):
        g = eligible[i]
        base = m3.build_record(i, records[i], g, cor, checks, m3.result_revealed(g, cor["calc_id"]), rev, f"m3p_{n:02d}")
        r, w = cor["correct_result"], cor["corrupted_result"]
        gold_student = [student_text(l) for l in g["lines"]]
        corr_student = [student_text(l) for l in cor["corrupted_lines"]]
        items.append({
            "id": f"m3p_{n:02d}",
            "source": "gsm8k/train.jsonl", "source_index": i,
            "question": records[i]["question"],
            "gold_steps_student": gold_student,
            "gold_trace_student": render_steps(gold_student),
            "gold_answer": s2.num_out(g["final"]),
            "corrupted_steps_student": corr_student,
            "corrupted_trace_student": render_steps(corr_student),
            "corrupted_answer": s2.num_out(cor["corrupted_final"]),
            "error_type": "M3_calculation_slip",
            "error_step": cor["error_step"], "calc_id": cor["calc_id"], "n_steps": len(g["lines"]),
            "expression": g["calcs"][cor["calc_id"]]["ann_expr"],
            "operator": g["calcs"][cor["calc_id"]]["ops"],
            "operator_family": op_family(g["calcs"][cor["calc_id"]]["ops"]),
            "operands": [s2.num_out(v) for v in g["calcs"][cor["calc_id"]]["leaves"]],
            "correct_result": s2.num_out(r), "corrupted_result": s2.num_out(w),
            "absolute_error": s2.num_out(abs(w - r)),
            "relative_error": round(float(abs(w - r) / abs(r)), 6),
            "error_direction": d,
            "propagated_calc_ids": base["propagated_calc_ids"],
            "dependency_chain": base["dependency_chain"],
            "propagation_explanation": base["propagation_explanation"],
            "semantic_plausibility": base["semantic_plausibility"],
            "result_revealed_by_wording": base["result_revealed_by_wording"],
            "gold_text_check": {"pass": True, "reason": GOLD_TEXT_CHECK[i][1],
                                "automatic_flags": [], "reviewer": "Claude (coding assistant); human review pending"},
            "automatic_validation": checks,
            # internal provenance (never shown to a student model)
            "gold_steps_internal": g["lines"], "corrupted_steps_internal": cor["corrupted_lines"],
            "gold_answer_text_internal": records[i]["answer"], "corrupted_answer_text_internal": cor["corrupted_answer_text"],
            "manual_review": {"approved": None, "notes": ""},
        })

    # ---------------- leakage audit over everything a student would see
    leaks = []
    for it in items:
        texts = [it["question"]] + it["gold_steps_student"] + it["corrupted_steps_student"] + \
                [it["gold_trace_student"], it["corrupted_trace_student"]]
        for term, snippet in leak_hits(texts):
            leaks.append({"id": it["id"], "term": term, "text": snippet})
        if str(it["source_index"]) in " ".join(it["corrupted_steps_student"]) and \
                str(it["source_index"]) not in " ".join(it["gold_steps_student"]):
            leaks.append({"id": it["id"], "term": "source_index", "text": "source index appears only in the corrupted trace"})
        # student traces must differ from internal ones only by annotation removal
        for a, b in zip(it["corrupted_steps_internal"], it["corrupted_steps_student"]):
            if student_text(a) != b:
                leaks.append({"id": it["id"], "term": "non-annotation edit", "text": b[:60]})
    assert not leaks, f"leakage found: {leaks}"

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"seed": SEED, "generator": "scripts/build_m3_pilot40.py",
                   "student_facing_fields": ["question", "gold_steps_student", "gold_trace_student",
                                             "corrupted_steps_student", "corrupted_trace_student"],
                   "items": items}, f, indent=2, ensure_ascii=False)
    with open(OUT_REVIEW, "w", encoding="utf-8") as f:
        f.write("M3 PILOT (40 items): MANUAL REVIEW FILE\n"
                "Error markers appear ONLY in this file. Traces below are exactly the student-facing text.\n\n")
        f.write("\n\n".join(review_block(it) for it in items) + "\n")

    vals = [it["relative_error"] for it in items]
    stats.update({
        "status": "COMPLETE",
        "final_items": len(items),
        "distinct_source_problems": len({it["source_index"] for it in items}),
        "error_direction": dict(Counter(it["error_direction"] for it in items)),
        "operator_family": dict(Counter(it["operator_family"] for it in items)),
        "error_step": dict(sorted(Counter(it["error_step"] for it in items).items())),
        "trace_length_steps": dict(sorted(Counter(it["n_steps"] for it in items).items())),
        "absolute_error": dict(sorted(Counter(it["absolute_error"] for it in items).items())),
        "relative_error_median": round(median(vals), 4),
        "relative_error_min_max": [round(min(vals), 4), round(max(vals), 4)],
        "leakage_violations": len(leaks),
        "all_validation_pass": all(it["automatic_validation"]["ALL_PASS"] for it in items),
    })
    with open(OUT_STATS, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, default=str)
    print(json.dumps(stats, indent=2, default=str))
    return 0


def review_block(it):
    chain = it["dependency_chain"]
    roles = {c["step_id"]: c["role"] for c in chain if c["role"] != "unaffected"}
    k = next(c for c in chain if c["role"] == "INJECTED_M3_ERROR")
    out = ["=" * 78, f"ITEM ID: {it['id']}", f"SOURCE: gsm8k train[{it['source_index']}]", f"QUESTION: {it['question']}", "",
           "GOLD TRACE:"]
    out += [f"  Step {n}: {t}" for n, t in enumerate(it["gold_steps_student"], 1)]
    out += ["", "CORRUPTED TRACE:"]
    for n, t in enumerate(it["corrupted_steps_student"], 1):
        mark = {"INJECTED_M3_ERROR": "   >>> INJECTED ERROR <<<", "propagated": "   [recomputed]"}.get(roles.get(n), "")
        out.append(f"  Step {n}: {t}{mark}")
    out += ["", "INJECTED ERROR:", f"  error step: {it['error_step']}",
            f"  correct calculation:   {k['gold_expression']} = {k['gold_result']}",
            f"  corrupted calculation: {k['corrupted_expression']} = {k['corrupted_claimed_result']}",
            f"  difference: {it['corrupted_result'] - it['correct_result']:+}  (absolute {it['absolute_error']}, "
            f"relative {it['relative_error']:.2%})",
            f"  direction: {it['error_direction']}", "", "DEPENDENCY PROPAGATION:"]
    out += [f"  {x}" for x in it["propagation_explanation"]]
    v = it["automatic_validation"]
    out += ["", f"GOLD ANSWER: {it['gold_answer']}", f"CORRUPTED ANSWER: {it['corrupted_answer']}", "",
            f"SEMANTIC PLAUSIBILITY: {'PASS' if it['semantic_plausibility']['pass'] else 'FAIL'}",
            f"  {it['semantic_plausibility']['reason']}",
            f"GOLD TEXT CHECK: PASS  ({it['gold_text_check']['reason']})", "",
            f"AUTOMATIC VALIDATION: {'PASS' if v['ALL_PASS'] else 'FAIL'} "
            f"({sum(1 for kk, x in v.items() if kk != 'ALL_PASS' and x is True)}/{len(v) - 1} checks)", "",
            "MANUAL REVIEW:", "  [ ] APPROVE", "  [ ] REJECT", "  Notes:", "=" * 78]
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
