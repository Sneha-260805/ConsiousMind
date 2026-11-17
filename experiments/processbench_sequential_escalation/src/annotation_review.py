"""Blinded annotation-fidelity gate between Stage 1 and Stage 2 (researcher decision 2026-10-12).

Order of operations (each step refuses to run unless the previous one is frozen):
  1. Stage-1 failure set frozen + hashed                     (scripts/07_freeze_stage1_failures.py)
  2. EXPORT review packet for the failed items ONLY          (scripts/07a_export_annotation_review.py)
     Shown per item: problem, numbered ORIGINAL solution, gold answer, the original solution's final answer, the
     ProcessBench-annotated first-error step k and the proposed false step f = k-1.
     NOT shown: any Stage-1 model response, Stage-1 answer or Stage-1 outcome type, or anything about Stage 2.
     Items are listed in a seeded random order (not failure order). The packet + blank form are hashed at export.
  3. Reviewer fills the form (one row per item):
       first_error_verdict  CONFIRM | DISAGREE | UNCLEAR   (is Step k really the FIRST erroneous step?)
       reviewer_first_error_step   optional, only for DISAGREE (recorded; never used to re-point a prompt)
       false_step_verdict   CORRECT | NOT_CORRECT | UNCLEAR (is Step f genuinely correct?)
       notes                optional
  4. FREEZE decisions (scripts/07b_freeze_annotation_review.py): the form must be complete and match the packet
     exactly; pre-specified rules (configs/stage_b.json -> annotation_review.decision_rules):
       primary_stage2 = first_error_verdict == CONFIRM        (else excluded from Stage 2 in ALL conditions)
       l_false        = primary_stage2 and false_step_verdict == CORRECT  (else ineligible ONLY for L_false)
     Decisions + filled form are hashed and locked BEFORE any Stage-2 prompt is built. Stage-2 outcomes are never
     used for exclusions (they do not exist yet when this runs).
"""
import csv
import io

from . import common as C
from .dataset_builder import render_trace

FORM_FIELDS = ["item_id", "annotated_first_error_step", "proposed_false_step", "first_error_verdict",
               "reviewer_first_error_step", "false_step_verdict", "notes"]


def packet_order(item_ids, seed):
    return sorted(item_ids, key=lambda i: C.sha256_text(f"{seed}:{i}"))


def build_packet(pool, failures_doc, cfg, test_only):
    items = {i["item_id"]: i for i in pool}
    ids = packet_order(failures_doc["item_ids"], cfg["annotation_review"]["packet_order_seed"])
    head = [f"# Stage B - blinded annotation-fidelity review ({len(ids)} items)",
            "", f"**{C.TEST_ONLY_BANNER}**" if test_only else "", "",
            "For each item decide, using ONLY the problem and the original solution below:",
            "1. **first_error_verdict** - is the annotated step really the FIRST erroneous step? "
            "`CONFIRM` / `DISAGREE` / `UNCLEAR`. If DISAGREE you may write the step you believe is first in "
            "`reviewer_first_error_step` (recorded only; prompts are never re-pointed).",
            "2. **false_step_verdict** - is the proposed false step genuinely correct? `CORRECT` / `NOT_CORRECT` / "
            "`UNCLEAR`.",
            "", "Fill `annotation_review_form.csv` (one row per item, same item_id). Do not edit any other column.",
            "This packet intentionally contains no model outputs and no information about how any model "
            "performed on these items. Items are in random order.", ""]
    body, form = [], []
    for n, iid in enumerate(ids, 1):
        it = items[iid]
        k, f = it["true_error_step"], it["false_location"]["false_step"]
        body += [f"---", f"## {n}. {iid}", "", "**Problem**", "", it["question"], "",
                 "**Original proposed solution**", "", "```", render_trace(it["steps"]), "```", "",
                 f"- Gold answer: **{it['gold_answer']}**  |  original solution's final answer: "
                 f"**{it['original_answer']}**",
                 f"- Annotated first error: **Step {k}** of {it['n_steps']}",
                 f"- Proposed false step (claimed correct): **Step {f}**", ""]
        form.append({"item_id": iid, "annotated_first_error_step": k, "proposed_false_step": f,
                     "first_error_verdict": "", "reviewer_first_error_step": "", "false_step_verdict": "",
                     "notes": ""})
    return "\n".join(head + body) + "\n", form


def form_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FORM_FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def read_form(path):
    text = open(path, encoding="utf-8-sig", newline="").read()
    return list(csv.DictReader(io.StringIO(text)))


def validate_and_decide(form_rows, blank_rows, cfg):
    """-> (decisions, problems). The filled form must match the exported blank form row-for-row."""
    ar = cfg["annotation_review"]
    probs = []
    blank = {r["item_id"]: r for r in blank_rows}
    seen = set()
    decisions = {}
    for r in form_rows:
        iid = (r.get("item_id") or "").strip()
        if iid not in blank:
            probs.append(f"unknown item_id {iid!r}")
            continue
        if iid in seen:
            probs.append(f"duplicate row for {iid}")
        seen.add(iid)
        b = blank[iid]
        for col in ("annotated_first_error_step", "proposed_false_step"):
            if str(r.get(col, "")).strip() != str(b[col]):
                probs.append(f"{iid}: column {col} was edited")
        fe = (r.get("first_error_verdict") or "").strip().upper()
        fs_ = (r.get("false_step_verdict") or "").strip().upper()
        if fe not in ar["first_error_verdicts"]:
            probs.append(f"{iid}: first_error_verdict {fe!r} not in {ar['first_error_verdicts']}")
        if fs_ not in ar["false_step_verdicts"]:
            probs.append(f"{iid}: false_step_verdict {fs_!r} not in {ar['false_step_verdicts']}")
        alt = (r.get("reviewer_first_error_step") or "").strip()
        if alt and not alt.isdigit():
            probs.append(f"{iid}: reviewer_first_error_step must be a step number or blank")
        primary = fe == "CONFIRM"
        l_false = primary and fs_ in ar.get("l_false_eligible_verdicts", ["CORRECT"])
        decisions[iid] = {"item_id": iid, "first_error_verdict": fe, "false_step_verdict": fs_,
                          "reviewer_first_error_step": int(alt) if alt.isdigit() else None,
                          "notes": (r.get("notes") or "").strip(),
                          "primary_stage2": primary, "l_false_eligible": l_false,
                          "false_step_validity_basis": None if not l_false else (
                              "Pass-1-implied false-step validity (A6; not independently Pass-2-validated)"
                              if fs_ == "PASS1_IMPLIED" else "separate false-step review (CORRECT)")}
    missing = set(blank) - seen
    if missing:
        probs.append(f"{len(missing)} items have no row (e.g. {sorted(missing)[:3]})")
    return decisions, probs


def summary(decisions, cfg):
    d = list(decisions.values())
    n_primary = sum(x["primary_stage2"] for x in d)
    need = cfg["annotation_review"]["decision_rules"]["min_primary_items"]
    return {"n_reviewed": len(d), "n_primary_stage2": n_primary,
            "n_excluded_first_error": len(d) - n_primary,
            "n_l_false_eligible": sum(x["l_false_eligible"] for x in d),
            "first_error_verdicts": {v: sum(x["first_error_verdict"] == v for x in d)
                                     for v in cfg["annotation_review"]["first_error_verdicts"]},
            "false_step_verdicts": {v: sum(x["false_step_verdict"] == v for x in d)
                                    for v in cfg["annotation_review"]["false_step_verdicts"]},
            "min_primary_items": need, "primary_gate_passed": n_primary >= need}
