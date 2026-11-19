"""Smoke-gate decision (researcher decision 2026-10-12). decide() = the Stage-1 smoke gate and the pre-A7 Stage-2 gate
(the v9 Stage-2 NO-GO was decided by it and is never rescored); since amendment A7 the Stage-2 smoke uses
decide_stage2_a7() below.

  technical checks (ALL must pass, else NO-GO): every request completed with no PERSISTENT technical failure
      (a transport error that succeeded on retry is not persistent), no duplicate keys, no hash/prompt mismatch,
      no visible thinking, deterministic repeat identical, resume generates nothing, final checkpoint + state,
      canary (real model only)
  truncation checks (ALL must pass, else NO-GO): no response hit max_new_tokens; no response > headroom_max_fraction
      (90%) of max_new_tokens
  parsing: unparsed = responses without an unambiguous numeric answer
      GO      unparsed <= go_max_unparsed      (Stage 1: >= 19/20 parsed)
      REVIEW  unparsed <= review_max_unparsed  (Stage 1: 18/20 parsed) and everything else passes
      NO-GO   otherwise
Unparseable outputs are observed outcomes; they are never regenerated.
"""


def decide(technical, truncation, n, n_parsed, go_max_unparsed, review_max_unparsed):
    unparsed = n - n_parsed
    if not all(technical.values()):
        dec, why = "NO-GO", "technical check failed: " + ", ".join(k for k, v in technical.items() if not v)
    elif not all(truncation.values()):
        dec, why = "NO-GO", "truncation / headroom check failed: " + ", ".join(k for k, v in truncation.items() if not v)
    elif unparsed <= go_max_unparsed:
        dec, why = "GO", f"{n_parsed}/{n} parsed"
    elif unparsed <= review_max_unparsed:
        dec, why = "REVIEW", f"{n_parsed}/{n} parsed - researcher review of the unparsed outputs required"
    else:
        dec, why = "NO-GO", f"only {n_parsed}/{n} parsed"
    return {"decision": dec, "reason": why, "n": n, "n_parsed": n_parsed, "n_unparsed": unparsed,
            "go_max_unparsed": go_max_unparsed, "review_max_unparsed": review_max_unparsed}


EXIT_CODES = {"GO": 0, "NO-GO": 2, "REVIEW": 4}


A7_GATE = "A7_stage2_pipeline_integrity_gate"


def decide_stage2_a7(technical, persistence, n, n_stop, n_stop_parsed, go_max_unparsed, review_max_unparsed):
    """Stage-2 smoke gate after amendment A7 (researcher clarification 2026-10-14, before any full Stage-2 run).

    A response that reaches the frozen max_new_tokens cap (finish_reason "length") is genuine model behaviour, NOT a
    technical smoke failure, provided the request completed normally and the output was correctly persisted; it stays
    a failed repair if no valid final answer parses (scorer unchanged). Truncation / headroom are therefore reported
    descriptively and never decide the gate. The gate decides on pipeline / integrity criteria only:
      technical   (ALL must pass, else NO-GO) - the unchanged Stage-2 technical checks
      persistence (ALL must pass, else NO-GO) - every output persisted with matching text sha, a recorded finish
                  reason (stop | length) and token count, no output over the cap, a cap hit exactly at the cap,
                  a parse status recorded for every output, effective cap = the frozen 2048
      parser/format pipeline on the responses that FINISHED NORMALLY (finish_reason "stop"; cap-hit responses are
                  excluded because they are model behaviour): unparsed <= go_max -> GO; <= review_max -> REVIEW;
                  otherwise NO-GO (same thresholds as before)."""
    unparsed = n_stop - n_stop_parsed
    if not all(technical.values()):
        dec, why = "NO-GO", "technical check failed: " + ", ".join(k for k, v in technical.items() if not v)
    elif not all(persistence.values()):
        dec, why = "NO-GO", "persistence / integrity check failed: " + ", ".join(k for k, v in persistence.items() if not v)
    elif unparsed <= go_max_unparsed:
        dec, why = "GO", f"pipeline/integrity checks passed; {n_stop_parsed}/{n_stop} normally finished responses parsed"
    elif unparsed <= review_max_unparsed:
        dec, why = "REVIEW", (f"{n_stop_parsed}/{n_stop} normally finished responses parsed - researcher review of the "
                              f"unparsed normally finished outputs required")
    else:
        dec, why = "NO-GO", f"only {n_stop_parsed}/{n_stop} normally finished responses parsed (parser/format pipeline)"
    return {"decision": dec, "reason": why, "gate_version": A7_GATE, "n": n, "n_finished_normally": n_stop,
            "n_finished_normally_parsed": n_stop_parsed, "n_finished_normally_unparsed": unparsed,
            "go_max_unparsed": go_max_unparsed, "review_max_unparsed": review_max_unparsed}
