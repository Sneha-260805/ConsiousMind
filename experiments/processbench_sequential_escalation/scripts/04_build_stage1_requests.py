"""Build and FREEZE the Stage-1 request set (V on every pool item) and its execution order. No model is called.

    python scripts/04_build_stage1_requests.py [--rebuild "<approved reason>"]
Writes data/processed/stage1_requests.jsonl, data/processed/stage1_execution_order.json,
data/manifests/stage1_request_manifest.json, and registers their sha256 in FROZEN_HASHES.json.
Stops if any prompt audit fails.
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import stage1_runner as s1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", metavar="REASON", default=None,
                    help="deliberate, researcher-approved rebuild of frozen artifacts (reason is logged)")
    a = ap.parse_args()
    cfg = C.load_config()
    C.verify_frozen([C.rel(C.ITEMS_FILE)])
    pool = C.read_json(C.ITEMS_FILE)["pool"]
    reqs = s1.build_requests(pool)
    probs = s1.audit(reqs, pool)
    if probs:
        raise SystemExit("STOP: Stage-1 prompt audit failed:\n" + "\n".join(probs[:20]))
    order = s1.execution_order(reqs, cfg["stage1"]["execution_order_seed"])
    res = {C.rel(C.STAGE1_REQUESTS): C.write_frozen(C.STAGE1_REQUESTS, C.write_jsonl, reqs, bool(a.rebuild)),
           C.rel(C.STAGE1_ORDER): C.write_frozen(C.STAGE1_ORDER, C.write_json, order, bool(a.rebuild))}
    smoke = s1.smoke_keys(reqs, order, cfg)
    chars = sorted(r["prompt_chars"] for r in reqs)
    man = {"stage": 1, "condition": "V", "n_requests": len(reqs), "request_key_format": "s1::<item_id>::V",
           "execution_order_seed": cfg["stage1"]["execution_order_seed"], "order_sha256": order["order_sha256"],
           "items_sha256": C.sha256_file(C.ITEMS_FILE),
           "prompt_chars": {"min": chars[0], "median": chars[len(chars) // 2], "max": chars[-1]},
           "by_domain": {d: sum(r["domain"] == d for r in reqs) for d in cfg["dataset"]["domains_used"]},
           "audit": "passed (unique keys, identical scaffold, no forbidden terms, 1..n step numbering)",
           "smoke_request_keys": smoke,
           "artifacts_sha256": {k: v[0] for k, v in res.items()}}
    res[C.rel(C.STAGE1_MANIFEST)] = C.write_frozen(C.STAGE1_MANIFEST, C.write_json, man, bool(a.rebuild))
    C.update_frozen_hashes({k: v[0] for k, v in res.items()}, a.rebuild)
    print(json.dumps({k: v[1] for k, v in res.items()}, indent=1))
    print(json.dumps({k: man[k] for k in ("n_requests", "by_domain", "prompt_chars", "order_sha256")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
