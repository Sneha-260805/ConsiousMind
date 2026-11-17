"""Inspect the actual ProcessBench release before anything is built (no model, no selection).

    python scripts/01_inspect_processbench.py
Writes data/manifests/processbench_inspection.json and data/manifests/PROCESSBENCH_INSPECTION.md:
source/revision/hashes, schema check of every record, per-split counts, label (first-error) distribution,
final_answer_correct x label cross-tab, generators, step counts, reference-answer availability and numeric-answer
rates (GSM8K -> GSM8K test; MATH -> Hendrycks MATH test), MATH-500 cross-check.
"""
import collections
import json
import sys

import _bootstrap  # noqa: F401
from src import common as C
from src import frozen_sources as fs
from src import processbench_adapter as pa


def main():
    cfg = C.load_config()
    src = cfg["sources"]["processbench"]
    d = C.STAGE_ROOT / src["local_dir"]
    for name, sha in src["files_sha256"].items():
        if C.sha256_file(d / name) != sha:
            raise SystemExit(f"STOP: {name} does not match the pinned sha256")
    gsm_idx = pa.gsm8k_reference_index(fs.pilot_path(cfg["sources"]["gsm8k_reference"]["pilot_file"], cfg))
    mr = cfg["sources"]["math_reference"]
    if C.sha256_file(C.STAGE_ROOT / mr["rows_dump"]) != mr["rows_dump_sha256"]:
        raise SystemExit("STOP: MATH rows dump changed")
    math_idx = pa.math_reference_index(C.STAGE_ROOT / mr["rows_dump"])
    m500 = pa.math500_index(C.STAGE_ROOT / mr["math500_file"])
    out = {"source": {"repo": src["repo"], "paper": src["paper"], "revision": src["revision"],
                      "files_sha256": src["files_sha256"], "hub_git_blob_oids": src["hub_git_blob_oids"]},
           "schema": {k: v.__name__ for k, v in pa.SCHEMA_FIELDS.items()},
           "schema_notes": ["label = 0-indexed earliest erroneous step; -1 = no error",
                            "NO reference-answer field in any split",
                            "steps are pre-segmented strings; shown to the solver as 'Step i:' with i = index + 1"],
           "splits": {}}
    for split in pa.SPLITS:
        recs = pa.load_split(d / f"{split}.json", split)          # schema-validates every record
        lab = collections.Counter(r["label"] for r in recs)
        s = {"n_records": len(recs),
             "n_distinct_problems": len({pa.ws(r["problem"]) for r in recs}),
             "label_distribution_0indexed": {str(k): lab[k] for k in sorted(lab)},
             "final_answer_correct_x_has_error": {
                 f"fac={a}|error={b}": n for (a, b), n in sorted(collections.Counter(
                     (r["final_answer_correct"], r["label"] >= 0) for r in recs).items())},
             "generators": dict(collections.Counter(r["generator"] for r in recs).most_common()),
             "n_steps": {"min": min(len(r["steps"]) for r in recs), "max": max(len(r["steps"]) for r in recs),
                         "distribution": dict(sorted(collections.Counter(len(r["steps"]) for r in recs).items()))},
             "trace_chars_max": max(len("\n".join(r["steps"])) for r in recs),
             "original_trace_has_boxed": sum(pa.last_boxed("\n".join(r["steps"])) is not None for r in recs),
             "original_trace_boxed_numeric": sum(pa.normalise_numeric(pa.last_boxed("\n".join(r["steps"])))
                                                 is not None for r in recs)}
        if split in ("gsm8k", "math"):
            idx = gsm_idx if split == "gsm8k" else math_idx
            st = collections.Counter()
            numeric = 0
            agree500 = disagree500 = 0
            for r in recs:
                status, ref = pa.lookup_reference(r["problem"], idx)
                st[status] += 1
                if status == "ok" and pa.normalise_numeric(ref["raw"]) is not None:
                    numeric += 1
                if split == "math" and status == "ok" and pa.ws(r["problem"]) in m500:
                    a, b = pa.last_boxed("\\boxed{" + m500[pa.ws(r["problem"])] + "}"), ref["raw"]
                    if pa.ws(a or "") == pa.ws(b or "") or (pa.normalise_numeric(a) is not None
                                                           and pa.normalise_numeric(a) == pa.normalise_numeric(b)):
                        agree500 += 1
                    else:
                        disagree500 += 1
            s["reference_lookup"] = dict(st)
            s["reference_numeric_parseable"] = numeric
            if split == "math":
                s["math500_crosscheck"] = {"overlapping_records": agree500 + disagree500, "agree": agree500,
                                           "disagree": disagree500}
        else:
            s["reference_lookup"] = "not attempted (domain not used in Stage B; answers mostly symbolic)"
        out["splits"][split] = s
    out["total_records"] = sum(s["n_records"] for s in out["splits"].values())
    C.write_json(C.INSPECTION_FILE, out)
    md = ["# ProcessBench inspection (Stage B)", "",
          f"Source: {src['repo']} @ `{src['revision']}` ({src['paper']}). Files sha256-verified; git blob OIDs match "
          "the Hub tree listing.", "",
          "Schema (every record validated): " + ", ".join(f"`{k}`: {v}" for k, v in out["schema"].items()), "",
          *[f"- {n}" for n in out["schema_notes"]], "",
          f"Total records: **{out['total_records']}**", "",
          "| split | records | distinct problems | error annotated | fac=False & error | label 0 | ref found | ref numeric |",
          "|---|---|---|---|---|---|---|---|"]
    for split, s in out["splits"].items():
        fx = s["final_answer_correct_x_has_error"]
        err = sum(v for k, v in fx.items() if k.endswith("error=True"))
        md.append(f"| {split} | {s['n_records']} | {s['n_distinct_problems']} | {err} | "
                  f"{fx.get('fac=False|error=True', 0)} | {s['label_distribution_0indexed'].get('0', 0)} | "
                  f"{s['reference_lookup'].get('ok', '-') if isinstance(s['reference_lookup'], dict) else '-'} | "
                  f"{s.get('reference_numeric_parseable', '-')} |")
    md += ["", "Label distributions (0-indexed) and other details: `processbench_inspection.json`.", ""]
    (C.DATA_MAN / "PROCESSBENCH_INSPECTION.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("generators",)}
                      for k, v in out["splits"].items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
