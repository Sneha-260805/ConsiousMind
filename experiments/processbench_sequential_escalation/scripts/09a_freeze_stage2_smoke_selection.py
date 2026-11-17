"""Freeze the Stage-2 SMOKE selection (before any Stage-2 inference). The 244-request set / order are only read.

    python scripts/09a_freeze_stage2_smoke_selection.py
Selection = stage2_runner.smoke_keys (longest-prompt item first, then execution order; stage2.smoke.n_items items x
all conditions), with the truncation edge case required by the researcher (stage2_runner.TRUNCATION_RULE): at least
one item whose Stage-1 response hit max_new_tokens. Writes data/manifests/stage2_smoke_selection.json (registered in
FROZEN_HASHES); scripts/09 refuses a real smoke whose keys differ from it. Re-running is a byte-identical no-op.
"""
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import stage2_runner as s2r


def main():
    cfg = C.load_config()
    P = s2r.paths(None)
    by_key, order, hashes, lock = s2r.frozen_inputs(P)
    C.verify_frozen([C.rel(C.STAGE2_MANIFEST)])
    if C.RUNS_ROOT.exists() and any(C.RUNS_ROOT.iterdir()):
        raise SystemExit("STOP: real runs exist locally; the smoke selection is frozen before any Stage-2 inference")
    fails = C.read_json(P["failures"])["failures"]
    keys, info = s2r.smoke_keys_with_truncation(by_key, order, cfg, fails)
    f1 = {f["item_id"]: f for f in fails}
    doc = {"stage": 2, "artifact": "stage2_smoke_selection", "rule": s2r.TRUNCATION_RULE,
           "base_rule": "stage2_runner.smoke_keys: longest Stage-2 prompt item first, then Stage-2 execution order",
           "n_items": len(info["items"]), "n_requests": len(keys), **info,
           "items_detail": {i: {"domain": f1[i]["domain"], "stage1_truncated": f1[i]["stage1_truncated"],
                                "stage1_new_tokens": f1[i]["stage1_new_tokens"],
                                "max_stage2_prompt_chars": max(r["prompt_chars"] for r in by_key.values()
                                                               if r["item_id"] == i)} for i in info["items"]},
           "request_keys": keys, "request_keys_sha256": C.sha256_text("\n".join(keys)),
           "requests_sha256": hashes["requests"], "order_sha256": hashes["order"],
           "stage2_manifest_sha256": C.sha256_file(C.STAGE2_MANIFEST), "failures_sha256": lock["failures_sha256"]}
    sha, status = C.write_frozen(s2r.SMOKE_SELECTION, C.write_json, doc)
    C.update_frozen_hashes({C.rel(s2r.SMOKE_SELECTION): sha})
    print(json.dumps({"status": status, "sha256": sha, **{k: doc[k] for k in (
        "items", "replaced", "replacement", "stage1_truncated_items", "n_requests", "items_detail")}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
