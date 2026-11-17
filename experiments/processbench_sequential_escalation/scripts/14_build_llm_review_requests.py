"""Build + FREEZE the A3 LLM-assisted review requests (Pass 1 and Pass 2) for the frozen Stage-1 failure set.

    python scripts/14_build_llm_review_requests.py              # write + register (re-run = identical check)
    python scripts/14_build_llm_review_requests.py --check      # rebuild in memory and compare only
Pass-1 prompts are built from a whitelisted item view (no annotation, no Stage-1 output); audited (forbidden terms,
mutation test on every annotation field, no Stage-1 response text, Pass-2 shows exactly Steps 1..f) before writing.
Both passes use the annotation-review packet order (seed 6303). Writes data/processed/llm_review/ and
data/manifests/llm_review_request_manifest.json and registers them in FROZEN_HASHES.json. No model is called.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import llm_review as lr
from src import llm_review_runner as lrr
from src import stage2_runner as s2r


def build(cfg):
    lock = s2r.verify_lock(s2r.paths(None))
    if C.STAGE2_REQUESTS.exists():
        raise SystemExit("STOP: Stage-2 requests exist; the review must precede them")
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    fails = C.read_json(C.STAGE1_FAILURES)
    p1, p2, order = lr.build_requests(pool, fails, cfg)
    probs = lr.audit(p1, p2, pool, C.read_jsonl(C.DATA_PROC / "stage1_failure_responses.jsonl"))
    if probs:
        raise SystemExit("STOP: review request audit failed:\n" + "\n".join(probs[:20]))
    smoke = lr.smoke_items(p1, cfg["llm_review"]["smoke"]["n_items"])
    man = {"artifact": "llm_review_requests", "amendment": "A3 (reviewer per A4)", "prompt_version": lr.PROMPT_VERSION,
           "STATUS": cfg["llm_review"]["provisional_statement"],
           "reviewer": cfg["llm_review"]["reviewer"]["reviewer_tag"],
           "failures_sha256": lock["failures_sha256"], "items_sha256": C.sha256_file(C.ITEMS_FILE),
           "n_items": len(p1), "n_requests": {"pass1": len(p1), "pass2": len(p2)},
           "system_prompt_sha256": C.sha256_text(lr.SYSTEM_PROMPT),
           "pass1_question_sha256": C.sha256_text(lr.PASS1_QUESTION),
           "pass2_question_sha256": C.sha256_text(lr.PASS2_QUESTION),
           "pass1_prompts_sha256": C.sha256_text("\n".join(r["prompt_sha256"] for r in p1)),
           "pass2_prompts_sha256": C.sha256_text("\n".join(r["prompt_sha256"] for r in p2)),
           "pass1_fields_shown": list(lr.PASS1_FIELDS), "pass2_fields_shown": list(lr.PASS2_FIELDS),
           "pass2_steps_shown": "Steps 1..f (candidate step f = k-1); later steps omitted",
           "smoke_item_ids": smoke, "max_prompt_chars": {"pass1": max(r["prompt_chars"] for r in p1),
                                                        "pass2": max(r["prompt_chars"] for r in p2)},
           "audit": "passed"}
    return p1, p2, order, man


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    C.verify_frozen(C.AMENDED_PROTOCOL_FILES + lrr.CODE_FILES)
    p1, p2, order, man = build(cfg)
    texts = {lrr.PASS_FILES[1]: "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in p1),
             lrr.PASS_FILES[2]: "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in p2),
             lrr.ORDER_FILE: C.dumps(order)}
    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    if a.check or C.rel(lrr.PASS_FILES[1]) in frozen:
        same = all(C.sha256_text(t) == frozen.get(C.rel(p)) for p, t in texts.items())
        print(json.dumps({"rebuild_identical_to_frozen": same, "n_items": man["n_items"]}, indent=1))
        return 0 if same else 1
    entries = {}
    for pass_no, reqs in ((1, p1), (2, p2)):
        entries[C.rel(lrr.PASS_FILES[pass_no])] = C.write_jsonl(lrr.PASS_FILES[pass_no], reqs)
    entries[C.rel(lrr.ORDER_FILE)] = C.write_json(lrr.ORDER_FILE, order)
    man["files_sha256"] = dict(entries)
    man["created"] = C.now()
    entries[C.rel(lrr.REQUEST_MANIFEST)] = C.write_json(lrr.REQUEST_MANIFEST, man)
    C.update_frozen_hashes(entries)
    print(json.dumps({**{k: man[k] for k in ("n_items", "n_requests", "smoke_item_ids", "system_prompt_sha256",
                                              "pass1_prompts_sha256", "pass2_prompts_sha256", "max_prompt_chars")},
                      "files": entries}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
