"""Blinded HUMAN two-pass annotation-fidelity review (protocol amendment A5; replaces the stopped A3/A4 LLM reviewer).

Same two-pass protocol, items, order and questions as A3 (src/llm_review.py); only the reviewer is now a human.
  PASS 1  view file = item_id, problem, numbered ORIGINAL solution, gold answer, original final answer
          (src/llm_review.py pass1_view; nothing else is written to the file). The reviewer records
          earliest_error_step (1..n | NO_ERROR | UNCLEAR), confidence (HIGH/MEDIUM/LOW) and an optional reason.
          All 76 judgments are FROZEN before the ProcessBench step k is read; only then map_first_error
          (same step -> CONFIRM; other definite step or NO_ERROR -> DISAGREE; UNCLEAR -> UNCLEAR). The reviewer's
          step is recorded only and never substituted into any prompt.
  PASS 2  view file (generated at the Pass-1 freeze) = item_id, problem, Steps 1..f only, candidate step f = k-1.
          -> CORRECT / NOT_CORRECT / UNCLEAR + confidence + optional reason.
Judgments live in an append-only, hash-chained JSONL log per pass: every save (including a revision) is one line
with UTC timestamps; the current judgment of an item is its last line; nothing is ever deleted or rewritten.
"""
import json
import os
from pathlib import Path

from . import common as C
from . import llm_review as lr
from .annotation_review import packet_order

CONFIDENCE = lr.CONFIDENCE
PASS1_SPECIAL = lr.PASS1_SPECIAL
PASS2_VERDICTS = lr.PASS2_VERDICTS
QUESTIONS = {1: lr.PASS1_QUESTION, 2: lr.PASS2_QUESTION}
PASS1_ITEM_KEYS = ("index", "item_id", "question", "steps", "gold_answer", "original_answer")
PASS2_ITEM_KEYS = ("index", "item_id", "question", "steps", "candidate_step")
LOG_KEYS = ("seq", "pass", "reviewer", "item_id", "judgment", "confidence", "reason", "opened_at", "saved_at",
            "view_file_sha256", "view_item_sha256", "prev_line_sha256")
GENESIS = "0" * 64


def paths(work_dir=None):
    """Real: data/processed/human_review + data/manifests. TEST: everything under the --work-dir."""
    from . import stage2_runner as s2r
    P = s2r.paths(work_dir)
    d = (C.DATA_PROC if P["real"] else Path(work_dir).resolve()) / "human_review"
    man = C.DATA_MAN if P["real"] else d
    return {**P, "hr_dir": d,
            "view": {1: d / "pass1_view.json", 2: d / "pass2_view.json"},
            "log": {1: d / "pass1_log.jsonl", 2: d / "pass2_log.jsonl"},
            "final": {1: d / "pass1_final.json", 2: d / "pass2_final.json"},
            "instructions": d / "REVIEWER_INSTRUCTIONS.md",
            "mapping": d / "pass1_mapping.json",
            "judgments": d / "human_review_judgments.json",
            "export_lock": {1: man / "HUMAN_REVIEW_EXPORT.json", 2: man / "HUMAN_REVIEW_PASS2_EXPORT.json"},
            "pass_lock": {1: man / "HUMAN_REVIEW_PASS1_FROZEN.json", 2: man / "HUMAN_REVIEW_PASS2_FROZEN.json"},
            "final_lock": man / "HUMAN_REVIEW_FROZEN.json"}


# ------------------------------------------------------------------ view files (blinding is structural)
def _ordered(pool, failures_doc, cfg):
    items = {i["item_id"]: i for i in pool}
    return [items[i] for i in packet_order(failures_doc["item_ids"], cfg["annotation_review"]["packet_order_seed"])]


def pass1_view_doc(pool, failures_doc, cfg, test_only):
    """Built ONLY from llm_review.pass1_view (item_id, question, steps, gold_answer, original_answer)."""
    items = [{"index": n, **lr.pass1_view(it)} for n, it in enumerate(_ordered(pool, failures_doc, cfg))]
    return {"pass": 1, "TEST_ONLY": test_only, "question": QUESTIONS[1], "n_items": len(items),
            "item_ids": [x["item_id"] for x in items], "items": items}


def pass2_view_doc(pool, failures_doc, cfg, test_only):
    """Steps 1..f ONLY (later steps are not even written to the file) + the candidate step number f."""
    items = []
    for n, it in enumerate(_ordered(pool, failures_doc, cfg)):
        v = lr.pass2_view(it)
        items.append({"index": n, "item_id": v["item_id"], "question": v["question"],
                      "steps": v["steps"][:v["candidate_step"]], "candidate_step": v["candidate_step"]})
    return {"pass": 2, "TEST_ONLY": test_only, "question": QUESTIONS[2], "n_items": len(items),
            "item_ids": [x["item_id"] for x in items], "items": items}


def item_sha(item):
    return C.sha256_text(json.dumps(item, sort_keys=True, ensure_ascii=False))


def audit_view(doc, pool, stage1_responses=()):
    """-> problems (empty = pass). Exact keys; Pass 1 independent of every annotation field (mutation test);
    Pass 2 shows exactly Steps 1..f; no Stage-1 response text anywhere."""
    items = {i["item_id"]: i for i in pool}
    p = doc["pass"]
    probs = []
    keys = PASS1_ITEM_KEYS if p == 1 else PASS2_ITEM_KEYS
    for x in doc["items"]:
        it = items[x["item_id"]]
        if tuple(x) != keys:
            probs.append(f"{x['item_id']}: keys {list(x)} != {list(keys)}")
        if p == 1:
            mutated = dict(it, true_error_step=-99, label_0indexed=-99, true_error_relative_position=-1.0,
                           false_location={"false_step": -98}, processbench_id="X", generator="X")
            if {"index": x["index"], **lr.pass1_view(mutated)} != x:
                probs.append(f"{x['item_id']}: Pass-1 view depends on annotation fields")
            if x["steps"] != it["steps"]:
                probs.append(f"{x['item_id']}: Pass-1 must show the whole original solution")
        else:
            f = it["false_location"]["false_step"]
            if x["candidate_step"] != f or x["steps"] != it["steps"][:f] or len(x["steps"]) != f:
                probs.append(f"{x['item_id']}: Pass-2 must show exactly Steps 1..{f}")
    blob = json.dumps(doc, ensure_ascii=False)
    for s in stage1_responses:
        t = s.get("stage1_response", "").strip()
        own = items[s["item_id"]]["question"] + "\n".join(items[s["item_id"]]["steps"]) if s.get("item_id") in items else ""
        for i in range(0, max(1, len(t) - 60), 40):
            frag = t[i:i + 60]
            if len(frag.strip()) >= 40 and frag in blob and frag not in own:
                probs.append(f"{s.get('item_id')}: Stage-1 response text in the view file")
                break
    return probs


# ------------------------------------------------------------------ judgments + append-only hash-chained log
def validate(pass_no, judgment, confidence, reason, n_steps, max_reason=500):
    """-> (judgment normalised, confidence, reason) or raises ValueError."""
    if pass_no == 1:
        if isinstance(judgment, str) and judgment.strip().upper() in PASS1_SPECIAL:
            judgment = judgment.strip().upper()
        else:
            try:
                judgment = int(str(judgment).strip())
            except ValueError:
                raise ValueError(f"earliest_error_step must be 1..{n_steps}, NO_ERROR or UNCLEAR")
            if not 1 <= judgment <= n_steps:
                raise ValueError(f"earliest_error_step {judgment} outside 1..{n_steps}")
    else:
        judgment = str(judgment or "").strip().upper()
        if judgment not in PASS2_VERDICTS:
            raise ValueError(f"verdict must be one of {PASS2_VERDICTS}")
    confidence = str(confidence or "").strip().upper()
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
    reason = " ".join(str(reason or "").split())
    if len(reason) > max_reason:
        raise ValueError(f"reason longer than {max_reason} characters")
    return judgment, confidence, reason


def line_sha(raw_line):
    return C.sha256_text(raw_line)


def read_log(path):
    """-> list of entries. Verifies seq, hash chain and that the file ends with a complete line; any damage STOPs
    (nothing is ever repaired or dropped silently)."""
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        raise SystemExit(f"STOP: {path} ends with an incomplete line (interrupted write?). Do not edit it; report it.")
    out, prev = [], GENESIS
    for n, raw in enumerate(text.splitlines()):
        try:
            e = json.loads(raw)
        except ValueError:
            raise SystemExit(f"STOP: {path} line {n + 1} is not valid JSON")
        if tuple(e) != LOG_KEYS or e["seq"] != n or e["prev_line_sha256"] != prev:
            raise SystemExit(f"STOP: {path} line {n + 1} breaks the log sequence / hash chain (was the file edited?)")
        prev = line_sha(raw)
        out.append(e)
    return out


def append(path, view_doc, view_file_sha, pass_no, reviewer, item_id, judgment, confidence, reason, opened_at,
           max_reason=500):
    """Validate and append ONE judgment line (flushed + fsynced). -> entry."""
    items = {x["item_id"]: x for x in view_doc["items"]}
    if item_id not in items:
        raise ValueError(f"unknown item {item_id!r}")
    if not str(reviewer or "").strip():
        raise ValueError("reviewer name required")
    x = items[item_id]
    n_steps = len(x["steps"])
    judgment, confidence, reason = validate(pass_no, judgment, confidence, reason, n_steps, max_reason)
    path = Path(path)
    log = read_log(path)
    prev = line_sha(path.read_text(encoding="utf-8").splitlines()[-1]) if log else GENESIS
    e = {"seq": len(log), "pass": pass_no, "reviewer": str(reviewer).strip(), "item_id": item_id,
         "judgment": judgment, "confidence": confidence, "reason": reason, "opened_at": opened_at or None,
         "saved_at": C.now(), "view_file_sha256": view_file_sha, "view_item_sha256": item_sha(x),
         "prev_line_sha256": prev}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return e


def current(log):
    """-> {item_id: last entry}, {item_id: number of saves}."""
    cur, n = {}, {}
    for e in log:
        cur[e["item_id"]] = e
        n[e["item_id"]] = n.get(e["item_id"], 0) + 1
    return cur, n


def completeness(view_doc, log, view_file_sha):
    """-> problems preventing a freeze (empty = complete)."""
    probs = []
    ids = view_doc["item_ids"]
    cur, _ = current(log)
    missing = [i for i in ids if i not in cur]
    if missing:
        probs.append(f"{len(missing)} of {len(ids)} items have no judgment yet (e.g. {missing[:3]})")
    unknown = sorted(set(cur) - set(ids))
    if unknown:
        probs.append(f"log contains unknown items {unknown[:3]}")
    if any(e["view_file_sha256"] != view_file_sha for e in log):
        probs.append("some judgments were made on a different view file")
    items = {x["item_id"]: x for x in view_doc["items"]}
    if any(e["item_id"] in items and e["view_item_sha256"] != item_sha(items[e["item_id"]]) for e in log):
        probs.append("some judgments were made on a different item view")
    reviewers = sorted({e["reviewer"] for e in log})
    if len(reviewers) > 1:
        probs.append(f"more than one reviewer name in the log: {reviewers}")
    return probs


def final_rows(view_doc, log):
    cur, n = current(log)
    return [{**{k: cur[i][k] for k in ("item_id", "judgment", "confidence", "reason", "opened_at", "saved_at",
                                        "seq", "reviewer")}, "n_saves": n[i]} for i in view_doc["item_ids"]]


def map_first_error(judgment, k):
    """Applied only after Pass 1 is frozen (same rule as A3: llm_review.map_first_error)."""
    return lr.map_first_error(judgment, k)
