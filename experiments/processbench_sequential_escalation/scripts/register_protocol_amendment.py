"""Register a protocol amendment (researcher-approved pre-full-run smoke-test correction). Run once per amendment,
locally, BEFORE any full Stage-1 run.

    python scripts/register_protocol_amendment.py --amendment-id A2 \
        --trigger-archive <path>/stage_b_smoke2_RESULTS.zip --approved-by "<researcher>"
    (A1 was registered with --smoke1-archive <path>/stage_b_smoke1_RESULTS.zip; that flag is kept as an alias)

1. Verifies the trigger smoke archive against configs/stage_b.json -> protocol_amendments[<id>].trigger_archive_sha256
   and copies it BYTE-FOR-BYTE to data/smoke_records/<smoke label>/ (it is never altered, re-parsed or rescored; it
   stays the record that triggered the amendment). Its smoke_gate.json is only read, to quote the trigger, and its
   raw rows are only read for their token counts / finish reasons. Every earlier smoke record must still match
   FROZEN_HASHES.json byte-for-byte.
2. Takes the config / parser versions that RAN the trigger smoke from the archive's own bundle_manifest.json (old
   sha256) and registers the amended files in FROZEN_HASHES.json, logged old -> new with the amendment reason. A
   protocol file the amendment does not name (e.g. src/parser.py for A2) must be byte-identical to the version that
   ran. Runners verify both files from then on (common.AMENDED_PROTOCOL_FILES).
3. APPENDS the amendment to data/manifests/PROTOCOL_AMENDMENTS.json (also registered). Earlier amendment records are
   never rewritten.
A researcher-decision amendment without a trigger archive (trigger_archive null, e.g. A3) is registered with
--no-trigger: old = the currently registered config sha, new code files named in the amendment are registered too.
A reviewer replacement triggered by a real A3 reviewer smoke (trigger_kind "review_smoke", e.g. A4) is registered with
--trigger-archive <path>/stage_b_llm_review_smoke_RESULTS.zip: the archive is preserved byte-for-byte under
data/smoke_records/<label>/ and the review requests are rebuilt only if their user prompts stay byte-identical.
A reviewer STOP triggered by a real reviewer smoke (trigger_kind "review_smoke_stop", e.g. A5: LLM reviewer -> human
reviewer) preserves the archive the same way, rebuilds nothing, keeps the LLM-review code + requests byte-identical
and registers the config + src/human_review.py.
A Stage-2 smoke-gate clarification (trigger_kind "stage2_smoke_gate_clarification", e.g. A7) is registered with
--trigger-archive <path>/stage_b_diag_headroom_RESULTS.zip: the DIAGNOSTIC-ONLY archive is preserved byte-for-byte
under data/smoke_records/<label>/ with data/manifests/DIAG_HEADROOM_V11_RECORD.json (quoted; never scored or
substituted); requests / order / smoke selection / parser / scorer must equal what ran in that bundle, cap stays 2048.
Refuses if any real model run exists locally. Re-running an already registered amendment is a no-op check.
"""
import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

import _bootstrap  # noqa: F401
from src import common as C
from src import parser


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trigger-archive", "--smoke1-archive", dest="trigger_archive")
    ap.add_argument("--no-trigger", action="store_true", help="amendment has no trigger archive (researcher decision)")
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--amendment-id", default="A1")
    a = ap.parse_args()
    if C.real_runs_exist():
        raise SystemExit("STOP: real model runs exist; protocol amendments are only registered before any full run")
    cfg = C.load_config()
    am = next((x for x in cfg.get("protocol_amendments", []) if x["id"] == a.amendment_id), None)
    if am is None:
        raise SystemExit(f"STOP: amendment {a.amendment_id} is not in configs/stage_b.json")
    if cfg["parser"]["version"] != parser.parser_version():
        raise SystemExit("STOP: configs/stage_b.json parser.version != src/parser.py")
    if am.get("trigger_kind") == "researcher_decision_pass2_waiver":
        if not a.no_trigger:
            raise SystemExit(f"STOP: {am['id']} has no trigger archive; pass --no-trigger")
        return register_pass2_waiver(am, cfg, a.approved_by)
    if am.get("trigger_archive") is None:
        if not a.no_trigger:
            raise SystemExit(f"STOP: {am['id']} has no trigger archive; pass --no-trigger")
        return register_without_trigger(am, cfg, a.approved_by)
    if not a.trigger_archive:
        raise SystemExit("STOP: --trigger-archive is required")
    if am.get("trigger_kind") == "review_smoke":
        return register_review_smoke_trigger(am, cfg, a.approved_by, Path(a.trigger_archive))
    if am.get("trigger_kind") == "review_smoke_stop":
        return register_review_stop_trigger(am, cfg, a.approved_by, Path(a.trigger_archive))
    if am.get("trigger_kind") == "stage2_smoke_gate_clarification":
        return register_smoke_gate_clarification(am, cfg, a.approved_by, Path(a.trigger_archive))
    label = am.get("trigger_smoke_label", "smoke1")

    src = Path(a.trigger_archive)
    got = C.sha256_file(src)
    if got != am["trigger_archive_sha256"]:
        raise SystemExit(f"STOP: {label} archive sha256 {got} != recorded {am['trigger_archive_sha256']}")

    existing = C.read_json(C.PROTOCOL_AMENDMENTS) if C.PROTOCOL_AMENDMENTS.exists() else None
    prior = [x for x in (existing or {}).get("amendments", []) if x["id"] == am["id"]]
    if prior:
        if prior[0]["trigger_archive_sha256"] != got:
            raise SystemExit(f"STOP: {am['id']} is already registered with a different trigger archive")
        print(f"amendment {am['id']} already registered (records are never rewritten)")
        return 0

    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    for r, h in frozen.items():
        if r.startswith(C.rel(C.SMOKE_RECORDS) + "/") and C.sha256_file(C.STAGE_ROOT / r) != h:
            raise SystemExit(f"STOP: preserved smoke record {r} no longer matches its registered sha256")

    dest = C.SMOKE_RECORDS / label / am["trigger_archive"]
    if dest.exists():
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: {dest} exists with different content; it is never overwritten")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: copy of the {label} archive is not byte-identical")
    (dest.parent / (dest.name + ".sha256.txt")).write_text(f"{got}  {dest.name}\n", encoding="utf-8")

    run_dir = f"{label}_stage1_qwen"
    with zipfile.ZipFile(dest) as z:
        gate = json.loads(z.read(f"{run_dir}/smoke_gate.json"))
        bundle = json.loads(z.read("bundle_manifest.json"))
        rows = [json.loads(l) for l in z.read(f"{run_dir}/raw_outputs.jsonl").decode("utf-8").splitlines() if l.strip()]
    ran = {r: bundle["files"].get(f"stage_b/{r}") for r in C.AMENDED_PROTOCOL_FILES}
    after = {r: C.sha256_file(C.STAGE_ROOT / r) for r in C.AMENDED_PROTOCOL_FILES}
    if not any("parser" in ch for ch in am["changes"]) and after["src/parser.py"] != ran["src/parser.py"]:
        raise SystemExit(f"STOP: {am['id']} does not amend the parser, but src/parser.py differs from the version that "
                         f"ran {label}")
    limit = gate["max_new_tokens"]
    over = [{"request_key": r["request_key"], "new_tokens": r["model_metadata"]["response"]["new_tokens"],
             "finish_reason": r["model_metadata"]["response"].get("finish_reason")} for r in rows
            if r["model_metadata"]["response"]["new_tokens"] > gate["headroom_limit_tokens"]]

    new_limit = cfg["solver"]["stage_b_overrides"]["max_new_tokens"]
    entry = {
        **{k: v for k, v in am.items() if not k.startswith("_")},
        "approved_by": a.approved_by, "registered": C.now(),
        "trigger_record": C.rel(dest),
        "trigger_smoke_gate_quoted": {k: gate.get(k) for k in (
            "decision", "reason", "n", "n_parsed", "numeric_parse_status", "unparsed_request_keys", "truncated",
            "max_new_tokens", "headroom_limit_tokens", "generated_tokens_max", "transport_failures_retried",
            "technical_checks", "truncation_checks")},
        "trigger_responses_over_headroom": over,
        f"{label}_bundle": {"bundle_version": bundle.get("bundle_version"),
                            "code_fingerprint": bundle.get("code_fingerprint")},
        "files_before_sha256": ran,
        "files_after_sha256": after,
        "max_new_tokens_before": limit,
        "max_new_tokens_after": new_limit,
        "headroom_limit_tokens_after": cfg["stage1"]["smoke"]["headroom_max_fraction"] * new_limit,
        "parser_version_after": parser.parser_version(),
    }
    record = existing or {"note": "Researcher-approved protocol amendments made BEFORE any full Stage-B run. Each is "
                                  "triggered by a preserved real smoke record; that record is never altered or "
                                  "rescored."}
    record["amendments"] = record.get("amendments", []) + [entry]
    C.write_json(C.PROTOCOL_AMENDMENTS, record)
    reason = (f"protocol amendment {am['id']} (researcher-approved pre-full-run smoke-test correction after {label} "
              f"NO-GO): " + "; ".join(am["changes"]))
    entries = dict(after)
    entries[C.rel(C.PROTOCOL_AMENDMENTS)] = C.sha256_file(C.PROTOCOL_AMENDMENTS)
    entries[C.rel(dest)] = got
    C.update_frozen_hashes(entries, reason, previous=ran)
    print(json.dumps({"registered": am["id"], f"{label}_record": C.rel(dest), "files_before": ran,
                      "files_after": after, "max_new_tokens": [limit, new_limit],
                      "responses_over_headroom": over}, indent=1))
    return 0


def register_without_trigger(am, cfg, approved_by, extra_files=("src/llm_review.py", "src/reviewer_loader.py",
                                                                  "src/llm_review_runner.py")):
    existing = C.read_json(C.PROTOCOL_AMENDMENTS)
    if any(x["id"] == am["id"] for x in existing["amendments"]):
        print(f"amendment {am['id']} already registered (records are never rewritten)")
        return 0
    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    before = {r: frozen[r] for r in C.AMENDED_PROTOCOL_FILES}
    after = {r: C.sha256_file(C.STAGE_ROOT / r) for r in C.AMENDED_PROTOCOL_FILES}
    if not any("parser" in ch for ch in am["changes"]) and after["src/parser.py"] != before["src/parser.py"]:
        raise SystemExit(f"STOP: {am['id']} does not amend the parser, but src/parser.py changed")
    new_files = {r: C.sha256_file(C.STAGE_ROOT / r) for r in extra_files}
    entry = {**{k: v for k, v in am.items() if not k.startswith("_")}, "approved_by": approved_by,
             "registered": C.now(), "files_before_sha256": before, "files_after_sha256": after,
             "new_files_sha256": new_files, "reviewer": cfg["llm_review"]["reviewer"],
             "provisional_statement": cfg["llm_review"]["provisional_statement"]}
    existing["amendments"].append(entry)
    C.write_json(C.PROTOCOL_AMENDMENTS, existing)
    reason = f"protocol amendment {am['id']} (researcher decision, pre-Stage-2): " + "; ".join(am["changes"])
    entries = {**after, **new_files, C.rel(C.PROTOCOL_AMENDMENTS): C.sha256_file(C.PROTOCOL_AMENDMENTS)}
    C.update_frozen_hashes(entries, reason)
    print(json.dumps({"registered": am["id"], "files_before": before, "files_after": after,
                      "new_files": new_files}, indent=1))
    return 0


REQUEST_FIELDS_FIXED = ("request_key", "stage", "review_pass", "review_index", "item_id", "domain", "prompt",
                        "prompt_sha256", "prompt_chars")


def register_review_smoke_trigger(am, cfg, approved_by, src):
    """Reviewer replacement triggered by a real A3 reviewer smoke (e.g. A4). The smoke archive is copied byte-for-byte
    to data/smoke_records/<label>/ (never altered or rescored; its gate + reviewer settings are only quoted). The review
    requests are rebuilt ONLY because rendered_sha256 includes the system prompt: every other request field, the
    order, the items and the smoke selection must be byte-identical to the frozen version, else STOP."""
    import importlib.util
    from src import llm_review_runner as lrr
    label = am["trigger_smoke_label"]
    got = C.sha256_file(src)
    if got != am["trigger_archive_sha256"]:
        raise SystemExit(f"STOP: {label} archive sha256 {got} != recorded {am['trigger_archive_sha256']}")
    existing = C.read_json(C.PROTOCOL_AMENDMENTS)
    prior = [x for x in existing["amendments"] if x["id"] == am["id"]]
    if prior:
        if prior[0]["trigger_archive_sha256"] != got:
            raise SystemExit(f"STOP: {am['id']} is already registered with a different trigger archive")
        print(f"amendment {am['id']} already registered (records are never rewritten)")
        return 0
    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    for r, h in frozen.items():
        if r.startswith(C.rel(C.SMOKE_RECORDS) + "/") and C.sha256_file(C.STAGE_ROOT / r) != h:
            raise SystemExit(f"STOP: preserved smoke record {r} no longer matches its registered sha256")
    data = lrr.read_archive(src)
    gate = json.loads(data[next(n for n in data if n.endswith("/review_smoke_gate.json"))])
    settings = json.loads(data[next(n for n in data if n.endswith("/reviewer_effective_settings.json"))])
    for rel_ in ("data/processed/llm_review/pass1_requests.jsonl", "data/processed/llm_review/pass2_requests.jsonl"):
        if C.sha256_bytes(data[f"frozen_inputs/{rel_}"]) != frozen[rel_]:
            raise SystemExit(f"STOP: the trigger smoke did not run the registered {rel_}")
    dest = C.SMOKE_RECORDS / label / am["trigger_archive"]
    if dest.exists():
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: {dest} exists with different content; it is never overwritten")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: copy of the {label} archive is not byte-identical")
    (dest.parent / (dest.name + ".sha256.txt")).write_text(f"{got}  {dest.name}\n", encoding="utf-8")

    code = list(lrr.CODE_FILES)
    req_files = [C.rel(p) for p in lrr.FROZEN_FILES]
    before = {r: frozen[r] for r in C.AMENDED_PROTOCOL_FILES + code + req_files}
    if C.sha256_file(C.STAGE_ROOT / "src/parser.py") != before["src/parser.py"]:
        raise SystemExit(f"STOP: {am['id']} does not amend the Stage-B answer parser, but src/parser.py changed")
    spec = importlib.util.spec_from_file_location("build14", C.STAGE_ROOT / "scripts" / "14_build_llm_review_requests.py")
    b14 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b14)
    p1, p2, order, man = b14.build(cfg)
    old = {p: C.read_jsonl(lrr.PASS_FILES[p]) for p in (1, 2)}
    old_man = C.read_json(lrr.REQUEST_MANIFEST)
    for p, new in ((1, p1), (2, p2)):
        if [{k: r[k] for k in REQUEST_FIELDS_FIXED} for r in new] != \
                [{k: r[k] for k in REQUEST_FIELDS_FIXED} for r in old[p]]:
            raise SystemExit(f"STOP: Pass-{p} user prompts / items / order would change; {am['id']} only replaces "
                             f"the reviewer and the system prompt")
    if C.dumps(order) != (lrr.ORDER_FILE).read_text(encoding="utf-8"):
        raise SystemExit("STOP: review order would change")
    for k in ("failures_sha256", "items_sha256", "n_items", "n_requests", "pass1_question_sha256",
              "pass2_question_sha256", "pass1_prompts_sha256", "pass2_prompts_sha256", "smoke_item_ids",
              "max_prompt_chars", "pass1_fields_shown", "pass2_fields_shown"):
        if man[k] != old_man[k]:
            raise SystemExit(f"STOP: request manifest field {k} would change")
    entries = {}
    for pass_no, reqs in ((1, p1), (2, p2)):
        entries[C.rel(lrr.PASS_FILES[pass_no])] = C.write_jsonl(lrr.PASS_FILES[pass_no], reqs)
    entries[C.rel(lrr.ORDER_FILE)] = C.write_json(lrr.ORDER_FILE, order)
    man["files_sha256"] = dict(entries)
    man["created"] = C.now()
    man["supersedes"] = {"system_prompt_sha256": old_man["system_prompt_sha256"],
                         "prompt_version": old_man["prompt_version"], "reviewer": old_man["reviewer"],
                         "manifest_sha256": frozen[C.rel(lrr.REQUEST_MANIFEST)]}
    entries[C.rel(lrr.REQUEST_MANIFEST)] = C.write_json(lrr.REQUEST_MANIFEST, man)
    after = {r: C.sha256_file(C.STAGE_ROOT / r) for r in C.AMENDED_PROTOCOL_FILES + code + req_files}
    entry = {**{k: v for k, v in am.items() if not k.startswith("_")}, "approved_by": approved_by,
             "registered": C.now(), "trigger_record": C.rel(dest),
             "trigger_smoke_gate_quoted": {k: gate.get(k) for k in (
                 "decision", "reason", "checks", "deterministic_repeat_identical_raw_text",
                 "max_prompt_plus_max_new_tokens", "judgments", "n_generations")},
             "trigger_reviewer_settings_quoted": settings,
             "files_before_sha256": before, "files_after_sha256": after,
             "request_rebuild": "rendered_sha256 only (system prompt); prompt / prompt_sha256 / items / order / "
                                "smoke selection byte-identical (checked before writing)",
             "reviewer_before": {"model": settings["model"], "revision": settings["requested_revision"],
                                 "max_new_tokens": settings["max_new_tokens"],
                                 "max_context_tokens": settings["max_context_tokens"],
                                 "system_prompt_sha256": settings["system_prompt_sha256"]},
             "reviewer_after": cfg["llm_review"]["reviewer"],
             "system_prompt_sha256_after": man["system_prompt_sha256"],
             "provisional_statement": cfg["llm_review"]["provisional_statement"]}
    existing["amendments"].append(entry)
    C.write_json(C.PROTOCOL_AMENDMENTS, existing)
    reason = (f"protocol amendment {am['id']} (researcher-approved pre-full-review reviewer replacement after "
              f"{label} NO-GO): " + "; ".join(am["changes"]))
    reg = {**after, C.rel(C.PROTOCOL_AMENDMENTS): C.sha256_file(C.PROTOCOL_AMENDMENTS), C.rel(dest): got}
    C.update_frozen_hashes(reg, reason)
    print(json.dumps({"registered": am["id"], f"{label}_record": C.rel(dest),
                      "changed": {r: [before[r], after[r]] for r in after if before[r] != after[r]},
                      "unchanged": sorted(r for r in after if before[r] == after[r])}, indent=1))
    return 0


HUMAN_REVIEW_CODE = ["src/human_review.py"]


def register_review_stop_trigger(am, cfg, approved_by, src):
    """LLM reviewer STOPPED after a real reviewer smoke (A5). The smoke archive is copied byte-for-byte to
    data/smoke_records/<label>/ (a NO-GO record: never altered, re-parsed or rescored; its gate + reviewer settings are
    only quoted). Nothing is rebuilt: the LLM-review code, requests, order and manifest must stay byte-identical."""
    from src import llm_review_runner as lrr
    label = am["trigger_smoke_label"]
    got = C.sha256_file(src)
    if got != am["trigger_archive_sha256"]:
        raise SystemExit(f"STOP: {label} archive sha256 {got} != recorded {am['trigger_archive_sha256']}")
    existing = C.read_json(C.PROTOCOL_AMENDMENTS)
    prior = [x for x in existing["amendments"] if x["id"] == am["id"]]
    if prior:
        if prior[0]["trigger_archive_sha256"] != got:
            raise SystemExit(f"STOP: {am['id']} is already registered with a different trigger archive")
        print(f"amendment {am['id']} already registered (records are never rewritten)")
        return 0
    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    for r, h in frozen.items():
        if r.startswith(C.rel(C.SMOKE_RECORDS) + "/") and C.sha256_file(C.STAGE_ROOT / r) != h:
            raise SystemExit(f"STOP: preserved smoke record {r} no longer matches its registered sha256")
    data = lrr.read_archive(src)
    gate = json.loads(data[next(n for n in data if n.endswith("/review_smoke_gate.json"))])
    settings = json.loads(data[next(n for n in data if n.endswith("/reviewer_effective_settings.json"))])
    req_files = [C.rel(p) for p in lrr.FROZEN_FILES]
    for rel_ in req_files:
        if f"frozen_inputs/{rel_}" in data and C.sha256_bytes(data[f"frozen_inputs/{rel_}"]) != frozen[rel_]:
            raise SystemExit(f"STOP: the trigger smoke did not run the registered {rel_}")
    unchanged = list(lrr.CODE_FILES) + req_files + ["src/parser.py"]
    for r in unchanged:
        if C.sha256_file(C.STAGE_ROOT / r) != frozen[r]:
            raise SystemExit(f"STOP: {am['id']} changes nothing in {r}, but it differs from the registered version")
    dest = C.SMOKE_RECORDS / label / am["trigger_archive"]
    if dest.exists():
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: {dest} exists with different content; it is never overwritten")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: copy of the {label} archive is not byte-identical")
    (dest.parent / (dest.name + ".sha256.txt")).write_text(f"{got}  {dest.name}\n", encoding="utf-8")
    before = {r: frozen[r] for r in C.AMENDED_PROTOCOL_FILES}
    after = {r: C.sha256_file(C.STAGE_ROOT / r) for r in C.AMENDED_PROTOCOL_FILES}
    new_files = {r: C.sha256_file(C.STAGE_ROOT / r) for r in HUMAN_REVIEW_CODE}
    entry = {**{k: v for k, v in am.items() if not k.startswith("_")}, "approved_by": approved_by,
             "registered": C.now(), "trigger_record": C.rel(dest),
             "trigger_smoke_gate_quoted": {k: gate.get(k) for k in (
                 "decision", "reason", "checks", "deterministic_repeat_identical_raw_text",
                 "max_prompt_plus_max_new_tokens", "judgments", "n_generations")},
             "trigger_reviewer_settings_quoted": settings,
             "files_before_sha256": before, "files_after_sha256": after, "new_files_sha256": new_files,
             "unchanged_files_sha256": {r: frozen[r] for r in unchanged},
             "llm_reviewer_stopped": cfg["llm_review"]["reviewer"]}
    existing["amendments"].append(entry)
    C.write_json(C.PROTOCOL_AMENDMENTS, existing)
    reason = (f"protocol amendment {am['id']} (researcher decision after {label} NO-GO: LLM reviewer stopped, "
              f"blinded human review): " + "; ".join(am["changes"]))
    reg = {**after, **new_files, C.rel(C.PROTOCOL_AMENDMENTS): C.sha256_file(C.PROTOCOL_AMENDMENTS), C.rel(dest): got}
    C.update_frozen_hashes(reg, reason)
    print(json.dumps({"registered": am["id"], f"{label}_record": C.rel(dest),
                      "changed": {r: [before[r], after[r]] for r in after if before[r] != after[r]},
                      "new_files": new_files, "unchanged": sorted(unchanged)}, indent=1))
    return 0


A6_CODE = ["src/annotation_review.py", "src/analysis.py", "scripts/21_freeze_pass1_only_review.py",
           "scripts/08_build_stage2_requests.py"]
A6_PASS1_FROZEN = ["data/processed/human_review/pass1_view.json", "data/processed/human_review/pass1_log.jsonl",
                   "data/processed/human_review/pass1_final.json", "data/processed/human_review/pass1_mapping.json",
                   "data/manifests/HUMAN_REVIEW_EXPORT.json", "data/manifests/HUMAN_REVIEW_PASS1_FROZEN.json",
                   "data/processed/human_review/pass2_view.json", "data/manifests/HUMAN_REVIEW_PASS2_EXPORT.json",
                   "src/human_review.py"]


def register_pass2_waiver(am, cfg, approved_by):
    """A6: researcher decision after the frozen human Pass 1 - separate Pass 2 waived. No trigger archive. Every
    frozen Pass-1 file must be byte-identical to its registered sha256 (it is re-verified, never rewritten); the
    interpretation-rule lock must still match (primary endpoint unchanged)."""
    from src import stage2_runner as s2r
    existing = C.read_json(C.PROTOCOL_AMENDMENTS)
    if any(x["id"] == am["id"] for x in existing["amendments"]):
        print(f"amendment {am['id']} already registered (records are never rewritten)")
        return 0
    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    pass1 = C.verify_frozen(A6_PASS1_FROZEN)
    if C.STAGE2_REQUESTS.exists() or (C.DATA_PROC / "annotation_review" / "annotation_review_form_FILLED.csv").exists():
        raise SystemExit("STOP: decisions / Stage-2 requests already exist; A6 must precede them")
    if not s2r.rule_is_frozen(cfg):
        raise SystemExit("STOP: the interpretation-rule lock no longer matches; A6 must not change the endpoint/rule")
    before = {r: frozen[r] for r in C.AMENDED_PROTOCOL_FILES}
    after = {r: C.sha256_file(C.STAGE_ROOT / r) for r in C.AMENDED_PROTOCOL_FILES}
    if after["src/parser.py"] != before["src/parser.py"]:
        raise SystemExit(f"STOP: {am['id']} does not amend the parser, but src/parser.py changed")
    code = {r: C.sha256_file(C.STAGE_ROOT / r) for r in A6_CODE}
    entry = {**{k: v for k, v in am.items() if not k.startswith("_")}, "approved_by": approved_by,
             "registered": C.now(), "files_before_sha256": before, "files_after_sha256": after,
             "code_files_sha256": code, "pass1_files_sha256_unchanged": pass1,
             "interpretation_rule_sha256": C.read_json(C.RULE_LOCK)["rule_sha256"],
             "pass2_waiver": cfg["annotation_review"]["pass2_waiver_A6"]}
    existing["amendments"].append(entry)
    C.write_json(C.PROTOCOL_AMENDMENTS, existing)
    reason = f"protocol amendment {am['id']} (researcher decision, pre-Stage-2): " + "; ".join(am["changes"])
    C.update_frozen_hashes({**after, **code, C.rel(C.PROTOCOL_AMENDMENTS): C.sha256_file(C.PROTOCOL_AMENDMENTS)},
                           reason)
    print(json.dumps({"registered": am["id"], "files_before": before, "files_after": after, "code": code,
                      "pass1_unchanged": pass1}, indent=1))
    return 0


A7_UNCHANGED = ["src/parser.py", "src/scorer.py", "data/processed/stage2_requests.jsonl",
                "data/processed/stage2_execution_order.json", "data/manifests/stage2_smoke_selection.json",
                "data/manifests/stage2_request_manifest.json", "data/processed/annotation_review_decisions.json",
                "data/manifests/INTERPRETATION_RULE_FROZEN.json", "scripts/22_diag_headroom.py"]
A7_CODE = ["src/smoke_gate.py", "scripts/09_stage2_smoke_test.py", "src/stage2_runner.py", "scripts/10_run_stage2.py"]
DIAG_RECORD = "data/manifests/DIAG_HEADROOM_V11_RECORD.json"
DIAG_DIR = "diag_headroom_sb_math-355_VV_4096"


def register_smoke_gate_clarification(am, cfg, approved_by, src):
    """A7: Stage-2 smoke-gate clarification after the v9 NO-GO + the v11 DIAGNOSTIC-ONLY headroom generation. The
    diagnostic archive is copied byte-for-byte to data/smoke_records/<label>/ and quoted in DIAG_RECORD (DIAGNOSTIC
    ONLY: not scored, never substituted). The v9 NO-GO record is re-verified, never rescored. Every Stage-2 input,
    the parser, the scorer and the 2048 cap must equal what ran in the diagnostic's bundle; the rule lock must hold."""
    from src import solver_loader as sl
    from src import stage2_runner as s2r
    label = am["trigger_smoke_label"]
    got = C.sha256_file(src)
    if got != am["trigger_archive_sha256"]:
        raise SystemExit(f"STOP: {label} archive sha256 {got} != recorded {am['trigger_archive_sha256']}")
    existing = C.read_json(C.PROTOCOL_AMENDMENTS)
    prior = [x for x in existing["amendments"] if x["id"] == am["id"]]
    if prior:
        if prior[0]["trigger_archive_sha256"] != got:
            raise SystemExit(f"STOP: {am['id']} is already registered with a different trigger archive")
        print(f"amendment {am['id']} already registered (records are never rewritten)")
        return 0
    frozen = C.read_json(C.FROZEN_HASHES)["files"]
    for r, h in frozen.items():
        if r.startswith(C.rel(C.SMOKE_RECORDS) + "/") and C.sha256_file(C.STAGE_ROOT / r) != h:
            raise SystemExit(f"STOP: preserved smoke record {r} no longer matches its registered sha256")
    C.verify_frozen(["data/manifests/STAGE2_SMOKE_V9_NOGO.json"])
    if not s2r.rule_is_frozen(cfg):
        raise SystemExit("STOP: the interpretation-rule lock no longer matches; A7 must not change the endpoint/rule")
    if sl.solver_config(cfg)[0]["max_new_tokens"] != 2048:
        raise SystemExit("STOP: A7 keeps max_new_tokens = 2048")
    C.verify_frozen([r for r in A7_UNCHANGED if r.startswith("data/")])  # 244 requests / order / selection / lock
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        raw_b, diag_b = z.read(f"{DIAG_DIR}/raw_output.json"), z.read(f"{DIAG_DIR}/diagnostic.json")
        bundle = json.loads(z.read("bundle_manifest.json"))
    diag, raw = json.loads(diag_b), json.loads(raw_b)
    raw_sha, diag_sha = C.sha256_bytes(raw_b), C.sha256_bytes(diag_b)
    if diag.get("TEST_ONLY") is not False or "DIAGNOSTIC ONLY" not in diag.get("LABEL", ""):
        raise SystemExit("STOP: not a real DIAGNOSTIC-ONLY result")
    if raw_sha != diag["raw_output_sha256"] or C.sha256_text(raw["raw_response"]) != raw["raw_response_sha256"]:
        raise SystemExit("STOP: diagnostic archive is internally inconsistent")
    now_sha = {r: C.sha256_file(C.STAGE_ROOT / r) for r in A7_UNCHANGED}
    ran = {r: bundle["files"].get(f"stage_b/{r}") for r in A7_UNCHANGED}
    if now_sha != ran:
        raise SystemExit(f"STOP: A7 changes none of these, but they differ from the bundle that ran the diagnostic: "
                         f"{[r for r in A7_UNCHANGED if now_sha[r] != ran[r]]}")
    dest = C.SMOKE_RECORDS / label / am["trigger_archive"]
    if dest.exists():
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: {dest} exists with different content; it is never overwritten")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if C.sha256_file(dest) != got:
            raise SystemExit(f"STOP: copy of the {label} archive is not byte-identical")
    (dest.parent / (dest.name + ".sha256.txt")).write_text(f"{got}  {dest.name}\n", encoding="utf-8")
    quoted = {k: diag[k] for k in ("request_key", "finish_reason", "generated_tokens", "max_new_tokens",
                                   "prompt_tokens", "hit_cap", "final_answer_label_present", "numeric_parse_status",
                                   "prefix_identical_to_2048_smoke_output", "repetition_full",
                                   "repetition_beyond_2048_output", "raw_output_sha256")}
    rec = {"LABEL": "DIAGNOSTIC ONLY - follow-up to the v9 Stage-2 smoke NO-GO; not Stage-2 data, not scored, never "
                    "substituted for any Stage-2 output, not a scientific protocol result",
           "recorded": C.now(), "archive": C.rel(dest), "archive_sha256": got, "archive_files": sorted(names),
           "raw_output_json_sha256": raw_sha, "diagnostic_json_sha256": diag_sha,
           "raw_response_sha256": raw["raw_response_sha256"],
           "bundle_version": bundle.get("bundle_version"), "bundle_code_fingerprint": bundle.get("code_fingerprint"),
           "effective_settings": raw["effective_settings"], "solver": raw["solver"], "diagnostic_quoted": quoted,
           "researcher_conclusion_2026_10_14": "the model is genuinely stuck in a repetitive repair loop; increasing "
                                               "Stage-2 max_new_tokens is not justified (A7: cap stays 2048)"}
    C.write_json(C.STAGE_ROOT / DIAG_RECORD, rec)
    rec_sha = C.sha256_file(C.STAGE_ROOT / DIAG_RECORD)
    before = {r: frozen.get(r) for r in C.AMENDED_PROTOCOL_FILES + A7_CODE}
    after = {r: C.sha256_file(C.STAGE_ROOT / r) for r in C.AMENDED_PROTOCOL_FILES + A7_CODE}
    if after["src/parser.py"] != before["src/parser.py"]:
        raise SystemExit(f"STOP: {am['id']} does not amend the parser, but src/parser.py changed")
    entry = {**{k: v for k, v in am.items() if not k.startswith("_")}, "approved_by": approved_by,
             "registered": C.now(), "trigger_record": C.rel(dest), "diagnostic_record": DIAG_RECORD,
             "diagnostic_record_sha256": rec_sha, "diagnostic_quoted": quoted,
             "files_before_sha256": before, "files_after_sha256": after,
             "unchanged_sha256_equal_to_bundle_that_ran": now_sha,
             "max_new_tokens": 2048, "smoke_gate": cfg["stage2"]["smoke"]["gate"],
             "interpretation_rule_sha256": C.read_json(C.RULE_LOCK)["rule_sha256"]}
    existing["amendments"].append(entry)
    C.write_json(C.PROTOCOL_AMENDMENTS, existing)
    reason = (f"protocol amendment {am['id']} (researcher-approved pre-full-Stage-2 smoke-gate clarification after the "
              f"v9 NO-GO + DIAGNOSTIC-ONLY headroom result): " + "; ".join(am["changes"]))
    C.update_frozen_hashes({**after, C.rel(C.PROTOCOL_AMENDMENTS): C.sha256_file(C.PROTOCOL_AMENDMENTS),
                            C.rel(dest): got, DIAG_RECORD: rec_sha}, reason)
    print(json.dumps({"registered": am["id"], "diagnostic_archive": [C.rel(dest), got],
                      "diagnostic_record": [DIAG_RECORD, rec_sha],
                      "changed": {r: [before[r], after[r]] for r in after if before[r] != after[r]},
                      "unchanged_vs_bundle_that_ran": sorted(now_sha)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
