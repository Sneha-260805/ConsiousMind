"""DIAGNOSTIC ONLY - headroom follow-up to the NO-GO Stage-2 smoke (bundle v9). NOT Stage-2 data, NOT scored, never
substituted for the 2048-token smoke output, not a scientific protocol result.

    python scripts/22_diag_headroom.py --solver qwen --output-root /kaggle/working/stage_b_diag      # Kaggle GPU
    python scripts/22_diag_headroom.py --solver mock                                                # TEST ONLY
ONE generation: the frozen request s2::sb_math-355::VV, its exact frozen prompt (sha-checked against the frozen
request file AND the v9 smoke record), the frozen solver (Qwen3-4B @ 3101254b, FP16, greedy, enable_thinking=False,
batch 1) with ONLY max_new_tokens changed 2048 -> 4096 on an in-memory copy of the solver config (configs are not
modified). Writes <output-root>/diag_headroom_sb_math-355_VV_4096/ (never a Stage-2 run directory):
  raw_output.json   prompt sha, raw text, model metadata, effective settings (preserved separately)
  diagnostic.json   finish reason, generated tokens, FINAL_ANSWER present / parse status (format only - the answer
                    is NOT compared with the gold answer), repetition indicators, and whether the first part is
                    byte-identical to the preserved 2048-token smoke output (greedy prefix check)
Refuses if the output directory already holds a result (never overwritten).
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401
from src import common as C
from src import parser
from src import solver_loader as sl
from src import stage2_runner as s2r

KEY = "s2::sb_math-355::VV"
DIAG_MAX_NEW_TOKENS = 4096
LABEL = "DIAGNOSTIC ONLY - not Stage-2 data, not scored, not substituted for the 2048-token smoke output"


def repetition(text):
    """Descriptive loop indicators (no threshold decides anything; manual inspection of the tail is primary)."""
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 30]
    rep = {l: c for l, c in Counter(lines).items() if c >= 3}
    q = len(text) * 3 // 4
    head, tail = text[:q], text[q:]
    sh = [tail[i:i + 50] for i in range(0, max(0, len(tail) - 50), 25)]
    seen = sum(s in head for s in sh)
    return {"long_lines_repeated_3plus": len(rep), "max_line_repeats": max(rep.values(), default=0),
            "phrase_counts": {p: len(re.findall(p, text, re.I)) for p in
                              ("go back", "not correct", r"\bwait\b", "let.s try again", "recompute")},
            "last_quarter_50char_shingles_seen_earlier": round(seen / len(sh), 3) if sh else None,
            "most_repeated_lines": sorted(rep.items(), key=lambda x: -x[1])[:5]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", choices=["mock", "qwen"], required=True)
    ap.add_argument("--output-root", default=None)
    a = ap.parse_args()
    cfg = C.load_config()
    P = s2r.paths(None)
    by_key, _, hashes, _ = s2r.frozen_inputs(P)                # verifies the frozen 244 requests / order / review
    req = by_key[KEY]
    rec = C.read_json(C.DATA_MAN / "STAGE2_SMOKE_V9_NOGO.json")
    C.verify_frozen(["data/manifests/STAGE2_SMOKE_V9_NOGO.json"])
    smoke = rec["sb_math_355_VV_2048"]
    if C.sha256_text(req["prompt"]) != req["prompt_sha256"] or req["prompt_sha256"] != smoke["prompt_sha256"]:
        raise SystemExit("STOP: prompt differs from the frozen request / the v9 smoke record")
    sc, sha, ov = sl.solver_config(cfg)
    if sc["max_new_tokens"] != 2048:
        raise SystemExit("STOP: frozen Stage-2 max_new_tokens is not 2048")
    diag_sc = {**sc, "max_new_tokens": DIAG_MAX_NEW_TOKENS}         # in-memory copy; ONLY this field differs
    if a.solver == "mock":
        from src.mock_solver import MockSolver
        backend, test_only = MockSolver([req], DIAG_MAX_NEW_TOKENS), True
        out_root = Path(a.output_root or C.TEST_RUNS_ROOT / "diag")
        if C.TEST_RUNS_ROOT.resolve() not in out_root.resolve().parents and out_root.resolve() != C.TEST_RUNS_ROOT.resolve():
            raise SystemExit(f"STOP: mock output must be under {C.TEST_RUNS_ROOT}")
    else:
        if not a.output_root:
            raise SystemExit("STOP: --output-root is required for the real solver")
        out_root = Path(a.output_root)
        sl.guard_real_model()
        be = __import__("src.frozen_sources", fromlist=["x"]).pilot_module("backends", cfg)
        backend = be.make_backend(diag_sc)
        eff = backend.effective_settings()
        bad = []
        if eff.get("hub_resolved_revision") != sc["model_revision"]:
            bad.append("revision")
        if "float16" not in str(eff.get("dtype")) or eff.get("device") != "cuda:0":
            bad.append("dtype/device")
        if eff.get("enable_thinking") is not False or eff.get("max_new_tokens") != DIAG_MAX_NEW_TOKENS:
            bad.append("thinking/max_new_tokens")
        if eff.get("decoding") != "greedy (do_sample=False, num_beams=1)":
            bad.append("decoding")
        if bad:
            raise SystemExit(f"STOP: loaded solver differs from the frozen protocol (other than max_new_tokens): {bad}")
        test_only = False
    if out_root.resolve() == C.RUNS_ROOT.resolve() or C.DATA_PROC.resolve() in out_root.resolve().parents:
        raise SystemExit("STOP: diagnostic output never goes into data/ or outputs/runs/")
    d = out_root / f"diag_headroom_sb_math-355_VV_{DIAG_MAX_NEW_TOKENS}"
    if (d / "raw_output.json").exists():
        raise SystemExit(f"STOP: {d} already holds a diagnostic result; it is never overwritten")
    d.mkdir(parents=True, exist_ok=True)
    started = C.now()
    res = backend.generate(req["prompt"])
    text, meta = res.text, res.metadata
    raw = {"LABEL": LABEL, "TEST_ONLY": test_only, "request_key": KEY, "prompt": req["prompt"],
           "prompt_sha256": req["prompt_sha256"], "raw_response": text, "raw_response_sha256": C.sha256_text(text),
           "model_metadata": meta, "started": started, "finished": C.now(),
           "solver": {"model": sc["model"], "model_revision": sc["model_revision"], "frozen_config_sha256": sha,
                      "max_new_tokens_frozen_stage2": sc["max_new_tokens"],
                      "max_new_tokens_diagnostic": DIAG_MAX_NEW_TOKENS,
                      "only_difference": "max_new_tokens 2048 -> 4096 (in-memory copy of the solver config)"},
           "effective_settings": backend.effective_settings(),
           "frozen_inputs": {"requests_sha256": hashes["requests"], "order_sha256": hashes["order"]}}
    C.write_json(d / "raw_output.json", raw)
    trunc = meta.get("finish_reason") == "length"
    p = parser.parse(text, truncated=trunc)
    prefix_len = smoke["raw_response_chars"]
    prefix_same = C.sha256_text(text[:prefix_len]) == smoke["raw_response_sha256"]
    diag = {"LABEL": LABEL, "TEST_ONLY": test_only, "request_key": KEY,
            "finish_reason": meta.get("finish_reason"), "generated_tokens": meta.get("new_tokens"),
            "max_new_tokens": DIAG_MAX_NEW_TOKENS, "prompt_tokens": meta.get("prompt_tokens"),
            "hit_cap": meta.get("new_tokens") == DIAG_MAX_NEW_TOKENS or trunc,
            "final_answer_label_present": "FINAL_ANSWER" in text.upper(),
            "numeric_parse_status": p.get("numeric_parse_status"),
            "numeric_parsed_answer_format_only": p.get("numeric_parsed_answer"),
            "note_on_answer": "format check only; NOT compared with the gold answer, NOT a Stage-2 outcome",
            "prefix_identical_to_2048_smoke_output": prefix_same,
            "repetition_full": repetition(text),
            "repetition_beyond_2048_output": repetition(text[prefix_len:]) if prefix_same and len(text) > prefix_len else None,
            "tail_2000_chars": text[-2000:],
            "raw_output_sha256": C.sha256_file(d / "raw_output.json")}
    C.write_json(d / "diagnostic.json", diag)
    print(json.dumps({k: v for k, v in diag.items() if k != "tail_2000_chars"}, indent=1, ensure_ascii=False))
    print("---- last 2000 characters ----\n" + diag["tail_2000_chars"])
    print(f"{LABEL}. STOP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
