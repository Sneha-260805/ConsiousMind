"""ProcessBench schema handling, reference-answer lookup and conservative numeric answer normalisation.

ProcessBench (Qwen/ProcessBench @ 3bdcd537) record schema - verified by scripts/01_inspect_processbench.py:
    id                    str   "<split>-<n>"
    generator             str   model that wrote the solution
    problem               str
    steps                 list[str]   the solution, already segmented into steps (paragraphs)
    final_answer_correct  bool
    label                 int   index of the EARLIEST erroneous step, 0-INDEXED; -1 = no error annotated
There is NO reference-answer field. Gold answers come from the source benchmarks:
    gsm8k split  -> GSM8K test   (text after '####')                   exact whitespace-normalised problem match
    math split   -> MATH test    (last \\boxed{...} of the reference solution)   exact whitespace-normalised match

Step numbering shown to the solver is 1-INDEXED: annotated first-error step k = label + 1.

Numeric normalisation (used for gold answers and for the ORIGINAL trace's final answer, never for model outputs -
those use src/parser.py, which reuses this normaliser for terminal \\boxed{} answers): formatting-only
clean-up of a \\boxed{} / '####' string; returns a canonical decimal string or None. Accepted: integers, decimals, negatives, thousands separators (1,000 / 1{,}000), a leading
$ or \\$, a trailing %, ^\\circ, \\text{...} unit words, a single leading 'x =' style variable. Anything else
(fractions, roots, pi, tuples, intervals, words, expressions) -> None (item is not machine-checkable here).
"""
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA_FIELDS = {"id": str, "generator": str, "problem": str, "steps": list, "final_answer_correct": bool,
                 "label": int}
SPLITS = ["gsm8k", "math", "olympiadbench", "omnimath"]


class SchemaError(ValueError):
    pass


def ws(s):
    return " ".join(s.split())


def validate_record(rec, split=None):
    if not isinstance(rec, dict):
        raise SchemaError(f"record is {type(rec).__name__}, not an object")
    missing = [k for k in SCHEMA_FIELDS if k not in rec]
    extra = [k for k in rec if k not in SCHEMA_FIELDS]
    if missing or extra:
        raise SchemaError(f"{rec.get('id')}: missing {missing} / unexpected {extra}")
    for k, t in SCHEMA_FIELDS.items():
        v = rec[k]
        if t is int and (isinstance(v, bool) or not isinstance(v, int)):
            raise SchemaError(f"{rec['id']}: {k} is not an int")
        if t is not int and not isinstance(v, t):
            raise SchemaError(f"{rec['id']}: {k} is not {t.__name__}")
    if not all(isinstance(s, str) for s in rec["steps"]):
        raise SchemaError(f"{rec['id']}: non-string step")
    if not (-1 <= rec["label"] < len(rec["steps"])):
        raise SchemaError(f"{rec['id']}: label {rec['label']} outside [-1, {len(rec['steps']) - 1}]")
    if split and not rec["id"].startswith(split + "-"):
        raise SchemaError(f"{rec['id']}: id does not belong to split {split}")
    return rec


def load_split(path, split=None):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SchemaError(f"{path}: top level is {type(data).__name__}, expected a list")
    ids = [validate_record(r, split)["id"] for r in data]
    if len(ids) != len(set(ids)):
        raise SchemaError(f"{path}: duplicate ids")
    return data


def first_error_step(rec):
    """1-indexed earliest erroneous step, or None if no error is annotated."""
    return rec["label"] + 1 if rec["label"] >= 0 else None


# ------------------------------------------------------------------ \boxed{} extraction
BOX = re.compile(r"\\(?:boxed|fbox)\s*\{")


def boxed_all(text):
    """Contents of every \\boxed{...} / \\fbox{...} (balanced braces), in order. Unbalanced -> skipped."""
    out = []
    for m in BOX.finditer(text or ""):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        if depth == 0:
            out.append(text[m.end(): i - 1])
    return out


def last_boxed(text):
    b = boxed_all(text)
    return b[-1] if b else None


# ------------------------------------------------------------------ numeric normalisation
_TEXT = re.compile(r"\\(?:text|textbf|mbox|mathrm|textrm)\s*\{([^{}]*)\}")
_NUM = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")
_THOUSANDS = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_VAR = re.compile(r"^[A-Za-z]\s*=\s*")
# unit words: whitespace-separated, >= 2 letters each ("18 ways", "12 square units"); "2x", "2b", "-2x" are NOT units
_UNIT_WORDS = re.compile(r"^(-?[\d.,]+)(?:\s+[A-Za-z]{2,}(?:\s+[A-Za-z]{2,})*)?$")


def canonical(dec):
    if dec == dec.to_integral_value():
        return str(int(dec))
    return format(dec.normalize(), "f")


def normalise_numeric(raw):
    if raw is None:
        return None
    s = raw.strip()
    s = s.replace("{,}", ",").replace("\\!", "").replace("\\,", "").replace("\\;", "").replace("\\:", "")
    s = s.replace("~", " ").replace("\\$", "").replace("$", "").replace("\\%", "").replace("%", "")
    s = re.sub(r"\^\s*\{?\\circ\}?", "", s).replace("\\circ", "")
    s = s.replace("\\left", "").replace("\\right", "")
    for _ in range(3):
        s = _TEXT.sub(lambda m: " " + m.group(1) + " ", s)
    s = " ".join(s.split()).rstrip(".").strip()
    s = _VAR.sub("", s)
    m = _UNIT_WORDS.fullmatch(s)
    if not m:
        return None
    v = m.group(1)
    if "," in v:
        if not _THOUSANDS.fullmatch(v):
            return None
        v = v.replace(",", "")
    if not _NUM.fullmatch(v):
        return None
    try:
        return canonical(Decimal(v))
    except InvalidOperation:
        return None


def answer_type(canon):
    if canon is None:
        return "non_numeric"
    d = Decimal(canon)
    kind = "integer" if d == d.to_integral_value() else "decimal"
    return ("negative_" if d < 0 else "") + kind


# ------------------------------------------------------------------ reference answers
def gsm8k_reference_index(test_jsonl):
    idx = {}
    for i, line in enumerate(Path(test_jsonl).read_text(encoding="utf-8").splitlines()):
        if line.strip():
            r = json.loads(line)
            idx.setdefault(ws(r["question"]), []).append(
                {"source": "gsm8k_test", "source_index": i, "raw": r["answer"].split("####")[-1].strip()})
    return idx


def math_reference_index(rows_jsonl):
    idx = {}
    for line in Path(rows_jsonl).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            idx.setdefault(ws(r["problem"]), []).append(
                {"source": "hendrycks_math_test", "source_index": f"{r['subject_config']}/{r['row_idx']}",
                 "raw": last_boxed(r["solution"]), "level": r.get("level"), "subject": r.get("type")})
    return idx


def math500_index(jsonl):
    idx = {}
    for line in Path(jsonl).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            idx[ws(r["problem"])] = r["answer"]
    return idx


def lookup_reference(problem, index):
    """-> (status, ref) with status ok | no_match | multiple_conflicting."""
    hits = index.get(ws(problem), [])
    if not hits:
        return "no_match", None
    vals = {normalise_numeric(h["raw"]) if h["raw"] is not None else None for h in hits}
    raws = {h["raw"] for h in hits}
    if len(hits) > 1 and (len(vals) > 1 or len(raws) > 1):
        return "multiple_conflicting", None
    return "ok", hits[0]
