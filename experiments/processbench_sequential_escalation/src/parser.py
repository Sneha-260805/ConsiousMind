"""Stage-B answer parsing: a versioned Stage-B NUMERIC parser + the frozen pilot STRICT parser (diagnostic only).

Introduced by protocol amendment A1 (after the real smoke1 test returned NO-GO, before any full Stage-1 run). The
frozen pilot parser ("2.0 (strict + numeric)", information_placebo_pilot/scripts/parse_answers.py) is still imported
read-only after its SHA-256 is verified, but it is no longer the complete Stage-B numeric fallback: its last-line
fallback read "5" from a truncated "### Step 5: Final" heading and "90" from a truncated "\\boxed{90".

NUMERIC PARSER (scoring), frozen hierarchy - the first rule that applies decides; nothing else is searched:
  a. FINAL_ANSWER field. If the response contains a `final_answer:` label (case-insensitive, as in the pilot), the
     LAST label is the terminal answer field: the rest of its line must contain exactly one standalone number (the
     frozen pilot field rule; words/units allowed, >= 2 numbers -> ambiguous, 0 -> non_numeric). A label is present,
     so no fallback is attempted. If the generation was truncated (finish_reason "length") and that field runs to
     the very end of the text (no newline after it), the number may be cut off -> truncated_field (unparseable).
  b. Only if NO label exists: the TERMINAL ANSWER REGION is inspected, and only for \\boxed{...} answers.
       terminal region = the text after the LAST "final answer" phrase (case-insensitive, e.g. "### Final Answer:",
                         "**Final Answer**") if that phrase lies within the last TERMINAL_MAX_LINES non-empty lines;
                         otherwise the last non-empty paragraph (blank-line separated block) of the response.
     Inside it: every \\boxed{ / \\fbox{ must be properly closed (balanced braces) - an unclosed one (e.g. a
     truncated "\\boxed{90") -> incomplete_boxed, never accepted. Each closed box is normalised with the same
     formatting-only normaliser used for the gold answers (src/processbench_adapter.normalise_numeric). Exactly one
     distinct numeric value -> ok (repeating the same value is compatible); two or more distinct values, or a
     numeric box together with a non-numeric box -> ambiguous; only non-numeric boxes -> non_numeric; no box ->
     missing.
  c. The reasoning body outside the terminal region is never searched, and bare (unboxed) numbers are never taken.
  numeric_parse_status: ok | ambiguous | non_numeric | missing | incomplete_boxed | truncated_field
  numeric_parse_source: final_answer_field | terminal_boxed | None

STRICT FORMAT PARSER (diagnostic only): the frozen pilot strict parser, unchanged. strict_format_compliant comes ONLY
from it; a fallback parse (rule b) never makes a response strictly compliant.

Every Stage-B gold answer is an integer (dataset rule E5), so this numeric contract is sufficient.
"""
import re

from . import frozen_sources as fs
from .processbench_adapter import normalise_numeric

STAGE_B_PARSER_VERSION = "stage_b_numeric_v1 (FINAL_ANSWER field > terminal \\boxed{}; pilot strict 2.0 diagnostic)"
TERMINAL_MAX_LINES = 6

FINAL_ANSWER_PHRASE = re.compile(r"final\s+answer", re.IGNORECASE)
BOX_OPEN = re.compile(r"\\(?:boxed|fbox)\s*\{")

_PA = None


def _pa():
    global _PA
    if _PA is None:
        _PA = fs.pilot_module("parse_answers")
    return _PA


def parser_version():
    return STAGE_B_PARSER_VERSION


def pilot_parser_version():
    return _pa().PARSER_VERSION


def boxes(region):
    """-> (closed box contents in order, number of unclosed boxes)."""
    closed, unclosed = [], 0
    for m in BOX_OPEN.finditer(region):
        depth, i = 1, m.end()
        while i < len(region) and depth:
            depth += {"{": 1, "}": -1}.get(region[i], 0)
            i += 1
        if depth == 0:
            closed.append(region[m.end(): i - 1])
        else:
            unclosed += 1
    return closed, unclosed


def terminal_region(text):
    """-> (region text, how it was chosen)."""
    lines = text.splitlines(keepends=True)
    nonempty = [i for i, l in enumerate(lines) if l.strip()]
    if not nonempty:
        return "", "empty"
    window = set(nonempty[-TERMINAL_MAX_LINES:])
    offset = 0
    hit = None
    for i, l in enumerate(lines):
        for m in FINAL_ANSWER_PHRASE.finditer(l):
            if i in window:
                hit = offset + m.end()
        offset += len(l)
    if hit is not None:
        return text[hit:], "after_final_answer_phrase"
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paras[-1], "last_paragraph"


def parse_numeric(text, truncated=False):
    text = text or ""
    pa = _pa()
    r = {"numeric_parse_status": None, "numeric_parsed_answer": None, "numeric_parse_source": None,
         "numeric_ambiguous": False, "numeric_field_raw": None, "numeric_candidates": [],
         "terminal_region_rule": None, "terminal_region_raw": None, "terminal_unclosed_boxes": 0}
    labels = list(pa.LABEL_CI.finditer(text))
    if labels:                                                    # rule a
        last = labels[-1]
        end = text.find("\n", last.end())
        field = text[last.end(): end if end != -1 else len(text)]
        r["numeric_field_raw"] = field
        r["numeric_parse_source"] = "final_answer_field"
        if truncated and end == -1:
            r["numeric_parse_status"] = "truncated_field"
            return r
        nums = pa.numeric_tokens(field)
        r["numeric_candidates"] = [pa.canonical(n) for n in nums]
        if len(nums) == 1:
            r.update(numeric_parse_status="ok", numeric_parsed_answer=pa.canonical(nums[0]))
        elif len(nums) > 1:
            r.update(numeric_parse_status="ambiguous", numeric_ambiguous=True)
        else:
            r["numeric_parse_status"] = "non_numeric"
        return r
    region, how = terminal_region(text)                           # rule b
    r["terminal_region_rule"], r["terminal_region_raw"] = how, region
    closed, unclosed = boxes(region)
    r["terminal_unclosed_boxes"] = unclosed
    if unclosed:
        r.update(numeric_parse_status="incomplete_boxed", numeric_parse_source="terminal_boxed")
        return r
    if not closed:
        r["numeric_parse_status"] = "missing"
        return r
    values = [normalise_numeric(b) for b in closed]
    r["numeric_candidates"] = values
    r["numeric_parse_source"] = "terminal_boxed"
    distinct = set(v for v in values if v is not None)
    if len(distinct) == 1 and None not in values:
        r.update(numeric_parse_status="ok", numeric_parsed_answer=distinct.pop())
    elif distinct:
        r.update(numeric_parse_status="ambiguous", numeric_ambiguous=True)
    else:
        r["numeric_parse_status"] = "non_numeric"
    return r


def parse(text, truncated=False):
    """Full Stage-B parse: numeric (scoring) + pilot strict (diagnostic). `truncated` = finish_reason == "length"."""
    strict = _pa().parse_strict(text)
    return {"parser_version": STAGE_B_PARSER_VERSION, "pilot_strict_parser_version": pilot_parser_version(),
            **strict, **parse_numeric(text, truncated)}
