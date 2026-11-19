"""Register the researcher-approved Stage-2 smoke GO (amendment A7 gate) - unlocks the full Stage-2 run.

    python scripts/23_register_stage2_smoke_go.py --archive <path>/stage_b_stage2_smoke_RESULTS.zip \
        --sha256 <researcher-verified sha256> --approved-by "<researcher>"
Integrity only: reads smoke_gate.json, run_manifest.json, run_diagnostics.json, diagnostics/determinism.json and
bundle_manifest.json from the archive. scored_outputs.jsonl is never opened and no repair / rescue rate is computed.
1. Verifies the archive sha256 == --sha256, a REAL (not TEST_ONLY) smoke under the A7 gate with decision GO, every
   technical + persistence check true, no audit problems, cap 2048, and that the run used the frozen 244 requests /
   execution order and the frozen smoke selection.
2. Every Stage-2 protocol file in the bundle that ran the smoke must equal the local frozen file (byte-identical).
3. Copies the archive BYTE-FOR-BYTE to data/smoke_records/stage2_smoke_v12_go/ (never overwritten); the v9 NO-GO
   and v11 DIAGNOSTIC-ONLY records are re-verified unchanged.
4. Writes data/manifests/STAGE2_SMOKE_GO.json and registers it + the archive in FROZEN_HASHES.json.
stage2_runner.require_smoke_go() (scripts/10 --solver qwen, notebook MODE "stage2") then accepts ONLY this record.
Refuses if any real model run exists locally. Re-running with the same archive is a no-op check.
"""
import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

import _bootstrap  # noqa: F401
from src import common as C
from src import smoke_gate as sg
from src import solver_loader as sl
from src import stage2_runner as s2r

LABEL = "stage2_smoke_v12_go"
RUN = "smoke_stage2_qwen"
PROTOCOL_FILES = ["configs/stage_b.json", "src/parser.py", "src/scorer.py", "src/smoke_gate.py", "src/analysis.py",
                  "src/runner_core.py", "src/stage2_runner.py", "src/solver_loader.py",
                  "scripts/09_stage2_smoke_test.py", "scripts/10_run_stage2.py",
                  "data/processed/stage2_requests.jsonl", "data/processed/stage2_execution_order.json",
                  "data/manifests/stage2_request_manifest.json", "data/manifests/stage2_smoke_selection.json",
                  "data/processed/annotation_review_decisions.json", "data/manifests/INTERPRETATION_RULE_FROZEN.json"]
PRESERVED = ["data/manifests/STAGE2_SMOKE_V9_NOGO.json", "data/manifests/DIAG_HEADROOM_V11_RECORD.json",
             "data/smoke_records/stage2_smoke_v9/stage_b_stage2_smoke_RESULTS.zip",
             "data/smoke_records/diag_headroom_v11/stage_b_diag_headroom_RESULTS.zip"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", required=True)
    ap.add_argument("--sha256", required=True, help="the archive SHA-256 the researcher verified")
    ap.add_argument("--approved-by", required=True)
    a = ap.parse_args()
    if C.real_runs_exist():
        raise SystemExit("STOP: real model runs exist locally; the smoke GO is registered before the full run")
    src = Path(a.archive)
    got = C.sha256_file(src)
    if got != a.sha256.lower():
        raise SystemExit(f"STOP: archive sha256 {got} != researcher-verified {a.sha256}")
    cfg = C.load_config()
    C.verify_frozen(PRESERVED + C.AMENDED_PROTOCOL_FILES)
    if s2r.SMOKE_GO.exists():
        if C.read_json(s2r.SMOKE_GO)["archive_sha256"] != got:
            raise SystemExit("STOP: a different Stage-2 smoke GO is already registered; it is never overwritten")
        C.verify_frozen([C.rel(s2r.SMOKE_GO)])
        print("Stage-2 smoke GO already registered (records are never rewritten)")
        return 0
    if got == C.read_json(s2r.SMOKE_NOGO_V9)["archive_sha256"]:
        raise SystemExit("STOP: that is the preserved v9 NO-GO archive")
    if not s2r.rule_is_frozen(cfg):
        raise SystemExit("STOP: the interpretation-rule lock no longer matches")
    if sl.solver_config(cfg)[0]["max_new_tokens"] != 2048:
        raise SystemExit("STOP: the frozen cap is 2048")
    by_key, order, hashes, _ = s2r.frozen_inputs(s2r.paths(None))       # 244 requests / order / review lock
    with zipfile.ZipFile(src) as z:
        names = sorted(z.namelist())
        gate = json.loads(z.read(f"{RUN}/smoke_gate.json"))
        man = json.loads(z.read(f"{RUN}/run_manifest.json"))
        diag = json.loads(z.read(f"{RUN}/run_diagnostics.json"))
        det = json.loads(z.read(f"{RUN}/diagnostics/determinism.json"))
        bundle = json.loads(z.read("bundle_manifest.json"))
    sel = C.read_json(s2r.SMOKE_SELECTION)
    bad = []
    if gate.get("TEST_ONLY") is not False or man.get("test_only") is not False:
        bad.append("not a real-model smoke")
    if (gate.get("decision"), gate.get("gate_version"), gate.get("protocol_amendment")) != ("GO", sg.A7_GATE, "A7"):
        bad.append("not a GO under the A7 gate")
    if not all(gate["technical_checks"].values()) or not all(gate["persistence_checks"].values()):
        bad.append("a technical / persistence check failed")
    if gate.get("audit_problems"):
        bad.append("audit problems")
    if (gate.get("max_new_tokens"), man.get("max_new_tokens")) != (2048, 2048):
        bad.append("cap != 2048")
    if (man.get("requests_sha256"), man.get("execution_order_sha256")) != (hashes["requests"], hashes["order"]):
        bad.append("not the frozen requests / execution order")
    if sorted(gate["per_request"]) != sorted(sel["request_keys"]) or gate["n_requests"] != sel["n_requests"]:
        bad.append("not the frozen smoke selection")
    if (diag["completed"], diag["planned"], diag["duplicate_keys"]) != (12, 12, 0):
        bad.append("completion")
    if man.get("model_revision") != cfg["solver"]["model_revision"]:
        bad.append("model revision")
    ran = {r: bundle["files"].get(f"stage_b/{r}") for r in PROTOCOL_FILES}
    now = {r: C.sha256_file(C.STAGE_ROOT / r) for r in PROTOCOL_FILES}
    bad += [f"{r} differs from the version that ran the smoke" for r in PROTOCOL_FILES if ran[r] != now[r]]
    if bad:
        raise SystemExit(f"STOP: smoke archive not registrable as GO: {bad}")

    dest = C.SMOKE_RECORDS / LABEL / "stage_b_stage2_smoke_RESULTS.zip"
    if dest.exists():
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: {dest} exists with different content; it is never overwritten")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if C.sha256_file(dest) != got:
            raise SystemExit("STOP: copy of the smoke archive is not byte-identical")
    (dest.parent / (dest.name + ".sha256.txt")).write_text(f"{got}  {dest.name}\n", encoding="utf-8")
    td = gate["truncation_descriptive"]
    rec = {"artifact": "Stage-2 smoke GO (bundle v12, amendment A7 gate) - official pre-full-run smoke; preserved "
                       "byte-for-byte, never rescored",
           "LABEL": "INTEGRITY ONLY - no repair / rescue rates were computed, displayed or inspected from the smoke",
           "recorded": C.now(), "approved_by": a.approved_by,
           "archive": C.rel(dest), "archive_sha256": got, "archive_files": names,
           "gate_decision": gate["decision"], "gate_version": gate["gate_version"], "gate_reason": gate["reason"],
           "protocol_amendments": man.get("protocol_amendments"),
           "bundle_version": bundle.get("bundle_version"), "bundle_phase": bundle.get("phase"),
           "bundle_code_fingerprint": bundle.get("code_fingerprint"),
           "requests_sha256": man["requests_sha256"], "execution_order_sha256": man["execution_order_sha256"],
           "smoke_selection_sha256": C.sha256_file(s2r.SMOKE_SELECTION), "smoke_items": gate["smoke_items"],
           "model": man.get("model"), "model_revision": man.get("model_revision"),
           "frozen_solver_config_sha256": man.get("frozen_solver_config_sha256"),
           "max_new_tokens": man["max_new_tokens"], "parser_sha256": man.get("parser_sha256"),
           "integrity_findings": {
               "n_requests": gate["n_requests"], "completed": diag["completed"], "planned": diag["planned"],
               "duplicate_keys": diag["duplicate_keys"],
               "transport_failure_records": diag["transport_failure_records"], "retries_used": diag["retries_used"],
               "technical_checks": gate["technical_checks"], "persistence_checks": gate["persistence_checks"],
               "audit_problems": gate["audit_problems"], "visible_think_tags": diag["visible_think_tags"],
               "determinism_repeats": det["repeats"],
               "n_finished_normally": gate["n_finished_normally"],
               "n_finished_normally_parsed": gate["n_finished_normally_parsed"],
               "cap_hits_descriptive_only": {"keys": td["cap_hit_request_keys"], "n": td["n_cap_hit"],
                                             "without_valid_answer": td["cap_hit_without_valid_answer"],
                                             "note": "genuine model behaviour under A7; remain failed repairs"},
               "per_request_finish_tokens_parse_status": gate["per_request"],
               "generated_tokens_max": gate["generated_tokens_max"], "prompt_tokens_max": gate["prompt_tokens_max"]},
           "protocol_files_sha256_equal_to_bundle_that_ran": now,
           "preserved_records_unchanged": {r: C.sha256_file(C.STAGE_ROOT / r) for r in PRESERVED},
           "interpretation_rule_sha256": C.read_json(C.RULE_LOCK)["rule_sha256"],
           "unlocks": "full Stage-2 run (244 frozen requests) - scripts/10_run_stage2.py --solver qwen / notebook "
                      "MODE stage2 with APPROVED_STAGE2_SMOKE_SHA256 == archive_sha256"}
    sha, _ = C.write_frozen(s2r.SMOKE_GO, C.write_json, rec)
    C.update_frozen_hashes({C.rel(s2r.SMOKE_GO): sha, C.rel(dest): got})
    print(json.dumps({"registered": C.rel(s2r.SMOKE_GO), "sha256": sha, "archive": [C.rel(dest), got],
                      "decision": rec["gate_decision"], "bundle": [rec["bundle_version"], rec["bundle_code_fingerprint"]]},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
