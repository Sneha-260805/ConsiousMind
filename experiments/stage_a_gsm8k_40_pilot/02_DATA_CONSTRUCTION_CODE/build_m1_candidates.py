"""Stage 2: build exactly 10 strict M1 (wrong arithmetic operation) candidate traces for MANUAL REVIEW.

Pipeline
  1. Strict eligibility filter over data/raw/train.jsonl (read-only).
  2. Explicit calculation graph per example: one node per <<expr=result>> annotation, with every
     operand traced to exactly one source (question / earlier calc / unit constant). Anything
     ambiguous or untraceable rejects the whole example.
  3. Enumerate every (non-final calc, replacement operator) M1 corruption, propagate the corrupted
     value ONLY through graph edges, apply operator-swap safety checks.
  4. Independently re-parse each generated trace from text and validate it.
  5. Pick 10 diverse candidates with a fixed seed.

All arithmetic is exact (fractions.Fraction); no floating point.
No language model is called. No feedback messages are created.

Outputs
  data/pilot/m1_candidates_10.json   10 candidates (no error markers inside the traces)
  outputs/m1_candidates_review.txt   human-readable review file (error step marked here only)
  outputs/m1_candidates_stats.json   filter / rejection / selection statistics

Usage (from the project root):
    PYTHONIOENCODING=utf-8 python scripts/build_m1_candidates.py
"""
import ast
import json
import math
import random
import re
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_TRAIN = ROOT / "data" / "raw" / "train.jsonl"
OUT_JSON = ROOT / "data" / "pilot" / "m1_candidates_10.json"
OUT_REVIEW = ROOT / "outputs" / "m1_candidates_review.txt"
OUT_STATS = ROOT / "outputs" / "m1_candidates_stats.json"

SEED = 42
N_CANDIDATES = 10
N_OBVIOUS_ALLOWED = 2          # include at most this many locally-obvious items so they can be judged
MIN_ERROR_STEP = 2             # error must not sit in step 1
RATIO_BAND = (Fraction(1, 10), Fraction(10))   # corrupted/original for both the error calc and final answer
MAX_DECIMALS_NONINT = 2        # a non-integer value is only allowed where the original was non-integer (money)
DIVERSITY_CAPS = {"swap_pair": 2, "error_step": 3, "n_calcs": 4, "orig_op": 4}

OPS = ["+", "-", "*", "/"]
FINAL_SEP = "\n#### "
ANNOT_RE = re.compile(r"<<([^<>]*)>>")
NUM_PAT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
PROSE_NUM_RE = re.compile(r"(?<![\w.])(?:" + NUM_PAT + r")")
TOKEN_RE = re.compile(r"\s*(?:(?P<num>" + NUM_PAT + r")|(?P<op>[x×*/÷+\-–−])|(?P<paren>[()])|(?P<dollar>\$))")
OP_NORMAL = {"x": "*", "×": "*", "*": "*", "/": "/", "÷": "/", "+": "+", "-": "-", "–": "-", "−": "-"}
VIS_EXPR_CHARS = set("0123456789.,$ ()x×*/÷+-–−\t")
PROSE_ARITH_RE = re.compile(r"(?:" + NUM_PAT + r")\s*[x×*/÷+\-–]\s*\$?(?:" + NUM_PAT + r")[^=\n]{0,3}=")
# applied after ordinals (1st, 2nd, ...) are blanked out
ALGEBRA_RE = re.compile(r"\bLet [A-Za-z] be\b|\b\d+[A-Za-z]\b|\b[A-Za-z]\s*=\s*[\d$(]|\b\d+\s*[A-Za-z]\s*[+\-*/=]")
ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b")

WORD_NUMBERS = {
    "half": [2, Fraction(1, 2)], "twice": [2], "double": [2], "triple": [3], "thrice": [3], "quarter": [4, Fraction(1, 4)],
    "dozen": [12], "one": [1], "two": [2], "three": [3], "four": [4], "five": [5], "six": [6], "seven": [7],
    "eight": [8], "nine": [9], "ten": [10], "eleven": [11], "twelve": [12], "thirteen": [13], "fourteen": [14],
    "fifteen": [15], "sixteen": [16], "seventeen": [17], "eighteen": [18], "nineteen": [19], "twenty": [20],
    "thirty": [30], "forty": [40], "fifty": [50], "hundred": [100], "thousand": [1000], "third": [3, Fraction(1, 3)],
    "fourth": [4], "fifth": [5], "tenth": [10], "percent": [100], "once": [1], "pair": [2],
}
# Unit / world-knowledge constants accepted as an operand source when not in the question.
# Deliberately excludes small numbers like 2 and 3, which are too often silently computed in prose.
IMPLICIT_CONSTANTS = {Fraction(v) for v in (7, 12, 24, 30, 52, 60, 100, 365, 1000)}

# Lexical cues: words that, near an operation, signal which operator the reasoning intends.
STRONG_CUES = {
    # fraction words only in fraction phrases: "the third week" is an ordinal, not a division cue
    "/": ["half", "halve", "halved", "halves", "divide", "divided", "divides", "dividing", "split", "equally",
          "evenly", "average", "a third", "one third", "one-third", "thirds", "a quarter", "one quarter", "quarters",
          "ratio", "share", "shared", "percent", "%"],
    "*": ["twice", "double", "doubled", "doubles", "triple", "tripled", "times", "multiply", "multiplied",
          "dozen", "dozens", "total", "percent", "%"],
    "+": ["total", "altogether", "combined", "together", "sum", "in all", "plus", "add", "added", "adds",
          "additional", "extra", "increase", "increases", "increased"],
    "-": ["remaining", "remain", "remains", "left", "difference", "fewer", "less", "minus", "subtract",
          "subtracted", "spent", "change", "lost", "take away", "gave away"],
}
WEAK_CUES = {"/": ["per", "each", "every", "rate"], "*": ["per", "each", "every"], "+": ["more", "another", "also"],
             "-": ["after", "more than", "fewer than"]}


# =============================================================== exact arithmetic helpers
def frac(tok):
    return Fraction(tok.replace(",", ""))


def decimals_needed(v):
    for k in range(0, 7):
        if (v * 10 ** k).denominator == 1:
            return k
    return None


def fmt(v, template):
    """Format exact value v in the style of an original number token (commas, fixed decimals, leading dot)."""
    k = decimals_needed(v)
    if k is None:
        return None
    td = len(template.split(".")[1]) if "." in template else 0
    d = max(k, td)
    q = abs(v) * 10 ** d
    assert q.denominator == 1
    digits = str(q.numerator).rjust(d + 1, "0")
    int_part, dec_part = (digits[:-d], digits[-d:]) if d else (digits, "")
    if "," in template and len(int_part) > 3:
        int_part = f"{int(int_part):,}"
    s = int_part + ("." + dec_part if d else "")
    if template.startswith(".") and int_part == "0":
        s = s[1:]
    return ("-" if v < 0 else "") + s


def tokenize(expr):
    """Tokenize an arithmetic string. Returns list of (kind, text, start, end) or None if anything is left over."""
    toks, pos = [], 0
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            return None
        kind = m.lastgroup
        toks.append((kind, m.group(kind), m.start(kind), m.end(kind)))
        pos = m.end()
    return toks


def normalized(toks):
    out = []
    for kind, text, _, _ in toks:
        if kind == "num":
            out.append(("num", frac(text)))
        elif kind == "op":
            out.append(("op", OP_NORMAL[text]))
        elif kind == "paren":
            out.append(("paren", text))
    return out


def eval_expr(expr):
    """Strict exact evaluation of an annotation expression. Only binary + - * / and parentheses.
    Returns dict(value, ops, leaves, tokens) or raises ValueError / ZeroDivisionError."""
    toks = tokenize(expr)
    if toks is None:
        raise ValueError("untokenizable")
    if any(k == "dollar" for k, *_ in toks) or any(k == "op" and t not in "+-*/" for k, t, *_ in toks):
        raise ValueError("non-ascii operator or $ in annotation")
    leaves, src, last = [], [], 0
    for kind, text, s, e in toks:
        src.append(expr[last:s])
        if kind == "num":
            if "," in text:
                raise ValueError("comma in annotation number")
            src.append(f"n{len(leaves)}")
            leaves.append(frac(text))
        else:
            src.append(text)
        last = e
    tree = ast.parse("".join(src), mode="eval")
    ops, used = [], []

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Name) and node.id.startswith("n"):
            used.append(int(node.id[1:]))
            return leaves[int(node.id[1:])]
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left = ev(node.left)
            sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}[type(node.op)]
            ops.append(sym)
            right = ev(node.right)
            if sym == "/" and right == 0:
                raise ZeroDivisionError
            return {"+": left + right, "-": left - right, "*": left * right, "/": left / right if right else None}[sym]
        raise ValueError(f"unsupported syntax: {type(node).__name__}")

    value = ev(tree)
    tok_ops = [t for k, t, *_ in toks if k == "op"]
    if used != list(range(len(leaves))) or tok_ops != ops:
        raise ValueError("operator/operand order mismatch (unary operator?)")
    return {"value": value, "ops": ops, "leaves": leaves, "tokens": toks}


def prose_numbers(text):
    text = ORDINAL_RE.sub(" ", text)
    return [frac(m.group()) for m in PROSE_NUM_RE.finditer(text)]


def question_numbers(q):
    nums = set(prose_numbers(q))
    low = q.lower()
    for w, vals in WORD_NUMBERS.items():
        if re.search(r"\b" + re.escape(w) + r"s?\b", low):
            nums.update(Fraction(v) for v in vals)
    for m in re.finditer(r"(" + NUM_PAT + r")\s*%", q):
        nums.add(frac(m.group(1)) / 100)
    return nums


# =============================================================== trace parsing (text -> calc nodes)
class ParseError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def find_visible_expression(line, ann_start):
    """Locate the visible 'a op b =' immediately preceding an annotation. Returns (start, end, tokens) in line coords."""
    prefix = line[:ann_start]
    m = re.search(r"=\s*\$?\s*$", prefix)
    if not m:
        raise ParseError("visible_expression_missing")
    head_end = m.start()
    i = head_end
    while i > 0 and prefix[i - 1] in VIS_EXPR_CHARS:
        i -= 1
    # an 'x' glued to a preceding letter is part of a word ("box"), not a multiplication sign
    while i < head_end and prefix[i] in "xX" and i > 0 and prefix[i - 1].isalpha():
        i += 1
    seg_start = i
    while seg_start < head_end and not (prefix[seg_start].isdigit() or prefix[seg_start] in "$(" or
                                        (prefix[seg_start] == "." and seg_start + 1 < head_end and prefix[seg_start + 1].isdigit())):
        if OP_NORMAL.get(prefix[seg_start]) and not (prefix[seg_start] in "xX" and seg_start > 0 and prefix[seg_start - 1].isalpha()):
            raise ParseError("visible_expression_ambiguous", prefix[i:head_end])
        seg_start += 1
    seg = prefix[seg_start:head_end]
    toks = tokenize(seg)
    if not toks or not any(k == "op" for k, *_ in toks):
        raise ParseError("visible_expression_missing", seg)
    # stray trailing comma or similar would have failed tokenize; shift to line coordinates
    toks = [(k, t, s + seg_start, e + seg_start) for k, t, s, e in toks]
    return seg_start, seg_start + len(seg.rstrip()), toks


def parse_trace(answer_text, strict_arithmetic=True):
    """Parse a GSM8K-format answer into ordered steps and calc nodes. Raises ParseError on anything non-strict.
    strict_arithmetic=False (used only by the M3 validator) records an annotation whose stated result differs
    from its expression instead of raising; every other check is identical."""
    if FINAL_SEP not in answer_text:
        raise ParseError("no_final_answer_separator")
    body, final_raw = answer_text.rsplit(FINAL_SEP, 1)
    if not re.fullmatch(r"-?" + NUM_PAT, final_raw.strip()):
        raise ParseError("final_answer_not_numeric", final_raw)
    final = frac(final_raw.strip())
    lines = body.split("\n")
    if any(not ln.strip() for ln in lines):
        raise ParseError("blank_line_in_solution")
    calcs = []
    for li, line in enumerate(lines):
        anns = list(ANNOT_RE.finditer(line))
        if len(anns) > 1:
            raise ParseError("multiple_calcs_in_one_step", line)
        if not anns:
            continue
        a = anns[0]
        inner = a.group(1)
        if inner.count("=") != 1:
            raise ParseError("annotation_parse_failure", a.group(0))
        expr, res_txt = inner.split("=")
        if not re.fullmatch(NUM_PAT, res_txt.strip()):
            raise ParseError("annotation_parse_failure", a.group(0))
        try:
            ev = eval_expr(expr)
        except ZeroDivisionError:
            raise ParseError("annotation_parse_failure", a.group(0))
        except (ValueError, SyntaxError) as e:
            raise ParseError("annotation_parse_failure", f"{a.group(0)}: {e}")
        stated = frac(res_txt.strip())
        if ev["value"] != stated and strict_arithmetic:
            raise ParseError("annotation_inexact_or_wrong", f"{a.group(0)} evaluates to {ev['value']}")
        vs, ve, vtoks = find_visible_expression(line, a.start())
        if normalized(vtoks) != normalized(ev["tokens"]):
            raise ParseError("visible_expression_mismatch", f"{line[vs:ve]!r} vs {expr!r}")
        rm = re.match(r"\s*\$?\s*(" + NUM_PAT + r")", line[a.end():])
        if not rm:
            raise ParseError("visible_result_missing", line)
        vres_txt = rm.group(1)
        if frac(vres_txt) != stated:
            raise ParseError("visible_result_mismatch", f"{vres_txt} vs {res_txt}")
        vres_start = a.end() + rm.start(1)
        calcs.append({
            "calc_id": len(calcs), "line_index": li, "step": li + 1,
            "ann_span": (a.start(), a.end()), "ann_expr": expr, "ann_expr_offset": a.start() + 2,
            "ann_result_text": res_txt, "ann_result_offset": a.start() + 2 + len(expr) + 1,
            "ops": ev["ops"], "leaves": ev["leaves"], "ann_tokens": ev["tokens"], "value": stated,
            "evaluated": ev["value"],
            "vis_span": (vs, ve), "vis_tokens": vtoks,
            "vis_result_text": vres_txt, "vis_result_span": (vres_start, vres_start + len(vres_txt)),
            "calc_span": (vs, vres_start + len(vres_txt)),
        })
    return {"lines": lines, "final": final, "calcs": calcs}


def free_prose(line, calc):
    """Line text with its single calculation span (visible expr = annotation visible result) removed."""
    if calc is None:
        return line
    s, e = calc["calc_span"]
    return line[:s] + " " + line[e:]


def calc_on_line(parsed, li):
    for c in parsed["calcs"]:
        if c["line_index"] == li:
            return c
    return None


# =============================================================== strict example-level eligibility
def build_graph(rec):
    """Return (graph, None) if the example passes the strict filter, else (None, reasons)."""
    q, a = rec["question"], rec["answer"]
    reasons = []
    try:
        p = parse_trace(a)
    except ParseError as e:
        return None, [e.reason]
    calcs, lines = p["calcs"], p["lines"]
    if FINAL_SEP.join(["\n".join(lines), fmt(p["final"], a.rsplit(FINAL_SEP, 1)[1].strip())]) != a:
        reasons.append("layout_roundtrip_failure")
    if len(calcs) < 2:
        reasons.append("fewer_than_2_calcs")
    if not calcs or calcs[-1]["value"] != p["final"] or calcs[-1]["line_index"] != len(lines) - 1:
        reasons.append("final_answer_not_last_calc")
    if ALGEBRA_RE.search(ORDINAL_RE.sub(" ", "\n".join(ANNOT_RE.sub(" ", ln) for ln in lines))):
        reasons.append("algebraic_variables")
    # formatter must reproduce every original number token exactly (guards the rewrite step)
    for c in calcs:
        for kind, text, *_ in c["ann_tokens"] + c["vis_tokens"]:
            if kind == "num" and fmt(frac(text), text) != text:
                reasons.append("number_format_roundtrip_failure")
        for t in (c["ann_result_text"].strip(), c["vis_result_text"]):
            if fmt(frac(t), t) != t:
                reasons.append("number_format_roundtrip_failure")

    q_nums = question_numbers(q)
    results = {c["value"] for c in calcs}
    known_prose = q_nums | results | IMPLICIT_CONSTANTS
    for li, line in enumerate(lines):
        fp = free_prose(line, calc_on_line(p, li))
        if PROSE_ARITH_RE.search(fp):
            reasons.append("unannotated_arithmetic_in_prose")
        if any(x not in known_prose for x in prose_numbers(fp)):
            reasons.append("unannotated_value_in_prose")

    # operand provenance -> explicit dependency edges
    for c in calcs:
        c["operand_sources"] = []
        for v in c["leaves"]:
            calc_srcs = [d["calc_id"] for d in calcs[:c["calc_id"]] if d["value"] == v]
            ext = "question" if v in q_nums else ("implicit_constant" if v in IMPLICIT_CONSTANTS else None)
            if len(calc_srcs) > 1 or (calc_srcs and ext):
                reasons.append("ambiguous_dependency")
                c["operand_sources"].append("AMBIGUOUS")
            elif calc_srcs:
                c["operand_sources"].append(calc_srcs[0])
            elif ext:
                c["operand_sources"].append(ext)
            else:
                reasons.append("operand_untraceable")
                c["operand_sources"].append("UNTRACEABLE")
        c["depends_on"] = sorted({s for s in c["operand_sources"] if isinstance(s, int)})
    if reasons:
        return None, list(dict.fromkeys(reasons))
    p["question"], p["q_nums"], p["answer_text"] = q, q_nums, a
    return p, None


# =============================================================== corruption + propagation
def descendants(calcs, k):
    affected = {k}
    for c in calcs[k + 1:]:
        if any(d in affected for d in c["depends_on"]):
            affected.add(c["calc_id"])
    return affected


def replace_spans(text, repl):
    for (s, e), new in sorted(repl, key=lambda r: r[0][0], reverse=True):
        text = text[:s] + new + text[e:]
    return text


def make_corruption(g, k, new_op):
    """Return (record, None) or (None, rejection_reason)."""
    calcs = g["calcs"]
    c = calcs[k]
    if len(c["ops"]) != 1:
        return None, "target_calc_has_multiple_operators"
    if c["step"] < MIN_ERROR_STEP:
        return None, "error_step_is_step_1"
    a, b = c["leaves"]
    if new_op == "/" and b == 0:
        return None, "division_by_zero"
    new_val = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else None}[new_op]
    if new_val == c["value"]:
        return None, "unchanged_result"

    affected = descendants(calcs, k)
    def value_problem(cid, v):
        old = calcs[cid]["value"]
        if v < 0:
            return "negative_value"
        if v == 0:
            return "zero_value"
        if old.denominator == 1 and v.denominator != 1:
            return "non_integer_value_where_integer_expected"
        if decimals_needed(v) is None or decimals_needed(v) > MAX_DECIMALS_NONINT:
            return "non_terminating_or_too_many_decimals"
        return None

    # every value is checked as soon as it is computed, before it is substituted downstream
    new_vals = {k: new_val}
    if value_problem(k, new_val):
        return None, value_problem(k, new_val)
    for d in calcs[k + 1:]:
        if d["calc_id"] not in affected:
            continue
        leaves = [new_vals[s] if isinstance(s, int) and s in new_vals else v for v, s in zip(d["leaves"], d["operand_sources"])]
        expr = _expr_with_leaves(d, leaves, use_visible=False)
        try:
            v = eval_expr(expr)["value"]
        except ZeroDivisionError:
            return None, "downstream_division_by_zero"
        if value_problem(d["calc_id"], v):
            return None, value_problem(d["calc_id"], v)
        new_vals[d["calc_id"]] = v
    new_final = new_vals[calcs[-1]["calc_id"]] if calcs[-1]["calc_id"] in new_vals else g["final"]
    if new_final == g["final"]:
        return None, "final_equals_gold"
    if new_final.denominator != 1:
        return None, "non_integer_final_answer"
    lo, hi = RATIO_BAND
    if c["value"] <= 0 or not (lo <= new_val / c["value"] <= hi):
        return None, "implausible_magnitude_error_step"
    if g["final"] <= 0 or not (lo <= new_final / g["final"] <= hi):
        return None, "implausible_magnitude_final"

    # a value that will change must not be restated in free prose anywhere (we would not know whether
    # that mention refers to it), and the new values must not collide with any other number in the trace
    prose_nums, other_nums = [], set(g["q_nums"]) | IMPLICIT_CONSTANTS
    for li, line in enumerate(g["lines"]):
        prose_nums += prose_numbers(free_prose(line, calc_on_line(g, li)))
    for cid in affected:
        if calcs[cid]["value"] in prose_nums:
            return None, "affected_value_restated_in_prose"
    for d in calcs:
        if d["calc_id"] not in affected:
            other_nums.add(d["value"])
        other_nums.update(lv for lv, s in zip(d["leaves"], d["operand_sources"]) if not isinstance(s, int))
    other_nums.update(prose_nums)
    nv = list(new_vals.values())
    if any(v in other_nums for v in nv) or len(set(nv)) != len(nv):
        return None, "corrupted_value_collides_with_existing_number"

    # ---- rewrite text: only calculation spans of affected calcs
    vis_mul = next((t for c2 in calcs for kd, t, *_ in c2["vis_tokens"] if kd == "op" and OP_NORMAL[t] == "*"), "*")
    vis_div = next((t for c2 in calcs for kd, t, *_ in c2["vis_tokens"] if kd == "op" and OP_NORMAL[t] == "/"), "/")
    vis_sym = {"+": "+", "-": "-", "*": vis_mul if vis_mul in "x×*" else "*", "/": vis_div}
    new_lines = list(g["lines"])
    for cid in sorted(affected):
        d = calcs[cid]
        line = new_lines[d["line_index"]]
        repl = []
        if cid == k:
            # swap the operator in the annotation and in the visible expression; operands unchanged
            ann_op = next(t for t in d["ann_tokens"] if t[0] == "op")
            vis_op = next(t for t in d["vis_tokens"] if t[0] == "op")
            off = d["ann_expr_offset"]
            repl.append(((off + ann_op[2], off + ann_op[3]), new_op))
            repl.append(((vis_op[2], vis_op[3]), vis_sym[new_op]))
        else:
            off = d["ann_expr_offset"]
            ann_nums = [t for t in d["ann_tokens"] if t[0] == "num"]
            vis_nums = [t for t in d["vis_tokens"] if t[0] == "num"]
            for i, s in enumerate(d["operand_sources"]):
                if isinstance(s, int) and s in new_vals:
                    repl.append(((off + ann_nums[i][2], off + ann_nums[i][3]), fmt(new_vals[s], ann_nums[i][1])))
                    repl.append(((vis_nums[i][2], vis_nums[i][3]), fmt(new_vals[s], vis_nums[i][1])))
        rs = d["ann_result_offset"]
        repl.append(((rs, rs + len(d["ann_result_text"])), _keep_ws(d["ann_result_text"], fmt(new_vals[cid], d["ann_result_text"].strip()))))
        repl.append((d["vis_result_span"], fmt(new_vals[cid], d["vis_result_text"])))
        new_lines[d["line_index"]] = replace_spans(line, repl)
    final_tmpl = g["answer_text"].rsplit(FINAL_SEP, 1)[1].strip()
    corrupted_text = "\n".join(new_lines) + FINAL_SEP + fmt(new_final, final_tmpl)
    return {
        "calc_id": k, "error_step": c["step"], "original_operator": c["ops"][0], "replacement_operator": new_op,
        "original_result": c["value"], "corrupted_result": new_val, "affected": sorted(affected),
        "new_values": new_vals, "corrupted_final": new_final, "corrupted_answer_text": corrupted_text,
        "corrupted_lines": new_lines,
    }, None


def _keep_ws(orig, new):
    lead = orig[:len(orig) - len(orig.lstrip())]
    trail = orig[len(orig.rstrip()):]
    return lead + new + trail


def _expr_with_leaves(d, leaves, use_visible):
    toks = d["vis_tokens"] if use_visible else d["ann_tokens"]
    expr = d["ann_expr"]
    nums = [t for t in toks if t[0] == "num"]
    repl = [((t[2], t[3]), fmt(v, t[1])) for t, v in zip(nums, leaves)]
    return replace_spans(expr, repl)


# =============================================================== obviousness
def cue_hits(text, cues):
    low = text.lower()
    return [w for w in cues if (w in low if w == "%" else re.search(r"\b" + re.escape(w) + r"\b", low))]


def obviousness(g, k, orig, new):
    c = g["calcs"][k]
    step_text = ANNOT_RE.sub("", g["lines"][c["line_index"]])
    strong = [w for w in cue_hits(step_text, STRONG_CUES[orig]) if w not in STRONG_CUES[new]]
    weak = cue_hits(step_text, WEAK_CUES[orig])
    q_strong = [w for w in cue_hits(g["question"], STRONG_CUES[orig]) if w not in STRONG_CUES[new]]
    if strong:
        reason = (f"error step wording contains {strong} which signals '{orig}' and contradicts the swapped-in '{new}'")
    else:
        reason = f"no strong '{orig}' cue in the error step wording"
        if weak:
            reason += f"; weak cue(s) present: {weak}"
    if q_strong:
        reason += f" (question itself contains '{orig}' cue(s): {q_strong})"
    return bool(strong), reason


# =============================================================== independent validation
def validate(rec, g, cor):
    """Re-parse gold and corrupted texts from scratch and check every requirement. Returns dict of checks."""
    chk = {}
    try:
        G = parse_trace(rec["answer"])
        chk["A_gold_parses"] = True
    except ParseError as e:
        return {"A_gold_parses": False, "detail": e.reason, "ALL_PASS": False}
    chk["A_gold_all_annotations_correct"] = True  # parse_trace raises on any inexact annotation
    chk["A_gold_final_equals_last_calc"] = G["calcs"][-1]["value"] == G["final"]
    chk["A_gold_final_equals_dataset_answer"] = G["final"] == frac(rec["answer"].rsplit(FINAL_SEP, 1)[1].strip())
    try:
        C = parse_trace(cor["corrupted_answer_text"])
        chk["B_corrupted_parses"] = True
    except ParseError as e:
        chk.update(B_corrupted_parses=False, detail=f"{e.reason}: {e.detail}", ALL_PASS=False)
        return chk
    k, aff = cor["calc_id"], set(cor["affected"])
    gl, cl = G["lines"], C["lines"]
    chk["B_same_step_and_calc_layout"] = (len(gl) == len(cl) and len(G["calcs"]) == len(C["calcs"]) and
                                         all(x["line_index"] == y["line_index"] for x, y in zip(G["calcs"], C["calcs"])))
    if not chk["B_same_step_and_calc_layout"]:
        chk["ALL_PASS"] = False
        return chk
    e_li = G["calcs"][k]["line_index"]
    chk["B_steps_before_error_unchanged"] = gl[:e_li] == cl[:e_li]
    gk, ck = G["calcs"][k], C["calcs"][k]
    chk["B_error_step_operator_swapped_as_intended"] = (gk["ops"] == [cor["original_operator"]] and
                                                         ck["ops"] == [cor["replacement_operator"]])
    chk["B_error_step_operands_unchanged"] = gk["leaves"] == ck["leaves"]
    chk["B_error_step_result_matches_record"] = ck["value"] == cor["corrupted_result"] != gk["value"]
    ok_down, ok_unrel, n_op_changes = True, True, 0
    for gc, cc in zip(G["calcs"], C["calcs"]):
        if gc["ops"] != cc["ops"]:
            n_op_changes += 1
        if gc["calc_id"] == k:
            continue
        if gc["calc_id"] in aff:
            # same operators, each operand either unchanged or equal to its (corrupted) upstream value
            if gc["ops"] != cc["ops"]:
                ok_down = False
            for i, s in enumerate(g["calcs"][gc["calc_id"]]["operand_sources"]):
                want = C["calcs"][s]["value"] if isinstance(s, int) and s in aff else gc["leaves"][i]
                if cc["leaves"][i] != want:
                    ok_down = False
        else:
            if gl[gc["line_index"]] != cl[cc["line_index"]] or gc["value"] != cc["value"]:
                ok_unrel = False
    chk["B_downstream_dependents_correct_given_corruption"] = ok_down
    chk["B_unrelated_calcs_unchanged"] = ok_unrel
    chk["B_all_corrupted_annotations_arithmetically_consistent"] = True  # parse_trace enforces exactness
    chk["B_exactly_one_operator_change"] = n_op_changes == 1
    chk["B_corrupted_final_equals_last_calc"] = C["calcs"][-1]["value"] == C["final"]
    chk["B_corrupted_final_differs_from_gold"] = C["final"] != G["final"]
    chk["B_prose_outside_calc_spans_unchanged"] = all(
        free_prose(gl[i], calc_on_line(G, i)) == free_prose(cl[i], calc_on_line(C, i)) for i in range(len(gl)))
    chk["B_lines_changed_only_where_graph_says"] = all(
        (gl[i] != cl[i]) == any(c["line_index"] == i for c in G["calcs"] if c["calc_id"] in aff) for i in range(len(gl)))
    chk["C_visible_expression_matches_annotation"] = True   # parse_trace raises otherwise
    chk["C_visible_result_matches_annotation"] = True       # parse_trace raises otherwise
    chk["D_no_review_markers_in_trace"] = ">>>" not in cor["corrupted_answer_text"] and "INJECTED" not in cor["corrupted_answer_text"]
    chk["ALL_PASS"] = all(v for kk, v in chk.items() if kk != "detail")
    return chk


# =============================================================== output helpers
def num_out(v):
    if v is None:
        return None
    return int(v) if v.denominator == 1 else float(v)


def clean(line):
    return ANNOT_RE.sub("", line)


def build_record(rec_idx, rec, g, cor, obvious, reason, checks):
    calcs = g["calcs"]
    k = cor["calc_id"]
    C = parse_trace(cor["corrupted_answer_text"])
    chain = []
    for c, cc in zip(calcs, C["calcs"]):
        chain.append({
            "calc_id": c["calc_id"], "step_id": c["step"], "operators": c["ops"],
            "operands": [num_out(v) for v in c["leaves"]],
            "operand_sources": [f"calc_{s}" if isinstance(s, int) else s for s in c["operand_sources"]],
            "dependencies_on_previous_calc_ids": c["depends_on"],
            "gold_expression": c["ann_expr"], "gold_result": num_out(c["value"]),
            "role": "INJECTED_ERROR" if c["calc_id"] == k else ("propagated" if c["calc_id"] in cor["affected"] else "unaffected"),
            "corrupted_expression": cc["ann_expr"], "corrupted_result": num_out(cc["value"]),
        })
    gk = calcs[k]
    corrupted_expr = C["calcs"][k]["ann_expr"]
    return {
        "candidate_id": None,
        "source": "gsm8k/train.jsonl", "source_index": rec_idx,
        "question": rec["question"],
        "gold_steps": [clean(l) for l in g["lines"]],
        "gold_steps_annotated": g["lines"],
        "gold_answer": num_out(g["final"]),
        "gold_answer_text": rec["answer"],
        "corrupted_steps": [clean(l) for l in cor["corrupted_lines"]],
        "corrupted_steps_annotated": cor["corrupted_lines"],
        "corrupted_answer": num_out(cor["corrupted_final"]),
        "corrupted_answer_text": cor["corrupted_answer_text"],
        "n_steps": len(g["lines"]), "n_calcs": len(calcs),
        "error_type": "M1_wrong_operation",
        "error_step": cor["error_step"], "calc_id": k,
        "original_expression": gk["ann_expr"], "corrupted_expression": corrupted_expr,
        "original_operator": cor["original_operator"], "replacement_operator": cor["replacement_operator"],
        "original_result": num_out(cor["original_result"]), "corrupted_result": num_out(cor["corrupted_result"]),
        "propagated_calc_ids": [c for c in cor["affected"] if c != k],
        "dependency_chain": chain,
        "obvious_from_local_wording": obvious, "obviousness_reason": reason,
        "automatic_validation": checks,
        "manual_review": {"approved": None, "notes": ""},
    }


def review_block(i, r):
    out = ["=" * 78, f"CANDIDATE {i}", f"SOURCE INDEX: train[{r['source_index']}]", f"QUESTION: {r['question']}", "",
           "GOLD TRACE:"]
    for s, t in enumerate(r["gold_steps"], 1):
        out.append(f"  Step {s}: {t}")
    out += ["", "CORRUPTED TRACE:"]
    changed = {c["step_id"]: c["role"] for c in r["dependency_chain"] if c["role"] != "unaffected"}
    for s, t in enumerate(r["corrupted_steps"], 1):
        mark = ""
        if changed.get(s) == "INJECTED_ERROR":
            mark = "   >>> INJECTED ERROR HERE <<<"
        elif changed.get(s) == "propagated":
            mark = "   [recomputed from corrupted value]"
        out.append(f"  Step {s}: {t}{mark}")
    out += ["", "INJECTED ERROR:", f"  Step: {r['error_step']}  (calc_id {r['calc_id']})",
            f"  Original:  {r['original_expression']} = {r['original_result']}",
            f"  Corrupted: {r['corrupted_expression']} = {r['corrupted_result']}",
            f"  Operator swap: {r['original_operator']} -> {r['replacement_operator']}", "",
            "DEPENDENCY PROPAGATION:"]
    for c in r["dependency_chain"]:
        deps = ", ".join(f"{o}<-{s}" for o, s in zip(c["operands"], c["operand_sources"]))
        if c["role"] == "INJECTED_ERROR":
            out.append(f"  calc_{c['calc_id']} (step {c['step_id']}): INJECTED  {c['gold_expression']}={c['gold_result']}  ->  "
                       f"{c['corrupted_expression']}={c['corrupted_result']}")
        elif c["role"] == "propagated":
            ups = [f"calc_{d}" for d in c["dependencies_on_previous_calc_ids"]]
            out.append(f"  calc_{c['calc_id']} (step {c['step_id']}): RECOMPUTED  {c['gold_expression']}={c['gold_result']}  ->  "
                       f"{c['corrupted_expression']}={c['corrupted_result']}   because it uses {', '.join(ups)} [{deps}]")
        else:
            out.append(f"  calc_{c['calc_id']} (step {c['step_id']}): unchanged   {c['gold_expression']}={c['gold_result']}   [{deps}]")
    v = r["automatic_validation"]
    out += ["", f"GOLD ANSWER: {r['gold_answer']}", f"CORRUPTED ANSWER: {r['corrupted_answer']}", "",
            "OBVIOUS FROM WORDING?", f"  {'yes' if r['obvious_from_local_wording'] else 'no'}",
            f"  Reason: {r['obviousness_reason']}", "",
            f"AUTOMATIC VALIDATION: {'PASS' if v['ALL_PASS'] else 'FAIL'}"]
    for kk, val in v.items():
        if kk != "ALL_PASS":
            out.append(f"  [{'PASS' if val is True else 'FAIL' if val is False else val}] {kk}")
    out.append("=" * 78)
    return "\n".join(out)


# =============================================================== main
def main():
    records = [json.loads(l) for l in open(RAW_TRAIN, encoding="utf-8")]
    ex_first_reason, ex_any_reason = Counter(), Counter()
    eligible = {}
    for i, rec in enumerate(records):
        g, reasons = build_graph(rec)
        if g is None:
            ex_first_reason[reasons[0]] += 1
            for r in reasons:
                ex_any_reason[r] += 1
        else:
            eligible[i] = g

    considered, cor_reject = 0, Counter()
    pool = {}  # idx -> list of valid corruptions
    for i, g in eligible.items():
        for c in g["calcs"][:-1]:
            # every non-final calc offers 3 replacement-operator slots (multi-operator calcs are counted
            # with 3 slots too, and all 3 are rejected because the swap target would be ambiguous)
            slots = [o for o in OPS if o != c["ops"][0]] if len(c["ops"]) == 1 else OPS[:3]
            for new_op in slots:
                considered += 1
                cor, why = make_corruption(g, c["calc_id"], new_op)
                if cor is None:
                    cor_reject[why] += 1
                    continue
                checks = validate(records[i], g, cor)
                if not checks["ALL_PASS"]:
                    cor_reject["automatic_validation_failed"] += 1
                    continue
                obvious, reason = obviousness(g, c["calc_id"], cor["original_operator"], new_op)
                pool.setdefault(i, []).append((cor, checks, obvious, reason))
    n_valid = sum(len(v) for v in pool.values())

    # ---------------- diverse selection with fixed seed
    rng = random.Random(SEED)
    order = sorted(pool)
    rng.shuffle(order)
    counts = {key: Counter() for key in DIVERSITY_CAPS}
    selected = []

    def keyvals(i, cor):
        return {"swap_pair": f"{cor['original_operator']}->{cor['replacement_operator']}", "error_step": cor["error_step"],
                "n_calcs": len(eligible[i]["calcs"]), "orig_op": cor["original_operator"]}

    def try_fill(want_obvious, limit):
        for i in order:
            if len(selected) >= limit:
                return
            if any(s[0] == i for s in selected):
                continue
            opts = [o for o in pool[i] if o[2] == want_obvious]
            # prefer the most numerically modest corruption (smallest |log ratio|) for this item
            opts.sort(key=lambda o: abs(math.log(o[0]["corrupted_result"] / o[0]["original_result"])))
            for cor, checks, obv, reason in opts:
                kv = keyvals(i, cor)
                if all(counts[key][kv[key]] < DIVERSITY_CAPS[key] for key in DIVERSITY_CAPS):
                    selected.append((i, cor, checks, obv, reason))
                    for key in DIVERSITY_CAPS:
                        counts[key][kv[key]] += 1
                    break

    try_fill(False, N_CANDIDATES - N_OBVIOUS_ALLOWED)
    try_fill(True, N_CANDIDATES)
    try_fill(False, N_CANDIDATES)  # top up with non-obvious if too few obvious ones fit
    assert len(selected) == N_CANDIDATES, f"only {len(selected)} candidates fit the diversity caps"

    out = []
    for n, (i, cor, checks, obv, reason) in enumerate(selected, 1):
        r = build_record(i, records[i], eligible[i], cor, obv, reason, checks)
        r["candidate_id"] = f"m1_c{n:02d}"
        out.append(r)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"seed": SEED, "generator": "scripts/build_m1_candidates.py", "candidates": out}, f, indent=2, ensure_ascii=False)
    with open(OUT_REVIEW, "w", encoding="utf-8") as f:
        f.write("M1 CANDIDATES FOR MANUAL REVIEW  (markers below exist ONLY in this review file)\n\n")
        f.write("\n\n".join(review_block(n, r) for n, r in enumerate(out, 1)) + "\n")

    pool_obvious = sum(o[2] for v in pool.values() for o in v)
    stats = {
        "seed": SEED,
        "train_examples": len(records),
        "eligible_examples_strict_filter": len(eligible),
        "example_rejections_first_reason": dict(ex_first_reason.most_common()),
        "example_rejections_any_reason_nonexclusive": dict(ex_any_reason.most_common()),
        "m1_corruptions_considered": considered,
        "m1_corruptions_rejected": sum(cor_reject.values()),
        "m1_corruptions_valid": n_valid,
        "m1_corruption_rejection_reasons": dict(cor_reject.most_common()),
        "examples_with_at_least_one_valid_corruption": len(pool),
        "valid_corruptions_flagged_obvious": pool_obvious,
        "valid_corruptions_not_obvious": n_valid - pool_obvious,
        "valid_pool_swap_distribution": dict(Counter(f"{o[0]['original_operator']}->{o[0]['replacement_operator']}" for v in pool.values() for o in v).most_common()),
        "selected": {
            "source_indices": [r["source_index"] for r in out],
            "error_step_distribution": dict(sorted(Counter(r["error_step"] for r in out).items())),
            "error_position_distribution": dict(Counter(f"calc {r['calc_id'] + 1} of {r['n_calcs']}" for r in out).most_common()),
            "swap_distribution": dict(Counter(f"{r['original_operator']}->{r['replacement_operator']}" for r in out).most_common()),
            "n_steps_distribution": dict(sorted(Counter(r["n_steps"] for r in out).items())),
            "n_calcs_distribution": dict(sorted(Counter(r["n_calcs"] for r in out).items())),
            "flagged_obvious": sum(r["obvious_from_local_wording"] for r in out),
            "all_validation_pass": all(r["automatic_validation"]["ALL_PASS"] for r in out),
        },
    }
    with open(OUT_STATS, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, default=str)

    print(open(OUT_REVIEW, encoding="utf-8").read())
    print(json.dumps(stats, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
