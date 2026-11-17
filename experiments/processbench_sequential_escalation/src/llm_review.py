"""Blinded LLM-ASSISTED annotation-fidelity review (protocol amendment A3; feasibility study only).

The reviewer is a fixed open model (configs/stage_b.json -> llm_review.reviewer). Its judgments are PROVISIONAL and
must never be described as human-validated; final-scale experiments need human validation.

Two passes over the 76 frozen Stage-1 failures, in the frozen annotation-review packet order (seed 6303):
  PASS 1  independent first-error localisation. The prompt is built ONLY from a whitelisted view of the item
          (item_id, problem, numbered ORIGINAL solution, gold answer, original final answer). It never sees the
          ProcessBench first-error step, the candidate false step, any Stage-1 response/outcome or Stage 2.
          After ALL Pass-1 judgments exist they are compared with the frozen annotation k (map_first_error):
          same step -> CONFIRM; different definite step or NO_ERROR -> DISAGREE; UNCLEAR / unusable -> UNCLEAR.
          The reviewer's step is recorded only; the annotation is never repaired or substituted.
  PASS 2  candidate false-step validation (runs only after Pass 1 is complete): problem + Steps 1..f of the
          original solution (later steps omitted) + the candidate step f = k-1.
          -> CORRECT / NOT_CORRECT / UNCLEAR. The model-facing field is neutrally named `candidate_step_verdict`
          (it is recorded as false_step_verdict) so the word "false" never primes the reviewer.
Every output is parsed by the fixed rules in parse_output; outputs are never regenerated, whatever they say.

A4 (after the A3 reviewer smoke NO-GO): reviewer Qwen2.5-7B-Instruct; prompt version v2 changes ONLY the system prompt
(classification task, one JSON object only). User prompts, questions, output schemas and parse rules are unchanged.
exact_schema() is the stricter smoke-gate check (the whole output is one JSON object with exactly the allowed keys);
it never yields a judgment - parse_output alone does.
"""
import json
import re

from . import common as C
from .annotation_review import packet_order
from .dataset_builder import render_trace

PROMPT_VERSION = "llm_review_prompts_v2"
SYSTEM_PROMPT = ("You are a careful, rigorous mathematics reviewer performing a classification task. You are given a "
                 "math problem and a proposed solution, and you classify the proposed solution exactly as the user "
                 "asks. This is not a request to solve the problem or to discuss it conversationally: do not write "
                 "your own solution, a step-by-step walkthrough or any LaTeX. Your entire reply must be "
                 "exactly one JSON object in the requested format, with no text before or after it.")

PASS1_QUESTION = ("Inspect the reasoning from Step 1 onward. Identify the EARLIEST step that contains a substantive "
                  "reasoning, arithmetic, algebraic, or logical error. Do not choose a later step that merely "
                  "propagates an earlier error.")
PASS2_QUESTION = ("Is this candidate step itself correct given the problem and all valid reasoning before it? "
                  "Judge the step itself, not whether later steps are correct.")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")
PASS1_SPECIAL = ("NO_ERROR", "UNCLEAR")
PASS2_VERDICTS = ("CORRECT", "NOT_CORRECT", "UNCLEAR")

# fields each pass may see; build_* only ever reads these (blinding is structural, and tested by mutation)
PASS1_FIELDS = ("item_id", "question", "steps", "gold_answer", "original_answer")
PASS2_FIELDS = ("item_id", "question", "steps", "candidate_step")


def pass1_view(item):
    return {f: item[f] for f in PASS1_FIELDS}


def pass2_view(item):
    return {"item_id": item["item_id"], "question": item["question"], "steps": item["steps"],
            "candidate_step": item["false_location"]["false_step"]}


def pass1_prompt(view):
    assert set(view) == set(PASS1_FIELDS), sorted(view)
    return "\n".join([
        "Review the proposed solution to the following math problem.", "",
        f"Item: {view['item_id']}", "",
        "Problem:", view["question"].strip(), "",
        "Proposed solution (numbered steps):", render_trace(view["steps"]), "",
        f"Reference (correct) final answer: {view['gold_answer']}",
        f"Final answer reached by the proposed solution: {view['original_answer']}", "",
        "Task: " + PASS1_QUESTION, "",
        "Respond with ONLY one JSON object and no other text, in exactly this format:",
        '{"reason": "<short explanation, at most two sentences>", "earliest_error_step": <the step number as an '
        'integer, or "NO_ERROR", or "UNCLEAR">, "confidence": "<HIGH, MEDIUM or LOW>"}'])


def pass2_prompt(view):
    assert set(view) == set(PASS2_FIELDS), sorted(view)
    f = view["candidate_step"]
    shown = view["steps"][:f]
    return "\n".join([
        "Check one step of the proposed solution to the following math problem.", "",
        f"Item: {view['item_id']}", "",
        "Problem:", view["question"].strip(), "",
        f"Proposed solution, Steps 1 to {f} (any later steps are not shown):", render_trace(shown), "",
        f"Candidate step: Step {f}", f"Step {f}: {shown[-1].strip()}", "",
        "Question: " + PASS2_QUESTION, "",
        "Respond with ONLY one JSON object and no other text, in exactly this format:",
        '{"reason": "<short explanation, at most two sentences>", "candidate_step_verdict": "<CORRECT, '
        'NOT_CORRECT or UNCLEAR>", "confidence": "<HIGH, MEDIUM or LOW>"}'])


def render_chat(user):
    """Exact Qwen2.5 ChatML rendering with the FIXED system message (replaces the template's default system prompt).
    The real backend checks the tokenizer's own rendering equals this string for every request."""
    return (f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
            "<|im_start|>assistant\n")


def request_key(pass_no, item_id):
    return f"rv{pass_no}::{item_id}"


def build_requests(pool, failures_doc, cfg):
    """-> (pass1_requests, pass2_requests, order) for the frozen failure set, in the review packet order."""
    items = {i["item_id"]: i for i in pool}
    ids = packet_order(failures_doc["item_ids"], cfg["annotation_review"]["packet_order_seed"])
    out = {1: [], 2: []}
    for n, iid in enumerate(ids):
        it = items[iid]
        for p, prompt in ((1, pass1_prompt(pass1_view(it))), (2, pass2_prompt(pass2_view(it)))):
            out[p].append({"request_key": request_key(p, iid), "stage": f"review_pass{p}", "review_pass": p,
                           "review_index": n, "item_id": iid, "domain": it["domain"],
                           "prompt": prompt, "prompt_sha256": C.sha256_text(prompt), "prompt_chars": len(prompt),
                           "rendered_sha256": C.sha256_text(render_chat(prompt))})
    order = {"order_rule": "annotation-review packet order (annotation_review.packet_order_seed)",
             "packet_order_seed": cfg["annotation_review"]["packet_order_seed"], "n": len(ids), "item_ids": ids,
             "item_ids_sha256": C.sha256_text("\n".join(ids))}
    return out[1], out[2], order


def runner_order(order, pass_no):
    keys = [request_key(pass_no, i) for i in order["item_ids"]]
    return {"order": [{"run_index": n, "request_key": k} for n, k in enumerate(keys)],
            "order_sha256": C.sha256_text("\n".join(keys))}


def smoke_items(pass1_reqs, n):
    """Deterministic: the longest Pass-1 prompt (context probe), then alternate gsm8k / math in review order."""
    first = max(pass1_reqs, key=lambda r: (r["prompt_chars"], r["item_id"]))
    chosen, want = [first["item_id"]], "gsm8k" if first["domain"] == "math" else "math"
    rest = [r for r in sorted(pass1_reqs, key=lambda r: r["review_index"]) if r["item_id"] != first["item_id"]]
    while len(chosen) < n and rest:
        pick = next((r for r in rest if r["domain"] == want), rest[0])
        chosen.append(pick["item_id"])
        rest.remove(pick)
        want = "gsm8k" if want == "math" else "math"
    return chosen


FORBIDDEN_PASS1 = ("first error", "first-error", "annotat", "processbench", "false step", "candidate step",
                   "stage 1", "stage-1", "stage 2", "stage-2", "qwen3", "label", "verification")


def audit(pass1, pass2, pool, stage1_responses=()):
    """Blinding / format audits -> list of problems (empty = pass)."""
    items = {i["item_id"]: i for i in pool}
    probs = []
    for r in pass1:
        it = items[r["item_id"]]
        low = r["prompt"].lower()
        scaffold = low.replace(it["question"].strip().lower(), "").replace(render_trace(it["steps"]).lower(), "")
        bad = [w for w in FORBIDDEN_PASS1 if w in scaffold]
        if bad:
            probs.append(f"{r['request_key']}: forbidden terms in Pass-1 scaffold {bad}")
        mutated = dict(it, true_error_step=-99, label_0indexed=-99, true_error_relative_position=-1.0,
                       false_location={"false_step": -98}, processbench_id="X", generator="X")
        if pass1_prompt(pass1_view(mutated)) != r["prompt"]:
            probs.append(f"{r['request_key']}: Pass-1 prompt depends on annotation fields")
        if f"Step {it['n_steps']}: " not in r["prompt"] or f"Step {it['n_steps'] + 1}: " in r["prompt"]:
            probs.append(f"{r['request_key']}: Pass-1 step numbering not 1..n")
    for r in pass2:
        it = items[r["item_id"]]
        f = it["false_location"]["false_step"]
        if f"Step {f}: " not in r["prompt"] or f"Step {f + 1}: " in r["prompt"]:
            probs.append(f"{r['request_key']}: Pass-2 must show exactly Steps 1..{f}")
        if "false" in r["prompt"].lower().replace(it["question"].lower(), "").replace(
                render_trace(it["steps"][:f]).lower(), ""):
            probs.append(f"{r['request_key']}: Pass-2 scaffold uses the word 'false'")
    texts = [s.get("stage1_response", "") for s in stage1_responses]
    for r in pass1 + pass2:
        own = items[r["item_id"]]["question"] + render_trace(items[r["item_id"]]["steps"])
        for t in texts:
            if any(len(t[s:s + 60].strip()) >= 40 and t[s:s + 60] in r["prompt"] and t[s:s + 60] not in own
                   for s in range(0, max(1, len(t) - 60), 20)):
                probs.append(f"{r['request_key']}: contains Stage-1 response text")
                break
    if len({len(pass1), len(pass2), len({r['item_id'] for r in pass1})}) != 1:
        probs.append("Pass-1 / Pass-2 request sets differ")
    return probs


# ------------------------------------------------------------------ parsing (fixed rules, pre-specified)
def _json_objects(text):
    """Every balanced {...} substring that json-decodes to a dict, in order of appearance."""
    t = re.sub(r"```(?:json)?", "", text)
    found, dec = [], json.JSONDecoder()
    for m in re.finditer(r"\{", t):
        try:
            obj, _ = dec.raw_decode(t[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def parse_output(text, pass_no, n_steps=None):
    """-> {"parse_status", "judgment", "confidence", "reason"}.
    Rules: (1) code fences are ignored; (2) take every JSON object containing the judgment key
    (earliest_error_step | candidate_step_verdict); none -> 'no_json'; (3) if they disagree on the judgment ->
    'ambiguous'; (4) Pass 1: an integer (or digit string) in 1..n_steps, or NO_ERROR / UNCLEAR (case-insensitive);
    Pass 2: CORRECT / NOT_CORRECT / UNCLEAR; anything else -> 'invalid_judgment'; (5) confidence must be HIGH /
    MEDIUM / LOW, else 'invalid_confidence' (the judgment is still kept). parse_status 'ok' only if all valid."""
    key = "earliest_error_step" if pass_no == 1 else "candidate_step_verdict"
    objs = [o for o in _json_objects(text) if key in o]
    if not objs:
        return {"parse_status": "no_json", "judgment": None, "confidence": None, "reason": None}
    if len({json.dumps(o[key]) for o in objs}) > 1:
        return {"parse_status": "ambiguous", "judgment": None, "confidence": None, "reason": None}
    o, raw = objs[0], objs[0][key]
    judgment = None
    if pass_no == 1:
        if isinstance(raw, bool):
            pass
        elif isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
            v = int(raw)
            judgment = v if n_steps is not None and 1 <= v <= n_steps else None
        elif isinstance(raw, str) and raw.strip().upper() in PASS1_SPECIAL:
            judgment = raw.strip().upper()
    elif isinstance(raw, str) and raw.strip().upper() in PASS2_VERDICTS:
        judgment = raw.strip().upper()
    conf = o.get("confidence")
    conf = conf.strip().upper() if isinstance(conf, str) and conf.strip().upper() in CONFIDENCE else None
    status = "invalid_judgment" if judgment is None else ("ok" if conf else "invalid_confidence")
    reason = o.get("reason") if isinstance(o.get("reason"), str) else None
    return {"parse_status": status, "judgment": judgment, "confidence": conf, "reason": reason}


PASS_KEYS = {1: ("reason", "earliest_error_step", "confidence"), 2: ("reason", "candidate_step_verdict", "confidence")}


def exact_schema(text, pass_no, n_steps=None):
    """Smoke-gate check (A4), stricter than parse_output and never used to derive a judgment: after stripping
    surrounding whitespace (and at most one enclosing ``` / ```json fence) the WHOLE output is one JSON object with
    exactly the keys PASS_KEYS[pass_no], a string reason, and a judgment + confidence that parse_output accepts
    ('ok'). -> (bool, why)"""
    s = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", s, flags=re.S)
    if m:
        s = m.group(1)
    try:
        obj = json.loads(s)
    except ValueError:
        return False, "not exactly one JSON object"
    if not isinstance(obj, dict):
        return False, "not a JSON object"
    if set(obj) != set(PASS_KEYS[pass_no]):
        return False, f"keys {sorted(obj)} != {sorted(PASS_KEYS[pass_no])}"
    if not isinstance(obj["reason"], str):
        return False, "reason is not a string"
    status = parse_output(text, pass_no, n_steps)["parse_status"]
    return status == "ok", status


def map_first_error(judgment, k):
    """Pass-1 judgment vs the frozen ProcessBench annotation k (applied only after ALL Pass-1 rows exist)."""
    if isinstance(judgment, int):
        return "CONFIRM" if judgment == k else "DISAGREE"
    if judgment == "NO_ERROR":
        return "DISAGREE"
    return "UNCLEAR"                       # UNCLEAR, or no usable judgment (no_json / ambiguous / invalid)


def map_false_step(judgment):
    return judgment if judgment in PASS2_VERDICTS else "UNCLEAR"


# ------------------------------------------------------------------ TEST-ONLY mock reviewer
MOCK_BANNER = "[TEST ONLY - MOCK REVIEWER - NOT A MODEL OUTPUT]"


class MockReviewer:
    """Deterministic (hash of the prompt) JSON judgments; exercises every parse path. Nothing here is a result."""
    name = "mock"

    def __init__(self, max_new_tokens=768, profile="default"):
        self.max_new_tokens, self.profile, self.calls = max_new_tokens, profile, 0

    def generate(self, prompt):
        from .mock_solver import MockResult
        self.calls += 1
        h = int(C.sha256_text(prompt)[:8], 16)
        n = len(re.findall(r"^Step \d+: ", prompt, flags=re.M))
        conf = CONFIDENCE[h % 3]
        if self.profile == "bad_json" or (self.profile == "default" and h % 17 == 0):
            body = "I think the error is somewhere in the middle."
        elif "earliest_error_step" in prompt:
            step = 1 + (h % max(n, 1)) if h % 11 else "UNCLEAR"
            body = json.dumps({"reason": f"{MOCK_BANNER} mock", "earliest_error_step": step, "confidence": conf})
        else:
            body = json.dumps({"reason": f"{MOCK_BANNER} mock", "candidate_step_verdict": PASS2_VERDICTS[h % 3],
                               "confidence": conf})
        return MockResult(body, {"finish_reason": "stop", "new_tokens": 40, "prompt_tokens": len(prompt) // 3})

    def effective_settings(self):
        return {"backend": "mock", "TEST_ONLY": True, "max_new_tokens": self.max_new_tokens}
