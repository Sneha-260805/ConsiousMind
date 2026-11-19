"""Stage-B analysis (code fixed before any Stage-B model output exists).

Inputs: frozen pool, scored Stage-1 rows (all items), the frozen failure set, the frozen blinded annotation-review
decisions, scored Stage-2 rows (complete; a partial Stage-2 run is refused).

Populations
  pool            all Stage-1 items (Stage-1 metrics; stratified by domain and MATH level)
  primary set     Stage-1 failures whose annotated first error was CONFIRMED by the blinded review (all Stage-2
                  conditions; L_false only where the false step was confirmed CORRECT, or - amendment A6 - where
                  its validity is Pass-1-implied: the same reviewer CONFIRMED k as the earliest substantive error,
                  so f = k-1 is labelled "Pass-1-implied false-step validity", not independently Pass-2-validated)
  analysis pop.   pool minus failures excluded by the review (final-repair denominators). A conservative full-pool
                  variant (review-excluded failures counted as unrepaired under every policy) is also reported.
Definitions
  Rescue(c)        = #(Stage-2 repair under c) / #(primary-set items that received c)
  FinalRepair(c)   = (#Stage-1 repairs + #Stage-2 repairs under c) / #analysis population    (policy "V -> c")
  FinalRepair(V)   = #Stage-1 repairs / #analysis population                                 (one-shot V, 1 call)
  Delta_rescue     = Rescue(L_true) - Rescue(VV), paired over the primary set               (PRIMARY)
  Delta_final      = Delta_rescue * n_primary / n_analysis_population
Paired statistics per comparison (items that received both conditions): both / A-only / B-only / neither, exact
two-sided McNemar, paired bootstrap 95% percentile CI (resample items with replacement; fixed seed; 10,000).
Interpretation (FROZEN precision rule, configs/stage_b.json -> interpretation_rule):
  STRONG       CI lower bound > 0 AND point estimate >= +0.10
  NO SUPPORT   CI upper bound < +0.10
  INCONCLUSIVE otherwise
V->L_true vs V->L_false is reported as a SEPARATE paired diagnostic-content contrast (not part of the label).
Unnecessary second-call rate = share of items given a second call although Stage 1 succeeded (0 by design).
Every output carries the oracle statement and the contamination limitation. TEST-ONLY inputs produce TEST-ONLY outputs
and no interpretation label.
"""
import csv
import math
import random
import statistics
from collections import Counter
from pathlib import Path

from . import common as C
from . import scorer

POLICIES = ["V", "VV", "N_true", "L_true", "L_false"]       # V = one-shot V; others = V -> c


# ------------------------------------------------------------------ statistics
def wilson(k, n, z=1.959964):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b, c):
    """Two-sided exact McNemar (binomial on discordant pairs)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def paired_bootstrap(a, b, resamples, seed, level=0.95):
    """Percentile CI of mean(a) - mean(b) for paired 0/1 vectors."""
    n = len(a)
    if n == 0:
        return (None, None)
    rng = random.Random(seed)
    diffs = [x - y for x, y in zip(a, b)]
    stats = []
    for _ in range(resamples):
        s = 0
        for _ in range(n):
            s += diffs[rng.randrange(n)]
        stats.append(s / n)
    stats.sort()
    lo = stats[int(math.floor((1 - level) / 2 * resamples))]
    hi = stats[min(resamples - 1, int(math.ceil((1 + level) / 2 * resamples)) - 1)]
    return (lo, hi)


def r4(x):
    return None if x is None else round(x, 4)


def label_outcome(primary, rule):
    """FROZEN precision rule. primary: paired-comparison row for V->L_true vs V->V."""
    d, lo, hi = primary["delta_rescue"], primary["delta_rescue_ci_low"], primary["delta_rescue_ci_high"]
    m = rule["meaningful_effect"]
    if d is None or lo is None:
        label = "NOT COMPUTABLE"
    elif lo > 0 and d >= m:
        label = "STRONG"
    elif hi < m:
        label = "NO SUPPORT"
    else:
        label = "INCONCLUSIVE"
    return {"label": label, "delta_rescue": d, "ci95": [lo, hi], "meaningful_effect": m,
            "rule": {k: rule[k] for k in ("STRONG", "NO_SUPPORT", "INCONCLUSIVE")}}


# ------------------------------------------------------------------ assembly
def assemble(pool, s1_scored, failures_doc, decisions, s2_scored, conditions):
    """Validate completeness and join everything per item. -> (rows, problems)"""
    probs = []
    pool_ids = [i["item_id"] for i in pool]
    s1 = {s["item_id"]: s for s in s1_scored}
    if sorted(s1) != sorted(pool_ids) or len(s1) != len(s1_scored):
        probs.append("Stage-1 scored rows do not cover the pool exactly once")
    fail_ids = set(failures_doc["item_ids"])
    if {iid for iid, s in s1.items() if not s["repair"]} != fail_ids:
        probs.append("frozen failure set != Stage-1 failures recomputed with the frozen scorer")
    if set(decisions) != fail_ids:
        probs.append("annotation-review decisions do not cover exactly the frozen failure set")
    s2 = {}
    for s in s2_scored:
        if (s["item_id"], s["condition"]) in s2:
            probs.append(f"duplicate Stage-2 row {s['request_key']}")
        s2[(s["item_id"], s["condition"])] = s
    expected = set()
    for iid in fail_ids:
        dec = decisions.get(iid) or {}
        if dec.get("primary_stage2"):
            expected |= {(iid, c) for c in conditions if c != "L_false" or dec.get("l_false_eligible")}
    if set(s2) != expected:
        extra, missing = set(s2) - expected, expected - set(s2)
        probs.append(f"Stage-2 rows != review-determined plan (missing {len(missing)}, unexpected {len(extra)}; "
                     f"e.g. {sorted(missing or extra)[:2]})")
    rows = []
    for it in pool:
        iid = it["item_id"]
        dec = decisions.get(iid) or {}
        failed = iid in fail_ids
        rows.append({"item": it, "s1": s1.get(iid), "failed": failed,
                     "primary": failed and bool(dec.get("primary_stage2")),
                     "review_excluded": failed and not dec.get("primary_stage2"),
                     "l_false_ok": failed and bool(dec.get("l_false_eligible")), "decision": dec,
                     "s2": {c: s2.get((iid, c)) for c in conditions}})
    return rows, probs


def has(r, c):
    return r["s2"].get(c) is not None


def final_success(row, policy):
    """1/0, or None if the item is outside this policy's population."""
    if row["review_excluded"]:
        return None
    if row["s1"]["repair"]:
        return 1
    if policy == "V":
        return 0
    if not has(row, policy):
        return None
    return int(bool(row["s2"][policy]["repair"]))


def compare(rows, a, b, n_pop, cfg, seed_offset=0):
    rows = [r for r in rows if has(r, a) and has(r, b)]
    va = [int(r["s2"][a]["repair"]) for r in rows]
    vb = [int(r["s2"][b]["repair"]) for r in rows]
    n = len(va)
    both = sum(x and y for x, y in zip(va, vb))
    a_only = sum(x and not y for x, y in zip(va, vb))
    b_only = sum(y and not x for x, y in zip(va, vb))
    an = cfg["analysis"]
    lo, hi = paired_bootstrap(va, vb, an["bootstrap_resamples"], an["bootstrap_seed"] + seed_offset, an["ci_level"])
    d = (sum(va) - sum(vb)) / n if n else None
    scale = n / n_pop if n_pop else 0
    return {"comparison": f"V->{a} vs V->{b}", "A": a, "B": b, "n_paired": n, "both_rescue": both,
            "A_only_rescues": a_only, "B_only_rescues": b_only, "neither_rescues": n - both - a_only - b_only,
            "rescue_A": r4(sum(va) / n) if n else None, "rescue_B": r4(sum(vb) / n) if n else None,
            "delta_rescue": r4(d), "delta_rescue_ci_low": r4(lo), "delta_rescue_ci_high": r4(hi),
            "ci_half_width": r4((hi - lo) / 2) if lo is not None else None,
            "mcnemar_exact_p": r4(mcnemar_exact(a_only, b_only)),
            "n_population": n_pop, "delta_final_repair": r4(d * scale) if d is not None else None,
            "delta_final_ci_low": r4(lo * scale) if lo is not None else None,
            "delta_final_ci_high": r4(hi * scale) if hi is not None else None,
            "bootstrap": f"{an['bootstrap_resamples']} paired resamples, seed {an['bootstrap_seed'] + seed_offset}, "
                         "percentile CI"}


def strata(items):
    """(name, predicate) pairs for stratified secondary analysis: domain and MATH level."""
    out = [("all", lambda r: True)]
    for d in sorted({r["item"]["domain"] for r in items}):
        out.append((d, lambda r, d=d: r["item"]["domain"] == d))
    for lv in sorted({r["item"]["math_level"] for r in items if r["item"]["math_level"]}):
        out.append((f"math {lv}", lambda r, lv=lv: r["item"]["math_level"] == lv))
    return out


def analyse(pool, s1_scored, failures_doc, review_doc, s2_scored, cfg, test_only):
    conds = cfg["stage2"]["conditions"]
    labels = cfg["stage2"]["condition_labels"]
    decisions = {d["item_id"]: d for d in review_doc["decisions"]}
    items, probs = assemble(pool, s1_scored, failures_doc, decisions, s2_scored, conds)
    if probs:
        raise SystemExit("STOP: analysis inputs incomplete/inconsistent (no partial analysis):\n" + "\n".join(probs[:20]))
    n_pool = len(items)
    prim = [r for r in items if r["primary"]]
    pop = [r for r in items if not r["review_excluded"]]
    n2, n_pop = len(prim), len(pop)
    T = {}

    # ---- Stage 1 (whole pool, stratified)
    def s1_row(scope, rows):
        k = sum(r["s1"]["repair"] for r in rows)
        oc = Counter(r["s1"]["outcome"] for r in rows)
        toks = [r["s1"]["new_tokens"] for r in rows if r["s1"]["new_tokens"] is not None]
        lo, hi = wilson(k, len(rows))
        return {"scope": scope, "n": len(rows), "stage1_repaired": k, "stage1_repair_rate": r4(k / len(rows)),
                "ci95_low": r4(lo), "ci95_high": r4(hi), "stage1_failures": len(rows) - k,
                "failures_passing_review": sum(r["primary"] for r in rows),
                **{f"n_{o}": oc[o] for o in scorer.OUTCOMES_STAGE1},
                "truncated": sum(r["s1"]["truncated"] for r in rows),
                "strict_format_compliant": sum(r["s1"]["strict_format_compliant"] for r in rows),
                "new_tokens_mean": r4(statistics.mean(toks)) if toks else None,
                "new_tokens_median": statistics.median(toks) if toks else None,
                "new_tokens_max": max(toks) if toks else None}
    T["stage1_metrics"] = [s1_row(name, [r for r in items if f(r)]) for name, f in strata(items)
                           if any(f(r) for r in items)]
    T["stage1_failures"] = [{"item_id": r["item"]["item_id"], "domain": r["item"]["domain"],
                             "math_level": r["item"]["math_level"], "true_error_step": r["item"]["true_error_step"],
                             "n_steps": r["item"]["n_steps"], "stage1_outcome": r["s1"]["outcome"],
                             "stage1_answer": r["s1"]["numeric_parsed_answer"], "gold_answer": r["item"]["gold_answer"],
                             "original_answer": r["item"]["original_answer"], "stage1_truncated": r["s1"]["truncated"],
                             "stage1_new_tokens": r["s1"]["new_tokens"],
                             "review_first_error_verdict": r["decision"].get("first_error_verdict"),
                             "review_false_step_verdict": r["decision"].get("false_step_verdict"),
                             "in_primary_stage2_set": r["primary"], "l_false_eligible": r["l_false_ok"]}
                            for r in items if r["failed"]]

    # ---- Stage-2 rescue + final repair (stratified)
    resc = []
    for scope, f in strata(items):
        fr = [r for r in prim if f(r)]
        ir = [r for r in pop if f(r)]
        if not ir:
            continue
        s1k = sum(r["s1"]["repair"] for r in ir)
        lo, hi = wilson(s1k, len(ir))
        resc.append({"scope": scope, "policy": "V (one-shot)", "condition": "-", "n_received": 0, "rescued": None,
                     "rescue_rate": None, "rescue_ci95_low": None, "rescue_ci95_high": None,
                     "n_population": len(ir), "final_repaired": s1k, "final_repair_rate": r4(s1k / len(ir)),
                     "final_ci95_low": r4(lo), "final_ci95_high": r4(hi)})
        for c in conds:
            got = [r for r in fr if has(r, c)]
            k = sum(r["s2"][c]["repair"] for r in got)
            lo, hi = wilson(k, len(got))
            popc = [r for r in ir if final_success(r, c) is not None]
            fk = sum(final_success(r, c) for r in popc)
            flo, fhi = wilson(fk, len(popc))
            resc.append({"scope": scope, "policy": labels[c], "condition": c, "n_received": len(got), "rescued": k,
                         "rescue_rate": r4(k / len(got)) if got else None, "rescue_ci95_low": r4(lo),
                         "rescue_ci95_high": r4(hi), "n_population": len(popc), "final_repaired": fk,
                         "final_repair_rate": r4(fk / len(popc)) if popc else None,
                         "final_ci95_low": r4(flo), "final_ci95_high": r4(fhi)})
    # conservative full-pool variant: review-excluded failures count as unrepaired under every policy
    for c in ["V", *conds]:
        rows = [r for r in items if r["review_excluded"] or final_success(r, c) is not None]
        k = sum(final_success(r, c) or 0 for r in rows)
        lo, hi = wilson(k, len(rows))
        resc.append({"scope": "conservative full pool (review-excluded = unrepaired)",
                     "policy": "V (one-shot)" if c == "V" else labels[c], "condition": "-" if c == "V" else c,
                     "n_received": None, "rescued": None, "rescue_rate": None, "rescue_ci95_low": None,
                     "rescue_ci95_high": None, "n_population": len(rows), "final_repaired": k,
                     "final_repair_rate": r4(k / len(rows)), "final_ci95_low": r4(lo), "final_ci95_high": r4(hi)})
    T["stage2_rescue_metrics"] = resc

    # ---- paired comparisons
    an = cfg["analysis"]
    a, b = an["primary_comparison"]
    plan = [((a, b), "all", prim, "PRIMARY"),
            (("L_true", "L_false"), "all", prim, "DIAGNOSTIC-CONTENT CONTRAST (separate; not in the label)"),
            *[((x, y), "all", prim, "secondary") for x, y in an["secondary_comparisons"]
              if (x, y) != ("L_true", "L_false")]]
    for name, f in strata(items)[1:]:
        plan.append(((a, b), name, [r for r in prim if f(r)], "descriptive stratum"))
    plan.append(((a, b), "sensitivity: excl. items with truncated/unparsed VV or L_true Stage-2 output",
                 [r for r in prim if not any(r["s2"][c]["truncated"] or r["s2"][c]["parse_failure"] for c in (a, b))],
                 "sensitivity"))
    plan.append(((a, b), "sensitivity: excl. items whose Stage-1 response was truncated",
                 [r for r in prim if not r["s1"]["truncated"]], "sensitivity"))
    pc = []
    for j, ((x, y), scope, rows, role) in enumerate(plan):
        row = compare(rows, x, y, n_pop, cfg, seed_offset=j)
        row.update(scope=scope, role=role)
        pc.append(row)
    T["stage2_paired_comparisons"] = pc

    # ---- failure decomposition
    fd = []
    oc1 = Counter(r["s1"]["outcome"] for r in items)
    for o in scorer.OUTCOMES_STAGE1:
        fd.append({"stage": 1, "condition": "V", "n": n_pool, "outcome": o, "count": oc1[o], "rate": r4(oc1[o] / n_pool)})
    for c in conds:
        got = [r for r in prim if has(r, c)]
        oc = Counter(r["s2"][c]["outcome"] for r in got)
        for o in scorer.OUTCOMES_STAGE2:
            fd.append({"stage": 2, "condition": c, "n": len(got), "outcome": o, "count": oc[o],
                       "rate": r4(oc[o] / len(got)) if got else None})
    T["failure_decomposition"] = fd

    # ---- cost and tokens (analysis population of each policy)
    def tok(s, k="new_tokens"):
        return (s or {}).get(k) or 0
    cost = []
    base = {id(r): final_success(r, "V") for r in pop}
    for p in POLICIES:
        rows = [r for r in pop if final_success(r, p) is not None]
        second = [p != "V" and r["primary"] and has(r, p) for r in rows]
        gen = [tok(r["s1"]) + (tok(r["s2"][p]) if s else 0) for r, s in zip(rows, second)]
        ptk = [tok(r["s1"], "prompt_tokens") + (tok(r["s2"][p], "prompt_tokens") if s else 0)
               for r, s in zip(rows, second)]
        fin = statistics.mean(final_success(r, p) for r in rows)
        base_fin = statistics.mean(base[id(r)] for r in rows)
        vv_rows = [r for r in rows if final_success(r, "VV") is not None]
        extra = statistics.mean(gen) - statistics.mean(tok(r["s1"]) for r in rows)
        cost.append({"policy": "V (one-shot)" if p == "V" else labels[p], "n_population": len(rows),
                     "final_repair_rate": r4(fin), "repair_calls_per_item_mean": r4(1 + statistics.mean(second)),
                     "second_calls": sum(second),
                     "unnecessary_second_call_rate": r4(sum(1 for r, s in zip(rows, second)
                                                            if s and r["s1"]["repair"]) / len(rows)),
                     "generated_tokens_per_item_mean": r4(statistics.mean(gen)),
                     "prompt_tokens_per_item_mean": r4(statistics.mean(ptk)),
                     "additional_generated_tokens_vs_V": r4(extra),
                     "final_repair_gain_vs_V_pp": r4(100 * (fin - base_fin)),
                     "final_repair_gain_vs_VV_pp": None if p == "V" else
                     r4(100 * (fin - statistics.mean(final_success(r, "VV") for r in vv_rows))),
                     "gain_vs_V_pp_per_1k_extra_tokens": r4(100 * (fin - base_fin) / (extra / 1000)) if extra > 0
                     else None})
    T["cost_and_tokens"] = cost

    # ---- truncation
    tr = []
    for stage, c, rows in [(1, "V", [r["s1"] for r in items])] + \
            [(2, c, [r["s2"][c] for r in prim if has(r, c)]) for c in conds]:
        toks = [s["new_tokens"] for s in rows if s["new_tokens"] is not None]
        tr.append({"stage": stage, "condition": c, "n": len(rows), "truncated": sum(s["truncated"] for s in rows),
                   "truncated_without_valid_answer": sum(s["truncated_without_valid_answer"] for s in rows),
                   "parse_failures": sum(s["parse_failure"] for s in rows),
                   "strict_format_compliant": sum(s["strict_format_compliant"] for s in rows),
                   "new_tokens_max": max(toks) if toks else None,
                   "new_tokens_mean": r4(statistics.mean(toks)) if toks else None,
                   "prompt_tokens_max": max(((s["prompt_tokens"] or 0) for s in rows), default=None)})
    T["truncation_report"] = tr

    # ---- item level
    il = []
    for r in items:
        it = r["item"]
        row = {"item_id": it["item_id"], "domain": it["domain"], "math_level": it["math_level"],
               "true_error_step": it["true_error_step"], "false_step": it["false_location"]["false_step"],
               "n_steps": it["n_steps"], "gold_answer": it["gold_answer"], "original_answer": it["original_answer"],
               "stage1_outcome": r["s1"]["outcome"], "stage1_answer": r["s1"]["numeric_parsed_answer"],
               "stage1_truncated": r["s1"]["truncated"], "stage1_failed": r["failed"],
               "review_first_error_verdict": r["decision"].get("first_error_verdict", ""),
               "review_false_step_verdict": r["decision"].get("false_step_verdict", ""),
               "entered_stage2": r["primary"]}
        for c in conds:
            s = r["s2"][c]
            row[f"s2_{c}_outcome"] = s["outcome"] if s else ""
            row[f"s2_{c}_answer"] = s["numeric_parsed_answer"] if s else ""
            row[f"s2_{c}_truncated"] = s["truncated"] if s else ""
        for p in POLICIES:
            v = final_success(r, p)
            row[f"final_repair_{'V_oneshot' if p == 'V' else 'V_to_' + p}"] = "" if v is None else v
        il.append(row)
    T["item_level_sequential_outcomes"] = il

    primary = pc[0]
    content = pc[1]
    summary = {
        "TEST_ONLY": test_only, "banner": C.TEST_ONLY_BANNER if test_only else "REAL MODEL RESULTS",
        "oracle_status": C.ORACLE_STATEMENT, "limitations": C.LIMITATIONS,
        "n_pool": n_pool, "n_stage1_failures": sum(r["failed"] for r in items),
        "n_review_excluded": sum(r["review_excluded"] for r in items), "n_primary_stage2": n2,
        "n_l_false": sum(r["l_false_ok"] for r in items), "n_analysis_population": n_pop,
        "stage1_repair_rate": T["stage1_metrics"][0]["stage1_repair_rate"],
        "failure_gate": failures_doc["failure_gate"], "annotation_review": review_doc["summary"],
        "rescue": {c: next(x["rescue_rate"] for x in resc if x["scope"] == "all" and x["condition"] == c)
                   for c in conds},
        "final_repair": {x["policy"]: x["final_repair_rate"] for x in resc if x["scope"] == "all"},
        "primary": primary, "diagnostic_content_contrast": content,
        "l_false_validity_basis": dict(Counter(r["decision"].get("false_step_validity_basis") or "unspecified"
                                               for r in items if r["l_false_ok"])),
        "pass2_waiver": (cfg["annotation_review"]["pass2_waiver_A6"]["statement"]
                         if any(r["decision"].get("false_step_verdict") == "PASS1_IMPLIED" for r in items) else None),
        "power_note": (f"primary Stage-2 N = {n2} < {cfg['annotation_review']['decision_rules']['min_primary_items']}"
                       if n2 < cfg["annotation_review"]["decision_rules"]["min_primary_items"] else None),
    }
    if test_only:
        summary["interpretation"] = {"label": "SUPPRESSED - TEST ONLY (mock outputs are not results)"}
    else:
        summary["interpretation"] = label_outcome(primary, cfg["interpretation_rule"])
        summary["interpretation"]["rule_frozen"] = C.RULE_LOCK.exists()
    return T, summary


# ------------------------------------------------------------------ writing
def write_csv(path, rows, test_only):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        if test_only:
            fh.write(f"# {C.TEST_ONLY_BANNER}\n")
        if not rows:
            return
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_outputs(out_dir, T, summary):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t = summary["TEST_ONLY"]
    for name, rows in T.items():
        write_csv(out / f"{name}.csv", rows, t)
    C.write_json(out / "summary.json", summary)
    p, q = summary["primary"], summary["diagnostic_content_contrast"]
    lines = [f"# Stage B results - {summary['banner']}", "", f"> {C.ORACLE_STATEMENT}", "", f"> {C.LIMITATIONS}", "",
             f"- Pool: {summary['n_pool']} items; Stage-1 V repair rate {summary['stage1_repair_rate']}; "
             f"Stage-1 failures {summary['n_stage1_failures']} (gate >= {summary['failure_gate']['min_stage1_failures']}: "
             f"{'passed' if summary['failure_gate']['passed'] else 'NOT passed'}); excluded by blinded annotation "
             f"review {summary['n_review_excluded']}; primary Stage-2 set {summary['n_primary_stage2']} "
             f"(L_false on {summary['n_l_false']})",
             "- Rescue rate by Stage-2 condition: " + ", ".join(f"{k} {v}" for k, v in summary["rescue"].items()),
             "- Final repair rate by policy (analysis population): "
             + ", ".join(f"{k} {v}" for k, v in summary["final_repair"].items()),
             f"- PRIMARY V->L_true vs V->V (n={p['n_paired']}): both {p['both_rescue']}, L_true only "
             f"{p['A_only_rescues']}, V retry only {p['B_only_rescues']}, neither {p['neither_rescues']}; "
             f"Delta_rescue {p['delta_rescue']} (95% CI {p['delta_rescue_ci_low']} to {p['delta_rescue_ci_high']}), "
             f"exact McNemar p {p['mcnemar_exact_p']}; Delta_final {p['delta_final_repair']}",
             f"- Diagnostic-content contrast V->L_true vs V->L_false (n={q['n_paired']}): Delta {q['delta_rescue']} "
             f"(95% CI {q['delta_rescue_ci_low']} to {q['delta_rescue_ci_high']}), McNemar p {q['mcnemar_exact_p']}",
             f"- L_false validity basis: " + ", ".join(f"{k} ({v})" for k, v in summary["l_false_validity_basis"].items()),
             *([f"- Pass-2 waiver (A6): {summary['pass2_waiver']}"] if summary.get("pass2_waiver") else []),
             f"- Interpretation label (frozen precision rule): {summary['interpretation']['label']}",
             *([f"- Power note: {summary['power_note']}"] if summary["power_note"] else []), ""]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out
