"""Parse model responses with TWO predefined parsers. Neither guesses semantically, and neither ever
triggers a re-prompt: a parse failure is an experimental outcome.

1. STRICT FORMAT PARSER (diagnostic: did the model follow the requested response format?)
   Unchanged from the first harness version.
   * Label exactly `FINAL_ANSWER:` (case-sensitive); the LAST occurrence is used.
   * Value = rest of that line, formatting-only normalisation: whitespace, quotes, markdown emphasis or
     backticks, one trailing period, a leading `$`, standard thousands separators, trailing zero
     decimals (1920.0 -> 1920). Anything else (units, words, expressions, value on the next line) ->
     non_numeric. No label -> missing.
   * strict_format_compliant = strict_parse_status == "ok" AND the field is on the last non-empty line
     (the prompt asks for the answer "on the last line exactly as FINAL_ANSWER: <answer>").

2. NUMERIC ANSWER PARSER (used for answer correctness / repair)
   * First preference: the LAST `final_answer:` label, case-insensitive (FINAL_ANSWER, Final_Answer,
     final_answer ...). Its field is the rest of that line. The field must contain EXACTLY ONE
     standalone number (with optional $, thousands separators, decimals). Words and units around it
     are allowed ("1920 dollars"). Two or more numbers -> ambiguous ("1920 or 1880", "40*48 = 1920").
     Zero numbers -> non_numeric. A label is present, so no fallback is attempted.
   * Only if NO label exists: the final non-empty line of the response is inspected, and accepted only
     if it contains exactly one standalone number (source = final_line_fallback). The rest of the
     reasoning is never searched.
   * numeric_parse_status: ok | ambiguous | non_numeric | missing

Usage: python scripts/parse_answers.py --run-dir outputs/runs/<run_id>
"""
import argparse
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

PARSER_VERSION = "2.0 (strict + numeric)"

# ------------------------------------------------------------------ strict parser (unchanged rules)
LABEL = re.compile(r"FINAL_ANSWER\s*:")
NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")
THOUSANDS = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
WRAPPERS = "*_`\"'“”‘’ \t"


def canonical(dec):
    """Decimal -> canonical string: integers without a decimal point, others without trailing zeros."""
    if dec == dec.to_integral_value():
        return str(int(dec))
    return format(dec.normalize(), "f")


def normalise_value(raw):
    v = raw.strip().strip(WRAPPERS)
    if v.endswith("."):
        v = v[:-1].rstrip().strip(WRAPPERS)
    neg = v.startswith("-")
    body = v[1:] if neg else v
    if body.startswith("$"):
        body = body[1:].strip()
    v = ("-" if neg else "") + body
    if "," in v:
        if not THOUSANDS.fullmatch(v):
            return None
        v = v.replace(",", "")
    if not NUMBER.fullmatch(v):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def last_nonempty_line(text):
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def parse_strict(text):
    text = text or ""
    matches = list(LABEL.finditer(text))
    r = {"strict_parse_status": None, "strict_parsed_answer": None, "strict_final_answer_raw": None,
         "n_final_answer_fields": len(matches), "multiple_final_answer_fields": len(matches) > 1,
         "final_answer_on_last_line": False, "strict_format_compliant": False}
    if not matches:
        r["strict_parse_status"] = "missing"
        return r
    last = matches[-1]
    line_end = text.find("\n", last.end())
    raw_value = text[last.end(): line_end if line_end != -1 else len(text)]
    r["strict_final_answer_raw"] = raw_value
    r["final_answer_on_last_line"] = text[last.end():].strip() == raw_value.strip()
    dec = normalise_value(raw_value)
    if dec is None:
        r["strict_parse_status"] = "non_numeric"
    else:
        r["strict_parse_status"] = "ok"
        r["strict_parsed_answer"] = canonical(dec)
    r["strict_format_compliant"] = r["strict_parse_status"] == "ok" and r["final_answer_on_last_line"]
    return r


# ------------------------------------------------------------------ numeric parser
LABEL_CI = re.compile(r"final_answer\s*:", re.IGNORECASE)
# a standalone number: optional sign, optional $, standard thousands groups or plain digits, optional
# decimals; not glued to letters/digits/decimal points on either side (so "3rd", "x2", "v1.5" are not numbers)
NUM_TOKEN = re.compile(
    r"(?<![A-Za-z0-9.,])(-?)\$?\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?![A-Za-z0-9]|[.,]\d)")


def numeric_tokens(s):
    out = []
    for m in NUM_TOKEN.finditer(s):
        try:
            out.append(Decimal(m.group(1) + m.group(2).replace(",", "")))
        except InvalidOperation:
            pass
    return out


def parse_numeric(text):
    text = text or ""
    r = {"numeric_parse_status": None, "numeric_parsed_answer": None, "numeric_parse_source": None,
         "numeric_ambiguous": False, "numeric_field_raw": None, "numeric_candidates": []}
    labels = list(LABEL_CI.finditer(text))
    if labels:
        last = labels[-1]
        end = text.find("\n", last.end())
        field = text[last.end(): end if end != -1 else len(text)]
        source = "final_answer_field"
    else:
        field = last_nonempty_line(text)
        source = "final_line_fallback"
    r["numeric_field_raw"] = field
    nums = numeric_tokens(field)
    r["numeric_candidates"] = [canonical(n) for n in nums]
    if len(nums) == 1:
        r.update(numeric_parse_status="ok", numeric_parsed_answer=canonical(nums[0]), numeric_parse_source=source)
    elif len(nums) > 1:
        r.update(numeric_parse_status="ambiguous", numeric_ambiguous=True, numeric_parse_source=source)
    else:
        r["numeric_parse_status"] = "non_numeric" if labels else "missing"
        r["numeric_parse_source"] = source if labels else None
    return r


def parse_response(text):
    return {"parser_version": PARSER_VERSION, **parse_strict(text), **parse_numeric(text)}


def parse_run(run_dir):
    import pilot_common as pc
    run_dir = Path(run_dir)
    raw, note = pc.read_jsonl_tolerant(run_dir / "raw_outputs.jsonl")
    parsed = []
    for r in raw:
        p = parse_response(r["raw_response"])
        parsed.append({"run_id": r["run_id"], "run_index": r["run_index"], "request_key": r["request_key"],
                       "item_id": r["item_id"], "condition_source": r["condition_source"],
                       "analysis_condition": r["analysis_condition"], "gold_answer": r["gold_answer"],
                       "corrupted_answer": r["corrupted_answer"], "test_only": r.get("test_only", False), **p})
    parsed.sort(key=lambda r: r["run_index"])
    pc.write_jsonl(run_dir / "parsed_outputs.jsonl", parsed)
    return parsed, note


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    rows, note = parse_run(ap.parse_args().run_dir)
    from collections import Counter
    print(f"parsed {len(rows)} rows | strict:", dict(Counter(r["strict_parse_status"] for r in rows)),
          "| numeric:", dict(Counter(r["numeric_parse_status"] for r in rows)), note or "")
