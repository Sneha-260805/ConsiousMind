"""TEST ONLY: fill a mock annotation-review form deterministically (for mock dry runs and tests; never for real data).

    python scripts/_mock_fill_review_form.py --work-dir outputs/test_runs/<x>/stage2_inputs
About 85% CONFIRM / 10% DISAGREE / 5% UNCLEAR first-error verdicts and 90% CORRECT false-step verdicts, by hash.
"""
import argparse
import sys

import _bootstrap  # noqa: F401
from src import annotation_review as ar
from src import common as C
from src import stage2_runner as s2r


def fill(P):
    if P["real"]:
        raise SystemExit("STOP: the mock form filler never touches real review files")
    d = P["review_dir"]
    rows = ar.read_form(d / "annotation_review_form.csv")
    for r in rows:
        u = int(C.sha256_text("mockreview:" + r["item_id"])[:8], 16) / 0xFFFFFFFF
        r["first_error_verdict"] = "CONFIRM" if u < 0.85 else ("DISAGREE" if u < 0.95 else "UNCLEAR")
        r["reviewer_first_error_step"] = str(int(r["annotated_first_error_step"]) + 1) \
            if r["first_error_verdict"] == "DISAGREE" else ""
        r["false_step_verdict"] = "CORRECT" if (u * 7) % 1 < 0.9 else "NOT_CORRECT"
        r["notes"] = "TEST ONLY mock verdict"
    (d / "annotation_review_form_FILLED.csv").write_text(ar.form_csv(rows), encoding="utf-8", newline="\n")
    return len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work-dir", required=True)
    print("filled", fill(s2r.paths(ap.parse_args().work_dir)), "mock rows (TEST ONLY)")
    sys.exit(0)
