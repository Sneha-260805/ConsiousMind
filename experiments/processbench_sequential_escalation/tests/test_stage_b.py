"""Stage-B unit + integration tests (standard library unittest; mock solver only - TEST ONLY; no model).

    python -m unittest discover -s tests -t .        (from experiments/stage_b_sequential_escalation)
    python tests/run_all.py                          (same, and records tests/LAST_PASS.json on success)
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import analysis as an                    # noqa: E402
from src import annotation_review as ar           # noqa: E402
from src import smoke_gate as sg                  # noqa: E402
from src import common as C                       # noqa: E402
from src import dataset_builder as db             # noqa: E402
from src import frozen_sources as fs              # noqa: E402
from src import parser                            # noqa: E402
from src import processbench_adapter as pa        # noqa: E402
from src import prompt_builder as pb              # noqa: E402
from src import runner_core as rc                 # noqa: E402
from src import scorer                            # noqa: E402
from src import stage1_runner as s1               # noqa: E402
from src import stage2_builder as s2b             # noqa: E402
from src import stage2_runner as s2r              # noqa: E402
from src.mock_solver import MockSolver, MOCK_BANNER  # noqa: E402
from src.persistence import DuplicateRequestError, RunStore, read_jsonl_tolerant  # noqa: E402

import os as _os                                   # noqa: E402
_os.environ["STAGE_B_TEST_SUITE"] = "1"           # H2: no real model load from any test (also via unittest directly)
CFG = C.load_config()
POOL = C.read_json(C.ITEMS_FILE)["pool"]
TMP = C.TEST_RUNS_ROOT / "_unittest"


def quiet(*_a, **_k):
    pass


class Pipeline:
    """One shared mock pipeline (Stage 1 -> freeze -> Stage 2 -> analysis) under outputs/test_runs/_unittest."""
    built = False

    @classmethod
    def build(cls):
        if cls.built:
            return cls
        if TMP.exists():
            shutil.rmtree(TMP)
        cls.s1_dir, cls.s1_stats, cls.s1_diag, _ = s1.run("mock", "s1", TMP, log=quiet)
        cls.work = TMP / "stage2_inputs"
        cls.P = s2r.paths(cls.work)
        by_key, order, hashes = s1.frozen_inputs(CFG)
        rows, man, sha = s2b.load_stage1_run(cls.s1_dir)
        cls.fails, cls.resp, cls.gate = s2b.failures_from_rows(rows, len(POOL), CFG)
        doc = s2b.failures_doc(cls.fails, cls.gate, sha, man, True)
        cls.work.mkdir(parents=True, exist_ok=True)
        f_sha = C.write_json(cls.P["failures"], doc)
        r_sha = C.write_jsonl(cls.P["responses"], cls.resp)
        C.write_json(cls.P["lock"], {"failures_sha256": f_sha, "responses_sha256": r_sha, "TEST_ONLY": True})
        for args in (["scripts/07a_export_annotation_review.py"], ["scripts/_mock_fill_review_form.py"],
                     ["scripts/07b_freeze_annotation_review.py", "--reviewer", "TEST"]):
            r = subprocess.run([sys.executable, *args, "--work-dir", str(cls.work)], cwd=C.STAGE_ROOT,
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
        cls.review, _ = s2r.verify_review_lock(cls.P)
        cls.decisions = {d["item_id"]: d for d in cls.review["decisions"]}
        cls.reqs2 = s2b.build_requests(POOL, cls.fails, cls.resp, CFG["stage2"]["conditions"], cls.decisions)
        C.write_jsonl(cls.P["requests"], cls.reqs2)
        C.write_json(cls.P["order"], s2b.execution_order(cls.reqs2, CFG["stage2"]["execution_order_seed"]))
        cls.s2_dir, cls.s2_stats, _, _ = s2r.run("mock", "s2", TMP, cls.P, log=quiet)
        cls.built = True
        return cls


# ====================================================================== dataset
class TestProcessBenchSchema(unittest.TestCase):
    def good(self):
        return {"id": "gsm8k-1", "generator": "g", "problem": "p", "steps": ["a", "b"],
                "final_answer_correct": False, "label": 1}

    def test_valid_record(self):
        self.assertEqual(pa.validate_record(self.good(), "gsm8k")["id"], "gsm8k-1")

    def test_missing_extra_and_types(self):
        for mut in (lambda r: r.pop("label"), lambda r: r.update(extra=1), lambda r: r.update(label=True),
                    lambda r: r.update(steps="x"), lambda r: r.update(label=5), lambda r: r.update(label=-2),
                    lambda r: r.update(final_answer_correct="no"), lambda r: r.update(steps=["a", 3])):
            r = self.good()
            mut(r)
            with self.assertRaises(pa.SchemaError):
                pa.validate_record(r, "gsm8k")
        with self.assertRaises(pa.SchemaError):
            pa.validate_record(self.good(), "math")

    def test_real_release_loads(self):
        d = C.STAGE_ROOT / CFG["sources"]["processbench"]["local_dir"]
        n = {s: len(pa.load_split(d / f"{s}.json", s)) for s in pa.SPLITS}
        self.assertEqual(n, {"gsm8k": 400, "math": 1000, "olympiadbench": 1000, "omnimath": 1000})

    def test_raw_sources_pinned(self):
        s = CFG["sources"]["processbench"]
        for name, sha in s["files_sha256"].items():
            self.assertEqual(C.sha256_file(C.STAGE_ROOT / s["local_dir"] / name), sha)


class TestErrorStep(unittest.TestCase):
    def test_label_to_1_indexed(self):
        self.assertEqual(pa.first_error_step({"label": 0}), 1)
        self.assertEqual(pa.first_error_step({"label": 3}), 4)
        self.assertIsNone(pa.first_error_step({"label": -1}))

    def test_pool_matches_source_label(self):
        d = C.STAGE_ROOT / CFG["sources"]["processbench"]["local_dir"]
        src = {r["id"]: r for s in ("gsm8k", "math") for r in pa.load_split(d / f"{s}.json", s)}
        for it in POOL:
            r = src[it["processbench_id"]]
            self.assertEqual(it["true_error_step"], r["label"] + 1)
            self.assertGreaterEqual(it["true_error_step"], 2)
            self.assertFalse(r["final_answer_correct"])


class TestAnswerExtraction(unittest.TestCase):
    def test_boxed(self):
        self.assertEqual(pa.last_boxed(r"a \boxed{1} b \boxed{\frac{1}{2}}"), r"\frac{1}{2}")
        self.assertEqual(pa.last_boxed(r"\boxed{\text{ 18 }}"), r"\text{ 18 }")
        self.assertIsNone(pa.last_boxed("no box"))
        self.assertIsNone(pa.last_boxed(r"\boxed{unbalanced"))
        self.assertEqual(pa.boxed_all(r"\fbox{3} \boxed{4}"), ["3", "4"])

    def test_normalise_accepts(self):
        cases = {"18": "18", "-7": "-7", "1,000": "1000", r"3,\!110,\!400": "3110400", "90{,}900{,}909": "90900909",
                 r"\$32,\!348": "32348", r"75^\circ": "75", r"80^{\circ}": "80", r"10\%": "10", ".5": "0.5",
                 "x=5": "5", r"18\text{ ways.}": "18", r"12\text{ square units}": "12", "4.50": "4.5", "$18$": "18"}
        for raw, want in cases.items():
            self.assertEqual(pa.normalise_numeric(raw), want, raw)

    def test_normalise_rejects(self):
        for raw in ["2b", "2x", "y = -2x", r"\frac{1}{2}", r"5\pi", r"\sqrt{3}", "(1,2)", "1,00", "Friday",
                    r"\text{A}", "3, -1", "10^3", "", None]:
            self.assertIsNone(pa.normalise_numeric(raw), raw)

    def test_answer_type(self):
        self.assertEqual(pa.answer_type("3"), "integer")
        self.assertEqual(pa.answer_type("-3"), "negative_integer")
        self.assertEqual(pa.answer_type("0.5"), "decimal")

    def test_reference_lookup(self):
        idx = pa.gsm8k_reference_index(fs.pilot_path("data/raw/test.jsonl"))
        it = next(i for i in POOL if i["domain"] == "gsm8k")
        st, ref = pa.lookup_reference(it["question"], idx)
        self.assertEqual((st, pa.normalise_numeric(ref["raw"])), ("ok", it["gold_answer"]))
        self.assertEqual(pa.lookup_reference("not a problem", idx)[0], "no_match")
        conflict = {pa.ws("q"): [{"raw": "1"}, {"raw": "2"}]}
        self.assertEqual(pa.lookup_reference("q", conflict)[0], "multiple_conflicting")

    def test_gold_and_original_differ(self):
        from decimal import Decimal
        for it in POOL:
            self.assertNotEqual(Decimal(it["gold_answer"]), Decimal(it["original_answer"]))
            self.assertEqual(it["gold_answer_type"].endswith("integer"), True)


class TestFalseStep(unittest.TestCase):
    def test_false_step_is_correct_pre_error_step(self):
        for it in POOL:
            fl = it["false_location"]
            self.assertEqual(fl["false_step"], it["true_error_step"] - 1)
            self.assertTrue(1 <= fl["false_step"] < it["true_error_step"])
            self.assertEqual(fl["distance"], it["true_error_step"] - fl["false_step"])

    def test_no_false_step_when_error_at_step_1(self):
        self.assertIsNone(db.false_step_record(["a", "b"], 1))
        with self.assertRaises(ValueError):
            pb.pointed_step({"item_id": "x", "false_location": None, "true_error_step": 1}, "L_false")


class TestDatasetFrozen(unittest.TestCase):
    def test_frozen_hashes_and_determinism(self):
        C.verify_frozen([C.rel(C.ITEMS_FILE), C.rel(C.CANDIDATES_FILE), C.rel(C.DATASET_MANIFEST)])
        rows, pool, reserve = db.build(CFG)
        self.assertEqual([i["item_id"] for i in pool], [i["item_id"] for i in POOL])
        self.assertEqual(len(POOL), CFG["dataset"]["target_items"])
        self.assertEqual(len(POOL), 265)                      # ALL eligible items (researcher decision)
        self.assertEqual(reserve, [])
        self.assertEqual(len(POOL), sum(r["exclusion_reason"] is None for r in rows))
        self.assertEqual({d: sum(i["domain"] == d for i in POOL) for d in ("gsm8k", "math")},
                         {"gsm8k": 121, "math": 144})
        self.assertTrue(all(i["math_level"] for i in POOL if i["domain"] == "math"))

    def test_unique_problems_no_step_conflicts(self):
        self.assertEqual(len({pa.ws(i["question"]) for i in POOL}), len(POOL))
        for it in POOL:
            self.assertFalse(any(db.STEP_REF.search(s) for s in it["steps"]))

    def test_stage_a_and_pilot_untouched_and_solver_identity(self):
        sa = fs.check_solver_matches_stage_a(CFG)
        self.assertEqual(sa["model_revision"], "3101254bbe4169895668a0e7653c3fd1f313576e")
        for relp in CFG["pilot_link"]["files_sha256"]:
            fs.pilot_path(relp, CFG)


# ====================================================================== prompts / requests
class TestStage1Requests(unittest.TestCase):
    def test_unique_and_frozen(self):
        by_key, order, _ = s1.frozen_inputs(CFG)
        self.assertEqual(len(by_key), len(POOL))
        self.assertEqual({r["item_id"] for r in by_key.values()}, {i["item_id"] for i in POOL})
        self.assertEqual(s1.audit(list(by_key.values()), POOL), [])

    def test_prompt_text_exact(self):
        it = POOL[0]
        p = pb.stage1_prompt(it)
        self.assertTrue(p.startswith(f"Problem:\n{it['question']}\n\nOriginal proposed solution:\nStep 1: "))
        self.assertTrue(p.endswith("Feedback:\nThe final answer in the proposed solution is wrong.\n\n"
                                   "Assess the proposed solution carefully and give your best corrected final answer."
                                   "\n\nReturn the final answer on the last line exactly as:\n\nFINAL_ANSWER: <answer>"))

    def test_deterministic_order(self):
        reqs = s1.build_requests(POOL)
        a = s1.execution_order(reqs, 5101)
        self.assertEqual(a, s1.execution_order(list(reversed(reqs)), 5101))
        self.assertEqual(a["order_sha256"], C.read_json(C.STAGE1_ORDER)["order_sha256"])
        self.assertNotEqual(a["order_sha256"], s1.execution_order(reqs, 1)["order_sha256"])

    def test_smoke_selection_frozen(self):
        by_key, order, _ = s1.frozen_inputs(CFG)
        keys = s1.smoke_keys(list(by_key.values()), order, CFG)
        self.assertEqual(keys, C.read_json(C.STAGE1_MANIFEST)["smoke_request_keys"])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), CFG["stage1"]["smoke"]["n_requests"])


class TestStage2Prompts(unittest.TestCase):
    PREV = "Let me redo Step 2 of my own work.\nStep 1: ...\nFINAL_ANSWER: 41"

    def prompts(self, it):
        return {c: pb.stage2_prompt(it, c, self.PREV) for c in pb.STAGE2_CONDITIONS}

    def test_vv_no_location_leakage(self):
        for it in POOL[:50]:
            fb = pb.stage2_feedback(it, "VV")
            self.assertFalse(any(ch.isdigit() for ch in fb))
            self.assertNotIn("step", fb.lower())

    def test_ntrue_no_error_claim(self):
        for it in POOL[:50]:
            sent = pb.condition_sentence("N_true", it["true_error_step"])
            self.assertEqual(pb.find_terms(sent, pb.ERROR_WORDS), [])
            self.assertIn(f"Step {it['true_error_step']} of the ORIGINAL", sent)

    def test_ltrue_and_lfalse_steps(self):
        for it in POOL:
            p = self.prompts(it)
            self.assertIn(f"The first mistake in the ORIGINAL proposed solution is in Step {it['true_error_step']}.",
                          p["L_true"])
            f = it["false_location"]["false_step"]
            self.assertIn(f"The first mistake in the ORIGINAL proposed solution is in Step {f}.", p["L_false"])
            self.assertLess(f, it["true_error_step"])
            self.assertEqual(p["L_true"].replace(f"is in Step {it['true_error_step']}.", "#"),
                             p["L_false"].replace(f"is in Step {f}.", "#"))

    def test_prompts_differ_only_in_condition_sentence(self):
        it = POOL[3]
        p = self.prompts(it)
        shells = {c: v.replace(pb.condition_sentence(c, pb.pointed_step(it, c)), "<S>") for c, v in p.items()}
        self.assertEqual(len(set(shells.values())), 1)
        for v in p.values():
            self.assertEqual(v.count(pb.BEGIN), 1)
            self.assertIn(f"{pb.BEGIN}\n{self.PREV}\n{pb.END}", v)
            self.assertIn(pb.STILL_WRONG, v)

    def test_one_shot_references_supported(self):
        it = POOL[0]
        self.assertNotIn("Feedback", pb.one_shot_prompt(it, "L0_1shot"))
        self.assertIn(f"Step {it['true_error_step']}.", pb.one_shot_prompt(it, "L_true_1shot"))
        self.assertEqual(CFG["optional_one_shot_references"]["enabled"], [])


# ====================================================================== parser / scorer
class TestParserScorer(unittest.TestCase):
    def test_field_rule_a(self):
        self.assertEqual(parser.parser_version(), CFG["parser"]["version"])
        self.assertTrue(parser.parser_version().startswith("stage_b_numeric_v1"))
        self.assertEqual(parser.pilot_parser_version(), "2.0 (strict + numeric)")
        p = parser.parse("work\nFINAL_ANSWER: 1,920")
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("ok", "1920"))
        self.assertEqual(p["numeric_parse_source"], "final_answer_field")
        self.assertTrue(p["strict_format_compliant"])
        self.assertEqual(parser.parse("FINAL_ANSWER: 3 or 4")["numeric_parse_status"], "ambiguous")
        self.assertEqual(parser.parse("FINAL_ANSWER: \\boxed{18}")["numeric_parsed_answer"], "18")
        self.assertEqual(parser.parse("")["numeric_parse_status"], "missing")
        # a label is present -> its field decides; no boxed fallback
        p = parser.parse("Final Answer: \\boxed{5}\nFINAL_ANSWER: none")
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("non_numeric", None))
        # the LAST label is the terminal field
        self.assertEqual(parser.parse("FINAL_ANSWER: 3\nmore\nFINAL_ANSWER: 4")["numeric_parsed_answer"], "4")

    def test_truncated_field_not_accepted(self):
        text = "reasoning ...\nFINAL_ANSWER: 9"
        self.assertEqual(parser.parse(text, truncated=False)["numeric_parsed_answer"], "9")
        p = parser.parse(text, truncated=True)               # field runs to the end of a cut-off generation
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("truncated_field", None))
        self.assertEqual(parser.parse(text + "\n", truncated=True)["numeric_parsed_answer"], "9")

    def test_terminal_boxed_rule_b(self):
        p = parser.parse("work\n\n### Final Answer:\n\n$$\n\\boxed{12}\n$$")
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"], p["numeric_parse_source"]),
                         ("ok", "12", "terminal_boxed"))
        self.assertFalse(p["strict_format_compliant"])       # (f) fallback never makes a response compliant
        self.assertEqual(p["strict_parse_status"], "missing")
        # no 'final answer' phrase -> last paragraph only
        self.assertEqual(parser.parse("a\n\nSo the answer is $\\boxed{1{,}000}$.")["numeric_parsed_answer"], "1000")
        self.assertEqual(parser.parse("x\n\n**Final Answer**\n\\[\n\\boxed{10 \\text{ guests}}\n\\]")
                         ["numeric_parsed_answer"], "10")
        # the same value repeated is compatible
        self.assertEqual(parser.parse("Final answer: \\boxed{7}, i.e. \\boxed{7.0}")["numeric_parsed_answer"], "7")

    def test_no_body_search_rule_c(self):
        body = "We get \\boxed{14} for the first part.\n\n" + "\n".join(f"line {i}" for i in range(10))
        p = parser.parse(body + "\n\nSo that is the count of 3 cases.")
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("missing", None))
        # pilot's last-line fallback would read 7 here; Stage B takes no bare numbers
        self.assertEqual(parser.parse("so 5 then\nthe answer is 7")["numeric_parse_status"], "missing")
        # a 'final answer' phrase far from the end does not open the region
        far = "The final answer is \\boxed{3}.\n\n" + "\n".join(f"check {i}" for i in range(8))
        self.assertEqual(parser.parse(far)["numeric_parse_status"], "missing")

    def test_incomplete_boxed_rule_d(self):
        p = parser.parse("### Final Answer:\n\n$$\n\\boxed{90", truncated=True)
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("incomplete_boxed", None))
        p = parser.parse("### Final Answer:\n\\boxed{4} and \\boxed{\\frac{1")
        self.assertEqual(p["numeric_parse_status"], "incomplete_boxed")

    def test_incompatible_candidates_rule_e(self):
        p = parser.parse("### Final Answer:\n\\boxed{4} or \\boxed{5}")
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("ambiguous", None))
        self.assertEqual(parser.parse("Final answer: \\boxed{4}, \\boxed{x+1}")["numeric_parse_status"], "ambiguous")
        self.assertEqual(parser.parse("Final answer: \\boxed{\\frac{1}{2}}")["numeric_parse_status"], "non_numeric")

    def test_smoke1_regression_cases(self):
        """Verbatim smoke1 responses (fixtures copied from the preserved archive; the archive is not rescored)."""
        doc = C.read_json(C.STAGE_ROOT / "tests" / "fixtures" / "smoke1_parser_cases.json")
        self.assertEqual(doc["source_archive_sha256"], CFG["protocol_amendments"][0]["trigger_archive_sha256"])
        cases = {c["item_id"]: c for c in doc["cases"]}
        want = {"sb_math-64": "0", "sb_math-268": "27", "sb_gsm8k-82": "10", "sb_math-186": "0"}
        for item, ans in want.items():
            c = cases[item]
            self.assertEqual(C.sha256_text(c["raw_response"]), c["raw_response_sha256"])
            p = parser.parse(c["raw_response"], c["finish_reason"] == "length")
            self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"],
                              p["numeric_parse_source"]), ("ok", ans, "terminal_boxed"), item)
            self.assertFalse(p["strict_format_compliant"], item)
        c = cases["sb_math-114"]                              # truncated "\boxed{90"
        self.assertEqual(c["finish_reason"], "length")
        self.assertTrue(c["raw_response"].rstrip().endswith("\\boxed{90"))
        p = parser.parse(c["raw_response"], True)
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("incomplete_boxed", None))
        self.assertEqual(pa_pilot().parse_response(c["raw_response"])["numeric_parsed_answer"], "90")  # old bug
        p = parser.parse(cases["sb_math-470"]["raw_response"], True)  # "### Step 5: Final" (cut)
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("missing", None))
        p = parser.parse(cases["sb_gsm8k-64"]["raw_response"])
        self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]), ("ok", "2000"))
        self.assertFalse(p["strict_format_compliant"])
        for c in doc["cases"]:
            p = parser.parse(c["raw_response"], c["finish_reason"] == "length")
            self.assertEqual((p["numeric_parse_status"], p["numeric_parsed_answer"]),
                             (c["expected_numeric_parse_status"], c["expected_numeric_parsed_answer"]), c["item_id"])

    def test_scorer_uses_truncation_and_new_parser(self):
        s = scorer.score_raw_row(self.row("### Final Answer:\n\\boxed{10}"))
        self.assertTrue(s["repair"])
        self.assertEqual((s["numeric_parse_source"], s["strict_format_compliant"]), ("terminal_boxed", False))
        self.assertTrue(s["parser_version"].startswith("stage_b_numeric_v1"))
        s = scorer.score_raw_row(self.row("### Final Answer:\n\\boxed{10", "length"))
        self.assertEqual(s["outcome"], "parse_failure")
        self.assertTrue(s["truncated_without_valid_answer"])

    def test_classify(self):
        self.assertEqual(scorer.classify("ok", "45000", "45000", "43000"), "repair")
        self.assertEqual(scorer.classify("ok", "43000.0", "45000", "43000"), "original_answer_retention")
        self.assertEqual(scorer.classify("ok", "7", "45000", "43000", "7"), "stage1_answer_retention")
        self.assertEqual(scorer.classify("ok", "7", "45000", "43000"), "new_wrong_answer")
        self.assertEqual(scorer.classify("ambiguous", None, "1", "2"), "parse_failure")

    def row(self, text, finish="stop", stage=1, s1a=None):
        return {"request_key": "k", "stage": stage, "item_id": "i", "domain": "gsm8k", "condition": "V",
                "gold_answer": "10", "original_answer": "12", "stage1_numeric_answer": s1a, "run_index": 0,
                "raw_response": text, "model_metadata": {"response": {"finish_reason": finish, "new_tokens": 1536}}}

    def test_truncation_tracking(self):
        s = scorer.score_raw_row(self.row("long reasoning without an answer", "length"))
        self.assertTrue(s["truncated"])
        self.assertTrue(s["truncated_without_valid_answer"])
        self.assertFalse(s["repair"])
        s = scorer.score_raw_row(self.row("x\nFINAL_ANSWER: 10\nLet me double-check", "length"))
        self.assertTrue(s["truncated"] and s["repair"] and not s["truncated_without_valid_answer"])
        s = scorer.score_raw_row(self.row("x\nFINAL_ANSWER: 10", "length"))   # field may be cut off
        self.assertEqual((s["numeric_parse_status"], s["outcome"]), ("truncated_field", "parse_failure"))

    def test_stage2_retention_flags(self):
        s = scorer.score_raw_row(self.row("FINAL_ANSWER: 9", stage=2, s1a="9"))
        self.assertEqual(s["outcome"], "stage1_answer_retention")
        self.assertTrue(s["retains_stage1_answer"])


# ====================================================================== persistence / runner
class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.d = TMP / "persist"
        if self.d.exists():
            shutil.rmtree(self.d)

    def test_duplicate_prevention(self):
        st = RunStore(self.d)
        done = set()
        st.append({"request_key": "a"}, done)
        with self.assertRaises(DuplicateRequestError):
            st.append({"request_key": "a"}, done)
        with open(st.raw, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"request_key": "a"}) + "\n")
        with self.assertRaises(DuplicateRequestError):
            st.completed_rows()

    def test_torn_final_line_repaired(self):
        st = RunStore(self.d)
        st.append({"request_key": "a"}, set())
        with open(st.raw, "ab") as fh:
            fh.write(b'{"request_key": "b", "trunc')
        rows, note = read_jsonl_tolerant(st.raw)
        self.assertEqual([r["request_key"] for r in rows], ["a"])
        self.assertIn("torn final line", note)

    def test_stage1_resume_never_regenerates(self):
        by_key, order, hashes = s1.frozen_inputs(CFG)
        keys = [o["request_key"] for o in order["order"]][:25]
        solver = MockSolver(list(by_key.values()), 768)
        man = rc.run_manifest("r", 1, "smoke", True, "mock", CFG, {"model": "m", "max_new_tokens": 768}, "x", {},
                              hashes, keys)
        st1 = rc.execute(self.d, by_key, order, keys, solver, man, CFG["runner"], quiet, limit=12)
        self.assertEqual((st1["generated"], st1["complete"]), (12, False))
        first = C.read_jsonl(self.d / "raw_outputs.jsonl")
        st2 = rc.execute(self.d, by_key, order, keys, solver, man, CFG["runner"], quiet)
        self.assertEqual((st2["generated"], st2["complete"]), (13, True))
        st3 = rc.execute(self.d, by_key, order, keys, solver, man, CFG["runner"], quiet)
        self.assertEqual(st3["generated"], 0)
        rows = C.read_jsonl(self.d / "raw_outputs.jsonl")
        self.assertEqual(rows[:12], first)
        self.assertEqual(len({r["request_key"] for r in rows}), 25)
        self.assertTrue((self.d / "checkpoints" / "checkpoint_0010.zip").exists())
        self.assertTrue((self.d / "checkpoints" / "checkpoint_FINAL.zip").exists())
        self.assertEqual(solver.calls, 25)
        bad = dict(man, max_new_tokens=1024)
        with self.assertRaises(SystemExit):
            rc.execute(self.d, by_key, order, keys, solver, bad, CFG["runner"], quiet)

    def test_transport_retry_only(self):
        by_key, order, hashes = s1.frozen_inputs(CFG)
        keys = [o["request_key"] for o in order["order"]][:3]
        solver = MockSolver(list(by_key.values()), 768, fail_transport_keys=[keys[1]])
        man = rc.run_manifest("r", 1, "smoke", True, "mock", CFG, {"model": "m"}, "x", {}, hashes, keys)
        cfg = dict(CFG["runner"], transport_retry_backoff_seconds=0)
        st = rc.execute(self.d, by_key, order, keys, solver, man, cfg, quiet)
        self.assertTrue(st["complete"])
        self.assertEqual(st["transport_failures"], 1)
        row = next(r for r in C.read_jsonl(self.d / "raw_outputs.jsonl") if r["request_key"] == keys[1])
        self.assertEqual(row["attempt_count"], 2)

    def test_real_model_refused_locally(self):
        from src import solver_loader as sl
        import os
        if os.path.isdir("/kaggle/working"):
            self.skipTest("on Kaggle")
        with self.assertRaises(SystemExit):
            sl.make_solver("qwen", CFG)


# ====================================================================== failure freezing / stage 2
class TestFailureSetAndStage2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = Pipeline.build()

    def test_stage1_scoring_and_failures(self):
        scored = scorer.score_rows(C.read_jsonl(self.p.s1_dir / "raw_outputs.jsonl"))
        self.assertEqual(len(scored), len(POOL))
        self.assertEqual({f["item_id"] for f in self.p.fails}, {s["item_id"] for s in scored if not s["repair"]})
        self.assertEqual(self.p.gate["n_stage1_failures"], len(self.p.fails))
        self.assertEqual(self.p.gate["passed"], len(self.p.fails) >= 50)

    def test_no_successful_item_enters_stage2(self):
        scored = {s["item_id"]: s for s in scorer.score_rows(C.read_jsonl(self.p.s1_dir / "raw_outputs.jsonl"))}
        for r in self.p.reqs2:
            self.assertFalse(scored[r["item_id"]]["repair"])
        d = self.p.decisions.values()
        self.assertEqual(len(self.p.reqs2), 3 * sum(x["primary_stage2"] for x in d)
                         + sum(x["l_false_eligible"] for x in d))

    def test_truncated_stage1_failures_carried_into_stage2(self):
        trunc = [f for f in self.p.fails if f["stage1_truncated"]]
        self.assertTrue(trunc)                                   # mock injects truncations
        got = {r["item_id"] for r in self.p.reqs2}
        for f in trunc:
            self.assertNotEqual(f["stage1_outcome"], "repair")   # observed failure, never regenerated
            self.assertEqual(f["item_id"] in got, self.p.decisions[f["item_id"]]["primary_stage2"])

    def test_review_exclusions_applied(self):
        got = {}
        for r in self.p.reqs2:
            got.setdefault(r["item_id"], set()).add(r["condition"])
        for iid, dec in self.p.decisions.items():
            if not dec["primary_stage2"]:
                self.assertNotIn(iid, got)                      # excluded from ALL conditions
            else:
                want = {"VV", "N_true", "L_true"} | ({"L_false"} if dec["l_false_eligible"] else set())
                self.assertEqual(got[iid], want)
        self.assertTrue(any(not d["primary_stage2"] for d in self.p.decisions.values()))
        self.assertTrue(any(d["primary_stage2"] and not d["l_false_eligible"] for d in self.p.decisions.values()))

    def test_stage2_audit_and_shared_stage1_response(self):
        self.assertEqual(s2b.audit(self.p.reqs2, POOL, self.p.fails, self.p.resp, self.p.decisions), [])
        resp = {r["item_id"]: r["stage1_response"].strip() for r in self.p.resp}
        by_item = {}
        for r in self.p.reqs2:
            by_item.setdefault(r["item_id"], set()).add(r["stage1_response_sha256"])
            self.assertIn(f"{pb.BEGIN}\n{resp[r['item_id']]}\n{pb.END}", r["prompt"])
        self.assertTrue(all(len(v) == 1 for v in by_item.values()))

    def test_audit_catches_injected_problems(self):
        reqs = [dict(r) for r in self.p.reqs2]
        vv = next(r for r in reqs if r["condition"] == "VV")
        vv["prompt"] = vv["prompt"].replace(pb.condition_sentence("VV"), "Look at Step 2.")
        extra = dict(reqs[0], item_id=next(i["item_id"] for i in POOL if i["item_id"] not in
                                           {f["item_id"] for f in self.p.fails}))
        probs = s2b.audit(reqs + [extra], POOL, self.p.fails, self.p.resp)
        self.assertTrue(any("leaks a step number" in x for x in probs))
        self.assertTrue(any("did not fail Stage 1" in x for x in probs))
        excluded = next(i for i, d in self.p.decisions.items() if not d["primary_stage2"])
        sneak = dict(reqs[0], item_id=excluded, request_key=f"s2::{excluded}::VV")
        probs = s2b.audit(self.p.reqs2 + [sneak], POOL, self.p.fails, self.p.resp, self.p.decisions)
        self.assertTrue(any("review-determined" in x for x in probs))

    def test_failure_lock_hash_checking(self):
        lock = s2r.verify_lock(self.p.P)
        self.assertEqual(lock["failures_sha256"], C.sha256_file(self.p.P["failures"]))
        tmp = TMP / "tamper"
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(self.p.work, tmp)
        P2 = s2r.paths(tmp)
        doc = C.read_json(P2["failures"])
        doc["item_ids"] = doc["item_ids"][1:]
        C.write_json(P2["failures"], doc)
        with self.assertRaises(SystemExit):
            s2r.verify_lock(P2)
        (P2["lock"]).unlink()
        with self.assertRaises(SystemExit):
            s2r.verify_lock(P2)

    def test_freeze_refuses_partial_and_mock_into_real(self):
        by_key, order, hashes = s1.frozen_inputs(CFG)
        rows, man, _ = s2b.load_stage1_run(self.p.s1_dir)
        self.assertEqual(s2b.verify_stage1_complete(rows, man, by_key, order, hashes), [])
        self.assertTrue(s2b.verify_stage1_complete(rows[:-1], man, by_key, order, hashes))
        bad = [dict(rows[0], prompt=rows[0]["prompt"] + " ")] + rows[1:]
        self.assertTrue(s2b.verify_stage1_complete(bad, man, by_key, order, hashes))
        with self.assertRaises(SystemExit):
            s2r.paths(C.STAGE_ROOT / "data" / "processed")      # test work dirs must be under outputs/test_runs

    def test_stage2_run_complete_and_rows_share_upstream(self):
        self.assertTrue(self.p.s2_stats["complete"])
        man = C.read_json(self.p.s2_dir / "run_manifest.json")
        self.assertEqual(man["upstream_sha256"], C.read_json(self.p.P["lock"])["failures_sha256"])
        self.assertTrue(man["test_only"])

    def test_rule_frozen_and_bound_to_content(self):
        self.assertTrue(s2r.rule_is_frozen(CFG))
        changed = json.loads(json.dumps(CFG))
        changed["interpretation_rule"]["meaningful_effect"] = 0.05
        self.assertFalse(s2r.rule_is_frozen(changed))
        changed = json.loads(json.dumps(CFG))
        changed["analysis"]["bootstrap_seed"] = 1
        self.assertFalse(s2r.rule_is_frozen(changed))

    def test_analysis_mock_is_test_only(self):
        rows1 = scorer.score_rows(C.read_jsonl(self.p.s1_dir / "raw_outputs.jsonl"))
        rows2 = scorer.score_rows(C.read_jsonl(self.p.s2_dir / "raw_outputs.jsonl"))
        T, summary = an.analyse(POOL, rows1, C.read_json(self.p.P["failures"]), self.p.review, rows2, CFG, True)
        self.assertEqual(summary["banner"], C.TEST_ONLY_BANNER)
        self.assertIn("SUPPRESSED", summary["interpretation"]["label"])
        out = an.write_outputs(TMP / "analysis", T, summary)
        for name in ("stage1_metrics", "stage1_failures", "stage2_rescue_metrics", "stage2_paired_comparisons",
                     "failure_decomposition", "cost_and_tokens", "truncation_report", "item_level_sequential_outcomes"):
            self.assertTrue((out / f"{name}.csv").read_text(encoding="utf-8").startswith(f"# {C.TEST_ONLY_BANNER}"))
        self.assertIn(C.TEST_ONLY_BANNER, (out / "REPORT.md").read_text(encoding="utf-8"))
        cost = {r["policy"]: r for r in T["cost_and_tokens"]}
        for r in cost.values():
            self.assertEqual(r["unnecessary_second_call_rate"], 0)
        p = T["stage2_paired_comparisons"][0]
        n_primary = sum(d["primary_stage2"] for d in self.p.decisions.values())
        self.assertEqual(p["both_rescue"] + p["A_only_rescues"] + p["B_only_rescues"] + p["neither_rescues"], n_primary)
        self.assertEqual((p["A"], p["B"], p["role"]), ("L_true", "VV", "PRIMARY"))
        q = T["stage2_paired_comparisons"][1]
        self.assertEqual((q["A"], q["B"]), ("L_true", "L_false"))
        self.assertIn("DIAGNOSTIC-CONTENT", q["role"])
        self.assertEqual(q["n_paired"], sum(d["l_false_eligible"] for d in self.p.decisions.values()))
        self.assertEqual(summary["n_primary_stage2"], n_primary)
        self.assertIn("contamination", summary["limitations"])
        self.assertTrue(any(r["scope"] == "math Level 5" for r in T["stage1_metrics"]))
        with self.assertRaises(SystemExit):
            an.analyse(POOL, rows1, C.read_json(self.p.P["failures"]), self.p.review, rows2[:-1], CFG, True)

    def test_archive_hashing(self):
        import subprocess
        r = subprocess.run([sys.executable, "scripts/12_package_results.py", "--run-dir", str(self.p.s1_dir)],
                           cwd=C.STAGE_ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        z = self.p.s1_dir.parent / "s1_FINAL.zip"
        self.assertTrue(json.loads(r.stdout)["passed"])
        tampered = z.with_name("tampered.zip")
        with zipfile.ZipFile(z) as zin, zipfile.ZipFile(tampered, "w") as zout:
            for n in zin.namelist():
                data = zin.read(n)
                zout.writestr(n, data + b"x" if n.endswith("raw_outputs.jsonl") else data)
        r = subprocess.run([sys.executable, "scripts/12_package_results.py", "--verify", str(tampered)],
                           cwd=C.STAGE_ROOT, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(json.loads(r.stdout)["bad_members"])


# ====================================================================== statistics / rule
class TestStatistics(unittest.TestCase):
    def test_mcnemar(self):
        self.assertEqual(an.mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(an.mcnemar_exact(13, 2), 0.0074, places=4)    # matches the pilot's reported p
        self.assertEqual(an.mcnemar_exact(5, 5), 1.0)

    def test_wilson(self):
        lo, hi = an.wilson(8, 10)
        self.assertAlmostEqual(lo, 0.4902, places=3)
        self.assertAlmostEqual(hi, 0.9433, places=3)

    def test_bootstrap_deterministic_and_paired(self):
        a, b = [1, 1, 0, 1, 0] * 12, [1, 0, 0, 0, 0] * 12
        x = an.paired_bootstrap(a, b, 2000, 7)
        self.assertEqual(x, an.paired_bootstrap(a, b, 2000, 7))
        self.assertTrue(x[0] <= 0.4 <= x[1])
        self.assertEqual(an.paired_bootstrap(a, a, 500, 1), (0.0, 0.0))

    def test_precision_label_rule(self):
        R = CFG["interpretation_rule"]

        def L(d, lo, hi):
            return an.label_outcome({"delta_rescue": d, "delta_rescue_ci_low": lo, "delta_rescue_ci_high": hi},
                                    R)["label"]
        self.assertEqual(L(0.15, 0.02, 0.28), "STRONG")
        self.assertEqual(L(0.10, 0.01, 0.20), "STRONG")
        self.assertEqual(L(0.09, 0.01, 0.18), "INCONCLUSIVE")       # CI excludes 0 but point < +10 pp
        self.assertEqual(L(0.15, -0.02, 0.30), "INCONCLUSIVE")      # CI includes 0 and +10 pp
        self.assertEqual(L(0.02, -0.05, 0.09), "NO SUPPORT")        # CI upper < +10 pp
        self.assertEqual(L(-0.10, -0.20, 0.0), "NO SUPPORT")
        self.assertEqual(L(0.05, -0.05, 0.10), "INCONCLUSIVE")      # upper bound exactly +10 pp is not < +10 pp
        self.assertEqual(L(None, None, None), "NOT COMPUTABLE")
        self.assertNotIn("0.5 *", json.dumps(R))                  # no 'false gain <= half of true gain' requirement


class TestTwoSentenceFeedback(unittest.TestCase):
    def test_identical_first_sentence_second_defines_condition(self):
        for it in POOL:
            seconds = set()
            for c in pb.STAGE2_CONDITIONS:
                fb = pb.stage2_feedback(it, c)
                first, sep, second = fb[:len(pb.STILL_WRONG)], fb[len(pb.STILL_WRONG)], fb[len(pb.STILL_WRONG) + 1:]
                self.assertEqual(first, "Your previous repair attempt is still incorrect.")
                self.assertEqual(sep, " ")
                self.assertEqual(second, pb.condition_sentence(c, pb.pointed_step(it, c)))
                self.assertEqual(second.count(". "), 0)              # exactly one sentence
                self.assertTrue(second.endswith("."))
                seconds.add(second)
                p = pb.stage2_prompt(it, c, "prev")
                self.assertIn(f"\n\nFeedback:\n{fb}\n\n{pb.STAGE2_CLOSING}", p)
            self.assertEqual(len(seconds), 4)


class TestAnnotationReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = Pipeline.build()

    def test_packet_is_blinded(self):
        packet = (self.p.P["review_dir"] / "annotation_review_packet.md").read_text(encoding="utf-8")
        for r in self.p.resp:
            self.assertNotIn(r["stage1_response"].strip()[:120], packet)
        for w in ("stage 1", "stage-1", "repair attempt", "retention", "parse_failure", "new_wrong_answer",
                  "mock reasoning", "final_answer"):
            self.assertNotIn(w, packet.lower())
        form = ar.read_form(self.p.P["review_dir"] / "annotation_review_form.csv")
        self.assertEqual({r["item_id"] for r in form}, {f["item_id"] for f in self.p.fails})
        self.assertNotEqual([r["item_id"] for r in form], sorted(r["item_id"] for r in form))   # seeded shuffle
        items = {i["item_id"]: i for i in POOL}
        for r in form:
            self.assertEqual(int(r["annotated_first_error_step"]), items[r["item_id"]]["true_error_step"])
            self.assertEqual(int(r["proposed_false_step"]), items[r["item_id"]]["true_error_step"] - 1)

    def test_form_validation(self):
        blank = ar.read_form(self.p.P["review_dir"] / "annotation_review_form.csv")
        good = [dict(r, first_error_verdict="CONFIRM", false_step_verdict="CORRECT") for r in blank]
        dec, probs = ar.validate_and_decide(good, blank, CFG)
        self.assertEqual(probs, [])
        self.assertTrue(all(d["primary_stage2"] and d["l_false_eligible"] for d in dec.values()))
        bad = [dict(r) for r in good]
        bad[0]["annotated_first_error_step"] = "99"
        bad[1]["first_error_verdict"] = "maybe"
        bad[2]["false_step_verdict"] = ""
        _, probs = ar.validate_and_decide(bad[:-1], blank, CFG)
        text = "\n".join(probs)
        for frag in ("was edited", "first_error_verdict", "false_step_verdict", "have no row"):
            self.assertIn(frag, text)
        mixed = [dict(good[0], first_error_verdict="DISAGREE", reviewer_first_error_step="2"),
                 dict(good[1], false_step_verdict="NOT_CORRECT")] + good[2:]
        dec, probs = ar.validate_and_decide(mixed, blank, CFG)
        self.assertEqual(probs, [])
        d0, d1 = dec[good[0]["item_id"]], dec[good[1]["item_id"]]
        self.assertEqual((d0["primary_stage2"], d0["l_false_eligible"], d0["reviewer_first_error_step"]),
                         (False, False, 2))
        self.assertEqual((d1["primary_stage2"], d1["l_false_eligible"]), (True, False))

    def test_review_lock_and_order_guards(self):
        tmp = TMP / "review_guard"
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(self.p.work, tmp)
        P2 = s2r.paths(tmp)
        s2r.verify_review_lock(P2)
        f = P2["review_dir"] / "annotation_review_form_FILLED.csv"
        f.write_text(f.read_text(encoding="utf-8").replace("CONFIRM", "DISAGREE", 1), encoding="utf-8")
        with self.assertRaises(SystemExit):
            s2r.verify_review_lock(P2)                       # form changed after freezing
        r = subprocess.run([sys.executable, "scripts/07b_freeze_annotation_review.py", "--reviewer", "x",
                            "--work-dir", str(tmp)], cwd=C.STAGE_ROOT, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)                 # never re-frozen
        P2["review_lock"].unlink()
        with self.assertRaises(SystemExit):
            s2r.verify_review_lock(P2)
        r = subprocess.run([sys.executable, "scripts/08_build_stage2_requests.py", "--work-dir", str(tmp)],
                           cwd=C.STAGE_ROOT, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)                 # Stage-2 build needs a frozen review
        C.write_jsonl(P2["requests"], [])
        r = subprocess.run([sys.executable, "scripts/07b_freeze_annotation_review.py", "--reviewer", "x",
                            "--work-dir", str(tmp)], cwd=C.STAGE_ROOT, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)                 # Stage-2 requests exist -> too late to freeze
        self.assertIn("Stage-2 requests already exist", r.stdout + r.stderr)

    def test_export_requires_frozen_failures(self):
        tmp = TMP / "review_nolock"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        r = subprocess.run([sys.executable, "scripts/07a_export_annotation_review.py", "--work-dir", str(tmp)],
                           cwd=C.STAGE_ROOT, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


class TestSmokeGate(unittest.TestCase):
    T = {"completed": True, "no_persistent_technical_failures": True, "prompts_match": True}
    TR = {"no_truncation": True, "no_response_over_90pct": True}

    def d(self, parsed, tech=None, trunc=None):
        return sg.decide(tech or self.T, trunc or self.TR, 20, parsed, 1, 2)["decision"]

    def test_parse_thresholds(self):
        self.assertEqual(self.d(20), "GO")
        self.assertEqual(self.d(19), "GO")
        self.assertEqual(self.d(18), "REVIEW")
        self.assertEqual(self.d(17), "NO-GO")
        sm = CFG["stage1"]["smoke"]
        self.assertEqual((sm["parse_go_min"], sm["parse_review_min"], sm["headroom_max_fraction"]), (19, 18, 0.9))
        self.assertEqual(sm["n_requests"], 20)

    def test_technical_and_truncation_are_no_go(self):
        self.assertEqual(self.d(20, tech=dict(self.T, no_persistent_technical_failures=False)), "NO-GO")
        self.assertEqual(self.d(20, tech=dict(self.T, prompts_match=False)), "NO-GO")
        self.assertEqual(self.d(20, trunc=dict(self.TR, no_truncation=False)), "NO-GO")
        self.assertEqual(self.d(20, trunc=dict(self.TR, no_response_over_90pct=False)), "NO-GO")
        self.assertEqual(sg.EXIT_CODES, {"GO": 0, "NO-GO": 2, "REVIEW": 4})

    def test_mock_smoke_end_to_end(self):
        r = subprocess.run([sys.executable, "scripts/05_stage1_smoke_test.py", "--solver", "mock",
                            "--output-root", str(TMP / "smoke")], cwd=C.STAGE_ROOT, capture_output=True, text=True)
        gate = C.read_json(TMP / "smoke" / "smoke3_stage1_mock" / "smoke_gate.json")
        self.assertEqual(r.returncode, sg.EXIT_CODES[gate["decision"]])
        self.assertTrue(gate["TEST_ONLY"])
        self.assertTrue(all(gate["technical_checks"].values()))     # mock: persistence/resume/determinism OK
        self.assertEqual((gate["smoke_run_label"], gate["max_new_tokens"], gate["headroom_limit_tokens"]),
                         ("smoke3", 2048, 0.9 * 2048))
        self.assertEqual(gate["headroom_limit_tokens"], 1843.2)
        self.assertEqual(gate["protocol_amendments"], ["A1", "A2", "A3", "A4", "A5", "A6", "A7"])
        self.assertTrue(gate["parser_version"].startswith("stage_b_numeric_v1"))


def pa_pilot():
    return fs.pilot_module("parse_answers")


class TestAmendmentA1(unittest.TestCase):
    def test_amended_settings_and_preserved_protocol(self):
        self.assertEqual(CFG["solver"]["stage_b_overrides"], {"max_new_tokens": 2048})     # since A2 (A1: 1536)
        am = CFG["protocol_amendments"][0]
        self.assertEqual(am["id"], "A1")
        self.assertEqual(am["trigger_archive_sha256"],
                         "c8e965fb87402da84f9298ce584aa91842c702fa02059757d92e5234cafd81b1")
        self.assertEqual(CFG["solver"]["model_revision"], "3101254bbe4169895668a0e7653c3fd1f313576e")
        sm = CFG["stage1"]["smoke"]
        self.assertEqual((sm["n_requests"], sm["parse_go_min"], sm["parse_review_min"], sm["headroom_max_fraction"]),
                         (20, 19, 18, 0.9))
        self.assertEqual(CFG["stage2"]["conditions"], ["VV", "N_true", "L_true", "L_false"])
        # same 20 smoke requests as smoke1 (frozen manifest; selection unchanged)
        by_key, order, _ = s1.frozen_inputs(CFG)
        self.assertEqual(s1.smoke_keys(list(by_key.values()), order, CFG),
                         C.read_json(C.STAGE1_MANIFEST)["smoke_request_keys"])
        doc = C.read_json(C.STAGE_ROOT / "tests" / "fixtures" / "smoke1_parser_cases.json")
        smoke = set(C.read_json(C.STAGE1_MANIFEST)["smoke_request_keys"])
        self.assertTrue(all(c["request_key"] in smoke for c in doc["cases"]))

    def test_registered_and_verified(self):
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + [C.rel(C.PROTOCOL_AMENDMENTS)])
        rec = C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"][0]
        self.assertEqual(rec["id"], "A1")
        self.assertEqual(rec["max_new_tokens_after"], 1536)
        self.assertEqual(rec["trigger_smoke_gate_quoted"]["decision"], "NO-GO")
        self.assertEqual(rec["trigger_smoke_gate_quoted"]["max_new_tokens"], 768)
        log = C.read_json(C.FROZEN_HASHES)["change_log"]
        for r in C.AMENDED_PROTOCOL_FILES:
            self.assertTrue(any(e["file"] == r and "A1" in e["reason"] and e["old_sha256"] != e["new_sha256"]
                                for e in log), r)

    def test_rule_lock_unchanged(self):
        self.assertTrue(s2r.rule_is_frozen(CFG))

    def test_manifest_records_parser(self):
        man = rc.run_manifest("r", 1, "smoke", True, "mock", CFG, {"model": "m", "max_new_tokens": 2048}, "x", {},
                              {"requests": "a", "order": "b", "items": "c"}, ["k"])
        self.assertTrue(man["parser_version"].startswith("stage_b_numeric_v1"))
        self.assertEqual(man["parser_sha256"], C.sha256_file(C.STAGE_ROOT / "src" / "parser.py"))
        self.assertEqual(man["protocol_amendments"], ["A1", "A2", "A3", "A4", "A5", "A6", "A7"])
        for f in ("parser_version", "parser_sha256", "protocol_amendments"):
            self.assertIn(f, rc.IDENTITY_FIELDS)


class TestAmendmentA2(unittest.TestCase):
    SMOKE1 = "c8e965fb87402da84f9298ce584aa91842c702fa02059757d92e5234cafd81b1"
    SMOKE2 = "e95729e470f55f67cb59595d7c60e3cbf8c09b1bff9d88f1cc876cb671d58c59"

    def test_only_max_new_tokens_changed(self):
        am = CFG["protocol_amendments"][1]
        self.assertEqual((am["id"], am["trigger_archive_sha256"], am["trigger_smoke_label"]), ("A2", self.SMOKE2, "smoke2"))
        self.assertEqual(am["changes"], ["solver.stage_b_overrides.max_new_tokens 1536 -> 2048 (Stage 1 AND Stage 2)"])
        self.assertEqual(CFG["solver"]["stage_b_overrides"], {"max_new_tokens": 2048})
        self.assertEqual(CFG["stage1"]["smoke"]["run_label"], "smoke3")
        self.assertEqual(CFG["stage1"]["smoke"]["headroom_max_fraction"] * 2048, 1843.2)
        self.assertEqual(CFG["solver"]["model_revision"], "3101254bbe4169895668a0e7653c3fd1f313576e")
        self.assertEqual(CFG["solver"]["frozen_config_sha256"],
                         "3118f86d5e0a1fae7228c7db086b2e2ba15210c23d67ee3dc0560ab01c98a77f")   # decoding settings

    def test_registered_parser_and_inputs_untouched(self):
        rec = C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]
        self.assertEqual([r["id"] for r in rec], ["A1", "A2", "A3", "A4", "A5", "A6", "A7"])
        a2 = rec[1]
        self.assertEqual((a2["max_new_tokens_before"], a2["max_new_tokens_after"], a2["headroom_limit_tokens_after"]),
                         (1536, 2048, 1843.2))
        self.assertEqual(a2["trigger_smoke_gate_quoted"]["decision"], "NO-GO")
        self.assertEqual(a2["trigger_smoke_gate_quoted"]["n_parsed"], 19)
        self.assertEqual({(o["request_key"], o["new_tokens"]) for o in a2["trigger_responses_over_headroom"]},
                         {("s1::sb_math-150::V", 1536), ("s1::sb_math-7::V", 1422)})
        # parser byte-identical to what ran smoke2 (and to A1's registered version)
        self.assertEqual(a2["files_before_sha256"]["src/parser.py"], a2["files_after_sha256"]["src/parser.py"])
        self.assertEqual(a2["files_after_sha256"]["src/parser.py"], rec[0]["files_after_sha256"]["src/parser.py"])
        self.assertEqual(a2["files_before_sha256"]["configs/stage_b.json"], rec[0]["files_after_sha256"]["configs/stage_b.json"])
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + [C.rel(C.PROTOCOL_AMENDMENTS), C.rel(C.ITEMS_FILE),
                        C.rel(C.STAGE1_REQUESTS), C.rel(C.STAGE1_ORDER), C.rel(C.STAGE1_MANIFEST), C.rel(C.RULE_LOCK)])
        log = C.read_json(C.FROZEN_HASHES)["change_log"]
        self.assertTrue(any(e["file"] == "configs/stage_b.json" and "A2" in e["reason"]
                            and e["old_sha256"] == a2["files_before_sha256"]["configs/stage_b.json"]
                            and e["new_sha256"] == a2["files_after_sha256"]["configs/stage_b.json"] for e in log))
        self.assertFalse(any(e["file"] == "src/parser.py" and "A2" in e["reason"] for e in log))

    def test_smoke_records_preserved_byte_for_byte(self):
        # the archives stay local (not shipped in the Kaggle bundle); inside a bundle only the registration is checked
        in_bundle = (C.STAGE_ROOT.parent / "manifest.json").exists()
        for label, h in (("smoke1", self.SMOKE1), ("smoke2", self.SMOKE2)):
            r = f"data/smoke_records/{label}/stage_b_{label}_RESULTS.zip"
            self.assertEqual(C.read_json(C.FROZEN_HASHES)["files"][r], h)
            if not in_bundle:
                self.assertEqual(C.sha256_file(C.STAGE_ROOT / r), h)

    def test_same_20_smoke_requests(self):
        by_key, order, _ = s1.frozen_inputs(CFG)
        keys = s1.smoke_keys(list(by_key.values()), order, CFG)
        self.assertEqual(len(keys), 20)
        self.assertEqual(keys, C.read_json(C.STAGE1_MANIFEST)["smoke_request_keys"])
        self.assertIn("s1::sb_math-150::V", keys)
        self.assertIn("s1::sb_math-7::V", keys)


class TestFrozenHashGuard(unittest.TestCase):
    def test_change_needs_reason_and_is_logged(self):
        doc = C.read_json(C.FROZEN_HASHES)
        k = C.rel(C.ITEMS_FILE)
        with self.assertRaises(SystemExit):
            C.update_frozen_hashes({k: "0" * 64})
        self.assertEqual(C.read_json(C.FROZEN_HASHES)["files"], doc["files"])
        self.assertTrue(any(e["file"] == k and "265" in e["reason"] for e in doc["change_log"]))
        self.assertTrue(C.RULE_LOCK.exists())
        C.verify_frozen([C.rel(C.RULE_LOCK), k, C.rel(C.STAGE1_REQUESTS), C.rel(C.STAGE1_ORDER)])


class TestLLMReviewA3(unittest.TestCase):
    """A3 blinded LLM-assisted review, reviewer per A4 (mock reviewer only - TEST ONLY)."""
    REV = "a09a35458c702b33eeacc393d103063234e8bc28"

    @classmethod
    def setUpClass(cls):
        from src import llm_review as lr
        from src import llm_review_runner as lrr
        cls.lr, cls.lrr = lr, lrr
        cls.p1 = C.read_jsonl(lrr.PASS_FILES[1])
        cls.p2 = C.read_jsonl(lrr.PASS_FILES[2])
        cls.items = {i["item_id"]: i for i in POOL}

    def run_script(self, *args):
        import os
        return subprocess.run([sys.executable, *args], cwd=C.STAGE_ROOT, capture_output=True, text=True,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def test_amendment_and_reviewer_config(self):
        am = {a["id"]: a for a in CFG["protocol_amendments"]}["A3"]
        self.assertIsNone(am["trigger_archive"])
        self.assertIn("never be described as human-validated", am["trigger"])
        r = CFG["llm_review"]["reviewer"]
        self.assertEqual((r["model"], r["model_revision"]), ("Qwen/Qwen2.5-7B-Instruct", self.REV))
        self.assertEqual(r["quantization"], {"method": "bitsandbytes", "load_in_4bit": True,
                                             "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": "float16",
                                             "bnb_4bit_use_double_quant": False})
        self.assertEqual((r["batch_size"], r["decoding"], r["max_context_tokens"], r["max_new_tokens"],
                          r["repetition_penalty"]), (1, "greedy (do_sample=False, num_beams=1)", 32768, 192, 1.0))
        self.assertTrue(s2r.rule_is_frozen(CFG))
        rec = {a["id"]: a for a in C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]}["A3"]
        self.assertEqual(rec["files_before_sha256"]["src/parser.py"], rec["files_after_sha256"]["src/parser.py"])
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + self.lrr.CODE_FILES + [C.rel(p) for p in self.lrr.FROZEN_FILES]
                        + [C.rel(C.STAGE1_FAILURES), C.rel(C.STAGE1_FAILURES_LOCK), C.rel(C.ITEMS_FILE)])
        self.assertEqual(C.read_json(C.STAGE1_FAILURES)["n_failures"], 76)

    def test_requests_frozen_and_rebuild_identical(self):
        r = self.run_script("scripts/14_build_llm_review_requests.py", "--check")
        if C.STAGE2_REQUESTS.exists():          # after A5/A6 the Stage-2 requests exist: 14 refuses by design
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Stage-2 requests exist", r.stdout + r.stderr)
            C.verify_frozen([C.rel(p) for p in self.lrr.FROZEN_FILES])          # LLM requests still untouched
        else:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((len(self.p1), len(self.p2)), (76, 76))
        order = C.read_json(self.lrr.ORDER_FILE)["item_ids"]
        self.assertEqual(order, ar.packet_order(C.read_json(C.STAGE1_FAILURES)["item_ids"], 6303))
        self.assertEqual([r["item_id"] for r in self.p1], order)

    def test_pass1_blinded(self):
        lr = self.lr
        self.assertEqual(lr.audit(self.p1, self.p2, POOL,
                                  C.read_jsonl(C.DATA_PROC / "stage1_failure_responses.jsonl")), [])
        for r in self.p1:
            it = self.items[r["item_id"]]
            for f in ("true_error_step", "false_location", "label_0indexed", "stage1_outcome"):
                self.assertNotIn(f, r)
            mutated = dict(it, true_error_step=1, label_0indexed=0, false_location={"false_step": 0})
            self.assertEqual(r["prompt"], lr.pass1_prompt(lr.pass1_view(mutated)))
            self.assertIn(f"Reference (correct) final answer: {it['gold_answer']}", r["prompt"])
            self.assertIn(lr.PASS1_QUESTION, r["prompt"])
        with self.assertRaises(AssertionError):            # the builder refuses any non-whitelisted field
            lr.pass1_prompt({**lr.pass1_view(self.items[self.p1[0]["item_id"]]), "true_error_step": 2})

    def test_pass2_shows_steps_1_to_f_only(self):
        for r in self.p2:
            it = self.items[r["item_id"]]
            f = it["false_location"]["false_step"]
            self.assertEqual(f, it["true_error_step"] - 1)
            self.assertIn(f"Candidate step: Step {f}", r["prompt"])
            self.assertNotIn(f"Step {f + 1}: ", r["prompt"])
            self.assertNotIn("Reference (correct) final answer", r["prompt"])
            self.assertIn(self.lr.PASS2_QUESTION, r["prompt"])

    def test_parse_and_mapping_rules(self):
        P, lr = self.lr.parse_output, self.lr
        self.assertEqual(P('{"reason": "r", "earliest_error_step": 3, "confidence": "high"}', 1, 5),
                         {"parse_status": "ok", "judgment": 3, "confidence": "HIGH", "reason": "r"})
        self.assertEqual(P('```json\n{"earliest_error_step": "2", "confidence": "LOW"}\n```', 1, 5)["judgment"], 2)
        self.assertEqual(P('{"earliest_error_step": "no_error", "confidence": "LOW"}', 1, 5)["judgment"], "NO_ERROR")
        self.assertEqual(P('{"earliest_error_step": 6, "confidence": "LOW"}', 1, 5)["parse_status"],
                         "invalid_judgment")
        self.assertEqual(P('{"earliest_error_step": true, "confidence": "LOW"}', 1, 5)["parse_status"],
                         "invalid_judgment")
        self.assertEqual(P('Step 3 is wrong.', 1, 5)["parse_status"], "no_json")
        self.assertEqual(P('{"earliest_error_step": 2} {"earliest_error_step": 3}', 1, 5)["parse_status"], "ambiguous")
        self.assertEqual(P('{"earliest_error_step": 2, "confidence": "maybe"}', 1, 5)["parse_status"],
                         "invalid_confidence")
        self.assertEqual(P('{"candidate_step_verdict": "not_correct", "confidence": "Medium"}', 2)["judgment"],
                         "NOT_CORRECT")
        self.assertEqual(P('{"candidate_step_verdict": "WRONG", "confidence": "LOW"}', 2)["parse_status"],
                         "invalid_judgment")
        self.assertEqual([lr.map_first_error(j, 3) for j in (3, 2, 4, "NO_ERROR", "UNCLEAR", None)],
                         ["CONFIRM", "DISAGREE", "DISAGREE", "DISAGREE", "UNCLEAR", "UNCLEAR"])
        self.assertEqual([lr.map_false_step(j) for j in ("CORRECT", "NOT_CORRECT", "UNCLEAR", None)],
                         ["CORRECT", "NOT_CORRECT", "UNCLEAR", "UNCLEAR"])

    def test_smoke_selection_and_context(self):
        man = C.read_json(self.lrr.REQUEST_MANIFEST)
        self.assertEqual(man["smoke_item_ids"], self.lr.smoke_items(self.p1, 5))
        self.assertEqual(len(set(man["smoke_item_ids"])), 5)
        self.assertEqual({self.items[i]["domain"] for i in man["smoke_item_ids"]}, {"gsm8k", "math"})
        longest = max(self.p1, key=lambda r: (r["prompt_chars"], r["item_id"]))["item_id"]
        self.assertEqual(man["smoke_item_ids"][0], longest)
        self.assertTrue(self.lr.render_chat("x").startswith("<|im_start|>system\n" + self.lr.SYSTEM_PROMPT))
        self.assertNotIn("boxed", self.lr.render_chat("x"))

    @staticmethod
    def model_load_allowed_here():
        import os
        return bool(os.environ.get("KAGGLE_KERNEL_RUN_TYPE")) or os.path.isdir("/kaggle/working") \
            or C.env_flag("STAGE_B_ALLOW_MODEL_LOAD")

    @unittest.skipIf(model_load_allowed_here.__func__(), "Kaggle/GPU host: the real reviewer is permitted here "
                     "(refusal is covered by the simulated tests below)")
    def test_real_reviewer_refused_locally(self):
        """Unpatched: on a local / unsupported machine the real reviewer is refused before anything loads."""
        from src import reviewer_loader as rl
        with self.assertRaises(SystemExit):
            rl.make_reviewer("qwen_instruct", CFG)

    def test_reviewer_refused_off_kaggle_simulated(self):
        """Environment-independent: with no Kaggle marker and no override, loading is refused (nothing imported)."""
        import os
        from unittest import mock
        from src import reviewer_loader as rl
        with mock.patch.dict(os.environ), mock.patch("os.path.isdir", return_value=False):
            os.environ.pop("KAGGLE_KERNEL_RUN_TYPE", None)
            os.environ.pop("STAGE_B_ALLOW_MODEL_LOAD", None)
            with self.assertRaises(SystemExit):
                rl.make_reviewer("qwen_instruct", CFG)

    def test_reviewer_refused_on_kaggle_without_cuda_simulated(self):
        import os
        import types
        from unittest import mock
        from src import reviewer_loader as rl
        fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
        with mock.patch.dict(os.environ, {"KAGGLE_KERNEL_RUN_TYPE": "Batch"}), \
                mock.patch.dict(sys.modules, {"torch": fake_torch}):
            with self.assertRaises(SystemExit):
                rl.make_reviewer("qwen_instruct", CFG)

    def fake_load(self, revision=None, quant=None, bnb_version=None, n4=1, ctx=32768, device="cuda:0",
                  default_template=False):
        """Run the UNCHANGED QwenReviewer.__init__ against fake torch / transformers / bitsandbytes / Hub
        helpers (no weights, no GPU). -> (reviewer or SystemExit, captured BitsAndBytesConfig kwargs)."""
        import types
        from unittest import mock
        from src import reviewer_loader as rl
        rc = CFG["llm_review"]["reviewer"]
        q = dict(rc["quantization"], **(quant or {}))
        captured = {}

        class Linear4bit:
            pass

        class Tok:
            pad_token_id, eos_token_id = 0, 0

            def apply_chat_template(self, messages, tokenize, add_generation_prompt):
                sys_msg = ("You are Qwen, created by Alibaba Cloud. You are a helpful assistant." if default_template
                           else messages[0]["content"])
                return (f"<|im_start|>system\n{sys_msg}<|im_end|>\n<|im_start|>user\n{messages[1]['content']}"
                        "<|im_end|>\n<|im_start|>assistant\n")

        method = types.SimpleNamespace(value=q.get("method", "bitsandbytes"))
        qc = types.SimpleNamespace(quant_method=method, load_in_4bit=q["load_in_4bit"],
                                   bnb_4bit_quant_type=q["bnb_4bit_quant_type"],
                                   bnb_4bit_compute_dtype="torch." + q["bnb_4bit_compute_dtype"],
                                   bnb_4bit_use_double_quant=q["bnb_4bit_use_double_quant"])
        model = types.SimpleNamespace(
            config=types.SimpleNamespace(quantization_config=qc, max_position_embeddings=ctx),
            generation_config=types.SimpleNamespace(eos_token_id=[151645, 151643]), device=device,
            eval=lambda: None, modules=lambda: [Linear4bit() for _ in range(n4)] + [object()])

        def bnb_config(**kw):
            captured.update(kw)
            return types.SimpleNamespace(**kw)

        fake_tf = types.SimpleNamespace(
            AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda p: Tok()),
            AutoModelForCausalLM=types.SimpleNamespace(from_pretrained=lambda p, **kw: model),
            BitsAndBytesConfig=bnb_config)
        fake_torch = types.SimpleNamespace(use_deterministic_algorithms=lambda *a, **k: None,
                                           manual_seed=lambda s: None, float16="torch.float16",
                                           bfloat16="torch.bfloat16")
        fake_bnb = types.SimpleNamespace(nn=types.SimpleNamespace(Linear4bit=Linear4bit),
                                         __version__=bnb_version or rc["bitsandbytes_version"])
        fake_be = types.SimpleNamespace(
            verify_pinned_revision=lambda repo, rev: revision or rev,
            pinned_snapshot=lambda repo, rev: Path("/hub/snapshots") / rev,
            package_versions=lambda names: {n: None for n in names})
        with mock.patch.dict(sys.modules, {"torch": fake_torch, "transformers": fake_tf, "bitsandbytes": fake_bnb}), \
                mock.patch.object(rl, "guard_real_model", lambda: None), \
                mock.patch.object(rl.fs, "pilot_module", lambda name, *a: fake_be):
            try:
                return rl.QwenReviewer(rc), captured
            except SystemExit as e:
                return e, captured

    def test_reviewer_allowed_only_with_registered_configuration(self):
        """Kaggle-side (runs everywhere, no model): the loader accepts ONLY the registered revision, NF4 config,
        bitsandbytes version, device, context and fixed chat rendering; any deviation STOPs before any request."""
        rc = CFG["llm_review"]["reviewer"]
        ok, kw = self.fake_load()
        self.assertNotIsInstance(ok, SystemExit, str(ok))
        self.assertEqual(kw, {"load_in_4bit": True, "bnb_4bit_quant_type": "nf4",
                              "bnb_4bit_compute_dtype": "torch.float16", "bnb_4bit_use_double_quant": False})
        self.assertEqual(ok.hub_revision, rc["model_revision"])
        self.assertEqual(ok.applied_quantization["bnb_4bit_quant_type"], "nf4")
        self.assertEqual(ok.applied_quantization["n_linear4bit_modules"], 1)
        self.assertEqual(ok.generation_kwargs(), {"max_new_tokens": 192, "do_sample": False, "num_beams": 1,
                                                  "temperature": None, "top_p": None, "top_k": None,
                                                  "repetition_penalty": 1.0})
        for bad in (dict(revision="0" * 40), dict(quant={"bnb_4bit_quant_type": "fp4"}),
                    dict(quant={"bnb_4bit_compute_dtype": "bfloat16"}), dict(quant={"bnb_4bit_use_double_quant": True}),
                    dict(quant={"load_in_4bit": False}), dict(quant={"method": "gptq"}), dict(n4=0),
                    dict(bnb_version="0.49.0"), dict(ctx=4096), dict(device="cpu"), dict(default_template=True)):
            got, _ = self.fake_load(**bad)
            self.assertIsInstance(got, SystemExit, f"accepted a non-registered configuration: {bad}")

    def test_reviewer_refuses_changed_prompts_or_config(self):
        """The review runner only starts on the registered prompt / request / config hashes."""
        from unittest import mock
        self.lrr.frozen_inputs()                                       # registered files verify
        real = C.sha256_file
        for target in (self.lrr.PASS_FILES[1], self.lrr.PASS_FILES[2], self.lrr.ORDER_FILE, C.CONFIG_PATH,
                       C.STAGE_ROOT / "src" / "llm_review.py"):
            with mock.patch.object(C, "sha256_file", lambda p, t=target: "0" * 64 if Path(p) == Path(t) else real(p)):
                with self.assertRaises(SystemExit, msg=str(target)):
                    self.lrr.frozen_inputs()
        man = C.read_json(self.lrr.REQUEST_MANIFEST)
        self.assertEqual(man["system_prompt_sha256"], C.sha256_text(self.lr.SYSTEM_PROMPT))
        self.assertEqual(man["pass1_prompts_sha256"], C.sha256_text("\n".join(r["prompt_sha256"] for r in self.p1)))
        self.assertEqual(man["pass2_prompts_sha256"], C.sha256_text("\n".join(r["prompt_sha256"] for r in self.p2)))

    def test_mock_smoke_gate(self):
        out = TMP / "llm_review_smoke"
        shutil.rmtree(out, ignore_errors=True)
        r = self.run_script("scripts/15_llm_review_smoke.py", "--reviewer", "mock", "--output-root",
                            str(out / "good"), "--mock-profile", "good")
        gate = C.read_json(out / "good" / "review_smoke_mock" / "review_smoke_gate.json")
        self.assertEqual((r.returncode, gate["decision"]), (0, "GO"), r.stdout[-2000:] + r.stderr[-2000:])
        self.assertTrue(gate["TEST_ONLY"])
        self.assertEqual(gate["n_generations"], 20)
        self.assertTrue(gate["deterministic_repeat_identical_raw_text"])
        self.lrr.read_archive(out / "good" / "stage_b_llm_review_smoke_RESULTS.zip")
        r = self.run_script("scripts/15_llm_review_smoke.py", "--reviewer", "mock", "--output-root",
                            str(out / "bad"), "--mock-profile", "bad_json")
        gate = C.read_json(out / "bad" / "review_smoke_mock" / "review_smoke_gate.json")
        self.assertEqual((r.returncode, gate["decision"]), (2, "NO-GO"))
        r = self.run_script("scripts/15_llm_review_smoke.py", "--reviewer", "mock", "--output-root",
                            str(out / "good"), "--mock-profile", "good")
        self.assertNotEqual(r.returncode, 0)                 # never re-run into an existing smoke folder

    def test_mock_full_review_and_freeze(self):
        work = TMP / "llm_review_full"
        shutil.rmtree(work, ignore_errors=True)
        w = work / "stage2_inputs"
        w.mkdir(parents=True)
        for f in (C.STAGE1_FAILURES, C.DATA_PROC / "stage1_failure_responses.jsonl", C.STAGE1_FAILURES_LOCK):
            shutil.copyfile(f, w / f.name)
        r = self.run_script("scripts/07a_export_annotation_review.py", "--work-dir", str(w))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_script("scripts/16_run_llm_review.py", "--reviewer", "mock", "--output-root", str(work / "out"))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        z = work / "out" / "stage_b_llm_review_FINAL.zip"
        data = self.lrr.read_archive(z)
        self.assertTrue(any(n.endswith("PASS1_COMPLETE.json") for n in data))
        real_filled = C.DATA_PROC / "annotation_review" / "annotation_review_form_FILLED.csv"
        existed = real_filled.exists()
        r = self.run_script("scripts/17_freeze_llm_review.py", "--source", str(z))      # mock -> never into data/
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(real_filled.exists(), existed)
        r = self.run_script("scripts/17_freeze_llm_review.py", "--source", str(z), "--work-dir", str(w))
        self.assertIn(r.returncode, (0, 3), r.stdout[-3000:] + r.stderr[-3000:])
        filled = ar.read_form(w / "annotation_review" / "annotation_review_form_FILLED.csv")
        blank = ar.read_form(w / "annotation_review" / "annotation_review_form.csv")
        self.assertEqual([x["item_id"] for x in filled], [x["item_id"] for x in blank])
        self.assertTrue(all("NOT human-validated" in x["notes"] for x in filled))
        doc = C.read_json(w / "annotation_review" / "llm_review_judgments.json")
        for rec in doc["records"]:
            self.assertEqual(rec["first_error_verdict"],
                             self.lr.map_first_error(rec["pass1"]["judgment"], rec["annotated_first_error_step"]))
            if rec["first_error_verdict"] != "DISAGREE":
                self.assertIsNone(rec["reviewer_first_error_step"])
        rep = doc["report"]
        self.assertEqual(sum(rep["first_error_verdicts"].values()), 76)
        self.assertEqual(r.returncode, 0 if rep["n_primary_stage2"] >= 50 else 3)
        r2 = self.run_script("scripts/17_freeze_llm_review.py", "--source", str(z), "--work-dir", str(w))
        self.assertNotEqual(r2.returncode, 0)                # the filled form is never overwritten
        # tampering: PASS1_COMPLETE no longer bound to the Pass-1 raw outputs -> refused
        bad = work / "tampered.zip"
        with zipfile.ZipFile(z) as zi, zipfile.ZipFile(bad, "w") as zo:
            for n in zi.namelist():
                b = zi.read(n)
                if n.endswith("PASS1_COMPLETE.json"):
                    d = json.loads(b)
                    d["pass1_raw_outputs_sha256"] = "0" * 64
                    b = json.dumps(d).encode()
                zo.writestr(n, b)
        (w / "annotation_review" / "annotation_review_form_FILLED.csv").unlink()
        r3 = self.run_script("scripts/17_freeze_llm_review.py", "--source", str(bad), "--work-dir", str(w))
        self.assertNotEqual(r3.returncode, 0)


class TestReviewerReplacementA4(unittest.TestCase):
    """A4: A3 reviewer smoke NO-GO -> Qwen2.5-7B-Instruct; same blinded two-pass procedure, items and user prompts."""
    TRIGGER = "9205bdda17367f4fb71e4f2520193a95d94af2cf02b11b65cab2da53a0bc934a"
    RECORD = "data/smoke_records/review_smoke_a3/stage_b_llm_review_smoke_RESULTS.zip"
    OLD_SYSTEM = "611b8aac8e8cb7f5b661f6aad75008117ad6854c24bfdedb1647e6e4fdaa1309"

    @classmethod
    def setUpClass(cls):
        from src import llm_review as lr
        from src import llm_review_runner as lrr
        cls.lr, cls.lrr = lr, lrr
        cls.fixture = C.read_json(C.STAGE_ROOT / "tests" / "fixtures" / "review_smoke_a3_outputs.json")

    def test_amendment_registered_and_trigger_preserved(self):
        am = {a["id"]: a for a in CFG["protocol_amendments"]}["A4"]
        self.assertEqual((am["trigger_archive_sha256"], am["trigger_kind"]), (self.TRIGGER, "review_smoke"))
        rec = {a["id"]: a for a in C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]}
        self.assertEqual(list(rec)[:4], ["A1", "A2", "A3", "A4"])            # later amendments append
        a4 = rec["A4"]
        self.assertEqual(a4["trigger_smoke_gate_quoted"]["decision"], "NO-GO")
        self.assertFalse(a4["trigger_smoke_gate_quoted"]["checks"]["pass1_json_parses"])
        self.assertEqual(a4["reviewer_before"]["model"], "Qwen/Qwen2.5-Math-7B-Instruct")
        self.assertEqual(a4["reviewer_after"]["model"], "Qwen/Qwen2.5-7B-Instruct")
        for f in ("src/parser.py", "data/processed/llm_review/review_order.json"):
            self.assertEqual(a4["files_before_sha256"][f], a4["files_after_sha256"][f])
        reg = C.read_json(C.FROZEN_HASHES)
        self.assertEqual(reg["files"][self.RECORD], self.TRIGGER)
        if not (C.STAGE_ROOT.parent / "manifest.json").exists():          # record is local only (not in the bundle)
            self.assertEqual(C.sha256_file(C.STAGE_ROOT / self.RECORD), self.TRIGGER)
        for f in ("configs/stage_b.json", "src/llm_review.py", "src/reviewer_loader.py",
                  "data/processed/llm_review/pass1_requests.jsonl"):
            self.assertTrue(any(e["file"] == f and "A4" in e["reason"]
                                and e["old_sha256"] == a4["files_before_sha256"][f]
                                and e["new_sha256"] == a4["files_after_sha256"][f] for e in reg["change_log"]), f)
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + self.lrr.CODE_FILES + [C.rel(p) for p in self.lrr.FROZEN_FILES]
                        + [C.rel(C.PROTOCOL_AMENDMENTS), C.rel(C.STAGE1_FAILURES), C.rel(C.ITEMS_FILE)])

    def test_reviewer_settings(self):
        r = CFG["llm_review"]["reviewer"]
        self.assertEqual((r["model"], r["model_revision"]),
                         ("Qwen/Qwen2.5-7B-Instruct", "a09a35458c702b33eeacc393d103063234e8bc28"))
        self.assertEqual(r["quantization"], {"method": "bitsandbytes", "load_in_4bit": True,
                                             "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": "float16",
                                             "bnb_4bit_use_double_quant": False})
        self.assertEqual((r["bitsandbytes_version"], r["device"], r["batch_size"], r["max_new_tokens"],
                          r["repetition_penalty"], r["tools"], r["web_access"]),
                         ("0.50.2", "cuda:0", 1, 192, 1.0, None, None))
        self.assertEqual(parser.parser_version(), CFG["parser"]["version"])
        self.assertEqual(C.sha256_file(C.STAGE_ROOT / "src" / "parser.py"),
                         "69353ea9a64bbd7566afcdc0f6e8b536d6130f2dacad10fe1b89bd6006586b90")
        from src import reviewer_loader as rl
        with self.assertRaises(SystemExit):                  # the replaced A3 reviewer can no longer be selected
            rl.make_reviewer("qwen_math", CFG)

    def test_only_system_prompt_changed(self):
        lr = self.lr
        man = C.read_json(self.lrr.REQUEST_MANIFEST)
        self.assertEqual(lr.PROMPT_VERSION, "llm_review_prompts_v2")
        self.assertNotEqual(C.sha256_text(lr.SYSTEM_PROMPT), self.OLD_SYSTEM)
        self.assertEqual(man["supersedes"]["system_prompt_sha256"], self.OLD_SYSTEM)
        # user prompts / questions / smoke items / order byte-identical to A3 (prompt version v1)
        self.assertEqual(man["pass1_prompts_sha256"], "9509b2c15cc905af275cfb5002bd2e968fe9dcf8def2c772edf0a2818b41c3b2")
        self.assertEqual(man["pass2_prompts_sha256"], "be565e311799a09c8d4dfb7de0750b9959681770ceddd39eb314b380c2f68d2e")
        self.assertEqual(man["pass1_question_sha256"], C.sha256_text(lr.PASS1_QUESTION))
        self.assertEqual(man["pass1_question_sha256"], "0af8d77168bd0f03b46c68dce1ebaeee387ada5db2e3f51501cc097802def9fb")
        self.assertEqual(man["pass2_question_sha256"], "187d0eec31da264a285c6baa472ffd1a480d0b675b5078197574c32458ba6093")
        self.assertEqual(man["smoke_item_ids"], ["sb_math-186", "sb_gsm8k-132", "sb_math-478", "sb_gsm8k-31",
                                                 "sb_math-133"])
        self.assertEqual(C.sha256_file(self.lrr.ORDER_FILE),
                         "9a136f68ac8cc69c4a537372768fad2634b533bbdce2f52fc93f13741f63b95d")
        for p in (1, 2):
            for r in C.read_jsonl(self.lrr.PASS_FILES[p]):
                self.assertEqual(r["rendered_sha256"], C.sha256_text(lr.render_chat(r["prompt"])))
        s = lr.SYSTEM_PROMPT
        for must in ("classification task", "not a request to solve the problem", "exactly one JSON object"):
            self.assertIn(must, s)
        self.assertNotIn("step by step", s)

    def test_exact_schema(self):
        X = self.lr.exact_schema
        self.assertEqual(X('{"reason": "r", "earliest_error_step": 3, "confidence": "HIGH"}', 1, 5), (True, "ok"))
        self.assertTrue(X(' {"reason": "r", "earliest_error_step": "NO_ERROR", "confidence": "low"}\n', 1, 5)[0])
        self.assertTrue(X('```json\n{"reason": "r", "candidate_step_verdict": "NOT_CORRECT", "confidence": "LOW"}\n```',
                          2)[0])
        for bad, p in (('Here: {"reason": "r", "earliest_error_step": 3, "confidence": "HIGH"}', 1),
                       ('{"reason": "r", "earliest_error_step": 3, "confidence": "HIGH"} done', 1),
                       ('{"earliest_error_step": 3, "confidence": "HIGH"}', 1),
                       ('{"reason": "r", "earliest_error_step": 3, "confidence": "HIGH", "extra": 1}', 1),
                       ('{"reason": "r", "earliest_error_step": 9, "confidence": "HIGH"}', 1),
                       ('{"reason": "r", "earliest_error_step": 3, "confidence": "SURE"}', 1),
                       ('{"reason": 1, "earliest_error_step": 3, "confidence": "HIGH"}', 1),
                       ('{"reason": "r", "candidate_step_verdict": "corresponding", "confidence": "high"}', 2),
                       ('[{"reason": "r", "candidate_step_verdict": "CORRECT", "confidence": "HIGH"}]', 2)):
            self.assertFalse(X(bad, p, 5)[0], bad)

    def test_trigger_outputs_stay_unusable(self):
        """The A3 outputs (prose + boxed pseudo-JSON) are never turned into classifications."""
        fx = self.fixture
        self.assertEqual(fx["source_sha256"], self.TRIGGER)
        self.assertEqual(len(fx["cases"]), 10)
        for c in fx["cases"]:
            self.assertEqual(self.lr.parse_output(c["raw_response"], c["review_pass"], c["n_steps"])["parse_status"],
                             "no_json", c["request_key"])
            self.assertFalse(self.lr.exact_schema(c["raw_response"], c["review_pass"], c["n_steps"])[0])

    def smoke(self, j1, j2, j1r=None, j2r=None):
        by_key = {p: {r["request_key"]: r for r in C.read_jsonl(self.lrr.PASS_FILES[p])} for p in (1, 2)}
        ids = [x["item_id"] for x in j1]
        raw1 = [by_key[1][self.lr.request_key(1, i)] for i in ids]
        return self.lrr.smoke_checks(POOL, CFG, j1, j1r or j1, j2, j2r or j2, raw1, by_key, ids,
                                     {"TEST_ONLY": True}, 0, True)

    def judg(self, text, p, item, finish="stop"):
        n = {i["item_id"]: i for i in POOL}[item]["n_steps"]
        exact, why = self.lr.exact_schema(text, p, n)
        return {"item_id": item, **self.lr.parse_output(text, p, n), "exact_schema": exact,
                "exact_schema_detail": why, "finish_reason": finish, "prompt_tokens": 100, "new_tokens": 40}

    def test_smoke_gate_requires_exact_schema(self):
        ids = C.read_json(self.lrr.REQUEST_MANIFEST)["smoke_item_ids"]
        ok1 = '{"reason": "r", "earliest_error_step": "UNCLEAR", "confidence": "LOW"}'
        ok2 = '{"reason": "r", "candidate_step_verdict": "UNCLEAR", "confidence": "LOW"}'
        j1 = [self.judg(ok1, 1, i) for i in ids]
        j2 = [self.judg(ok2, 2, i) for i in ids]
        g = self.smoke(j1, j2)
        self.assertEqual(g["decision"], "GO", g["reason"])
        wrapped = [self.judg("Answer: " + ok1, 1, ids[0])] + j1[1:]          # parses 'ok', but not exact schema
        self.assertEqual(wrapped[0]["parse_status"], "ok")
        g = self.smoke(wrapped, j2)
        self.assertEqual(g["decision"], "NO-GO")
        self.assertFalse(g["checks"]["pass1_exact_schema"])
        g = self.smoke(j1, j2, j2r=[self.judg(ok2[:-1] + ', "x": 1}', 2, ids[0])] + j2[1:])
        self.assertEqual(g["decision"], "NO-GO")                           # the repeat run must be exact too
        g = self.smoke(j1, [self.judg(ok2, 2, ids[0], finish="length")] + j2[1:])
        self.assertFalse(g["checks"]["no_truncation"])
        fx = {(c["review_pass"], c["item_id"]): c for c in self.fixture["cases"]}
        a3 = {p: [self.judg(fx[p, i]["raw_response"], p, i, fx[p, i]["finish_reason"]) for i in ids] for p in (1, 2)}
        g = self.smoke(a3[1], a3[2])
        self.assertEqual(g["decision"], "NO-GO")
        for k in ("pass1_json_parses", "pass1_exact_schema", "pass2_exact_schema", "pass2_json_parses_and_valid",
                  "pass2_candidate_step_verdict_valid", "no_truncation"):
            self.assertFalse(g["checks"][k], k)


class TestHumanReviewA5(unittest.TestCase):
    """A5: A4 reviewer smoke NO-GO -> LLM reviewer STOPPED; blinded HUMAN two-pass review (simulated reviewer - TEST)."""
    TRIGGER = "dd15c0cb7bcb2ace9cf1132cbca2e44d9ff0993988590e604f2d015035c9fca9"
    RECORD = "data/smoke_records/review_smoke_a4/stage_b_llm_review_smoke_RESULTS.zip"

    @classmethod
    def setUpClass(cls):
        from src import human_review as hr
        from src import llm_review_runner as lrr
        cls.hr, cls.lrr = hr, lrr
        cls.fails = C.read_json(C.STAGE1_FAILURES)
        cls.items = {i["item_id"]: i for i in POOL}
        cls.k = {i: cls.items[i]["true_error_step"] for i in cls.fails["item_ids"]}

    def run_script(self, *args):
        import os
        return subprocess.run([sys.executable, *args], cwd=C.STAGE_ROOT, capture_output=True, text=True,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    @staticmethod
    def server_module():
        import importlib.util
        if str(C.STAGE_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(C.STAGE_ROOT / "scripts"))                  # for the script's _bootstrap import
        spec = importlib.util.spec_from_file_location("srv19", C.STAGE_ROOT / "scripts" / "19_human_review_server.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def workdir(self, name):
        w = TMP / name / "stage2_inputs"
        shutil.rmtree(w.parent, ignore_errors=True)
        w.mkdir(parents=True)
        for f in (C.STAGE1_FAILURES, C.DATA_PROC / "stage1_failure_responses.jsonl", C.STAGE1_FAILURES_LOCK):
            shutil.copyfile(f, w / f.name)
        r = self.run_script("scripts/07a_export_annotation_review.py", "--work-dir", str(w))
        self.assertEqual(r.returncode, 0, r.stderr)
        return w

    def freeze(self, w, p, reviewer="Tester"):
        return self.run_script("scripts/20_freeze_human_review.py", "--pass", str(p), "--reviewer", reviewer,
                               "--work-dir", str(w))

    def fill(self, P, p, judge, reviewer="Tester"):
        view = C.read_json(P["view"][p])
        sha = C.sha256_file(P["view"][p])
        for x in view["items"]:
            j, conf = judge(x)
            self.hr.append(P["log"][p], view, sha, p, reviewer, x["item_id"], j, conf, f"reason {x['index']}",
                           C.now())

    def test_amendment_registered_and_trigger_preserved(self):
        am = {a["id"]: a for a in CFG["protocol_amendments"]}["A5"]
        self.assertEqual((am["trigger_archive_sha256"], am["trigger_kind"]), (self.TRIGGER, "review_smoke_stop"))
        self.assertIn("self-contradictory", am["trigger"])
        rec = {a["id"]: a for a in C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]}
        self.assertEqual(list(rec)[:5], ["A1", "A2", "A3", "A4", "A5"])
        a5 = rec["A5"]
        self.assertEqual(a5["trigger_smoke_gate_quoted"]["decision"], "NO-GO")
        self.assertTrue(a5["trigger_smoke_gate_quoted"]["checks"]["no_truncation"])
        self.assertTrue(a5["trigger_smoke_gate_quoted"]["checks"]["deterministic_repeat_same_structured_judgment"])
        self.assertEqual(a5["llm_reviewer_stopped"]["model"], "Qwen/Qwen2.5-7B-Instruct")
        reg = C.read_json(C.FROZEN_HASHES)
        self.assertEqual(reg["files"][self.RECORD], self.TRIGGER)
        if not (C.STAGE_ROOT.parent / "manifest.json").exists():          # records are local only
            self.assertEqual(C.sha256_file(C.STAGE_ROOT / self.RECORD), self.TRIGGER)
            self.assertEqual(C.sha256_file(C.STAGE_ROOT / TestReviewerReplacementA4.RECORD),
                             TestReviewerReplacementA4.TRIGGER)
        for r, h in a5["unchanged_files_sha256"].items():                  # LLM-review code / requests untouched
            self.assertEqual(reg["files"][r], h, r)
        self.assertTrue(any(e["file"] == "configs/stage_b.json" and "A5" in e["reason"] for e in reg["change_log"]))
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + ["src/human_review.py", C.rel(C.PROTOCOL_AMENDMENTS),
                                                    C.rel(C.STAGE1_FAILURES), C.rel(C.ITEMS_FILE)]
                        + self.lrr.CODE_FILES + [C.rel(p) for p in self.lrr.FROZEN_FILES])
        self.assertEqual(CFG["llm_review"]["stopped_by"], "A5")

    def test_llm_reviewer_refused(self):
        for s in ("scripts/15_llm_review_smoke.py", "scripts/16_run_llm_review.py"):
            r = self.run_script(s, "--reviewer", "qwen_instruct", "--output-root", str(TMP / "x"))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("stopped by amendment A5", r.stdout + r.stderr)
        r = self.run_script("scripts/17_freeze_llm_review.py", "--source", "nonexistent.zip")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stopped by amendment A5", r.stdout + r.stderr)

    def test_questions_and_mapping_unchanged(self):
        from src import llm_review as lr
        self.assertEqual(self.hr.QUESTIONS, {1: lr.PASS1_QUESTION, 2: lr.PASS2_QUESTION})
        self.assertEqual(C.sha256_text(lr.PASS1_QUESTION),
                         "0af8d77168bd0f03b46c68dce1ebaeee387ada5db2e3f51501cc097802def9fb")
        self.assertEqual(C.sha256_text(lr.PASS2_QUESTION),
                         "187d0eec31da264a285c6baa472ffd1a480d0b675b5078197574c32458ba6093")
        for j, k, v in ((3, 3, "CONFIRM"), (2, 3, "DISAGREE"), ("NO_ERROR", 3, "DISAGREE"), ("UNCLEAR", 3, "UNCLEAR")):
            self.assertEqual(self.hr.map_first_error(j, k), v)

    def test_pass1_view_blinded(self):
        doc = self.hr.pass1_view_doc(POOL, self.fails, CFG, False)
        self.assertEqual(doc["n_items"], 76)
        self.assertEqual(doc["item_ids"], C.read_json(self.lrr.ORDER_FILE)["item_ids"])       # same packet order
        self.assertEqual(self.hr.audit_view(doc, POOL, C.read_jsonl(C.DATA_PROC / "stage1_failure_responses.jsonl")),
                         [])
        keys = set()
        for x in doc["items"]:
            self.assertEqual(tuple(x), self.hr.PASS1_ITEM_KEYS)
            self.assertEqual(x["steps"], self.items[x["item_id"]]["steps"])
            keys |= set(x)
        self.assertEqual(set(doc), {"pass", "TEST_ONLY", "question", "n_items", "item_ids", "items"})
        self.assertFalse(keys & {"true_error_step", "label_0indexed", "false_location", "true_error_relative_position",
                                 "processbench_id", "generator"})
        bad = json.loads(json.dumps(doc))
        bad["items"][0]["true_error_step"] = 3
        self.assertTrue(self.hr.audit_view(bad, POOL))
        resp = [{"item_id": doc["items"][1]["item_id"], "stage1_response": "Z" * 30 + " leaked stage one text " * 5}]
        bad = json.loads(json.dumps(doc))
        bad["items"][1]["original_answer"] = resp[0]["stage1_response"]
        self.assertTrue(any("Stage-1" in p for p in self.hr.audit_view(bad, POOL, resp)))

    def test_pass2_view_steps_1_to_f(self):
        doc = self.hr.pass2_view_doc(POOL, self.fails, CFG, False)
        self.assertEqual(self.hr.audit_view(doc, POOL), [])
        for x in doc["items"]:
            it = self.items[x["item_id"]]
            f = it["false_location"]["false_step"]
            self.assertEqual((x["candidate_step"], len(x["steps"]), f), (f, f, it["true_error_step"] - 1))
            self.assertEqual(tuple(x), self.hr.PASS2_ITEM_KEYS)
        bad = json.loads(json.dumps(doc))
        bad["items"][0]["steps"] = self.items[bad["items"][0]["item_id"]]["steps"]
        self.assertTrue(self.hr.audit_view(bad, POOL))

    def test_log_validation_chain_and_damage(self):
        hr = self.hr
        d = TMP / "hr_log"
        shutil.rmtree(d, ignore_errors=True)
        view = hr.pass1_view_doc(POOL, self.fails, CFG, True)
        x = view["items"][0]
        log = d / "pass1_log.jsonl"
        for j, c, reason in ((0, "HIGH", ""), (len(x["steps"]) + 1, "HIGH", ""), ("maybe", "HIGH", ""),
                             (1, "SURE", ""), (1, "HIGH", "x" * 501)):
            with self.assertRaises(ValueError):
                hr.append(log, view, "v", 1, "T", x["item_id"], j, c, reason, None)
        with self.assertRaises(ValueError):
            hr.append(log, view, "v", 1, "T", "sb_not_an_item", 1, "HIGH", "", None)
        with self.assertRaises(ValueError):
            hr.append(log, view, "v", 1, " ", x["item_id"], 1, "HIGH", "", None)
        with self.assertRaises(ValueError):
            hr.append(log, view, "v", 2, "T", x["item_id"], "MAYBE", "HIGH", "", None)
        self.assertFalse(log.exists())
        e0 = hr.append(log, view, "v", 1, "T", x["item_id"], "1", "high", " a\n b ", "t0")
        self.assertEqual((e0["judgment"], e0["confidence"], e0["reason"], e0["seq"]), (1, "HIGH", "a b", 0))
        hr.append(log, view, "v", 1, "T", x["item_id"], "no_error", "LOW", "", "t1")         # revision
        hr.append(log, view, "v", 1, "T", view["items"][1]["item_id"], "UNCLEAR", "MEDIUM", "", "t2")
        entries = hr.read_log(log)
        cur, n = hr.current(entries)
        self.assertEqual((cur[x["item_id"]]["judgment"], n[x["item_id"]], len(entries)), ("NO_ERROR", 2, 3))
        self.assertEqual(entries[0]["judgment"], 1)                                          # history kept
        self.assertTrue(any("no judgment" in p for p in hr.completeness(view, entries, "v")))
        self.assertTrue(any("different view" in p for p in hr.completeness(view, entries, "other")))
        good = log.read_text(encoding="utf-8")
        log.write_text(good.replace('"LOW"', '"HIGH"', 1), encoding="utf-8", newline="\n")    # edited by hand
        with self.assertRaises(SystemExit):
            hr.read_log(log)
        log.write_text(good + '{"seq": 3', encoding="utf-8", newline="\n")                      # interrupted write
        with self.assertRaises(SystemExit):
            hr.read_log(log)
        lines = good.splitlines(keepends=True)
        log.write_text(lines[0] + lines[2], encoding="utf-8", newline="\n")                     # deleted revision
        with self.assertRaises(SystemExit):
            hr.read_log(log)

    def test_end_to_end_two_passes(self):
        hr, k = self.hr, self.k
        w = self.workdir("hr_e2e")
        P = hr.paths(w)
        self.assertNotEqual(self.freeze(w, 1).returncode, 0)                 # nothing exported yet
        for _ in range(2):                                                    # re-export = byte-identical no-op
            r = self.run_script("scripts/18_export_human_review.py", "--work-dir", str(w))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(P["view"][2].exists())                               # Pass-2 view only after Pass-1 freeze
        self.assertNotIn("true_error_step", P["view"][1].read_text(encoding="utf-8"))
        srv = self.server_module()
        with self.assertRaises(SystemExit):
            srv.load(P, 2)
        view = C.read_json(P["view"][1])
        sha = C.sha256_file(P["view"][1])
        for x in view["items"][:30]:                                          # a first session, stopped part-way
            hr.append(P["log"][1], view, sha, 1, "Tester", x["item_id"], "UNCLEAR", "LOW", "first look", None)
        r = self.freeze(w, 1)
        self.assertNotEqual(r.returncode, 0)                                  # incomplete -> refused
        self.assertIn("no judgment yet", r.stdout + r.stderr)

        def judge1(x):                                                        # simulated reviewer, every path
            h = int(C.sha256_text(x["item_id"])[:6], 16) % 10
            j = k[x["item_id"]] if h < 7 else ("NO_ERROR" if h == 7 else ("UNCLEAR" if h == 8 else 1))
            return j, hr.CONFIDENCE[h % 3]
        self.fill(P, 1, judge1)                                               # resumed: all 76 (30 revised)
        self.assertNotEqual(self.freeze(w, 1, "Other").returncode, 0)        # reviewer must match the log
        r = self.freeze(w, 1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        l1 = C.read_json(P["pass_lock"][1])
        self.assertEqual((l1["n_items"], l1["n_log_lines"], l1["n_revisions"]), (76, 106, 30))
        self.assertEqual(l1["log_sha256"], C.sha256_file(P["log"][1]))
        mp = C.read_json(P["mapping"])
        self.assertEqual(mp["pass1_lock_sha256"], C.sha256_file(P["pass_lock"][1]))
        self.assertLessEqual(l1["frozen"], mp["computed"])
        fin = {x["item_id"]: x for x in C.read_json(P["final"][1])["rows"]}
        for row in mp["rows"]:
            self.assertEqual(row["first_error_verdict"], hr.map_first_error(fin[row["item_id"]]["judgment"],
                                                                            k[row["item_id"]]))
            if row["first_error_verdict"] != "DISAGREE":
                self.assertIsNone(row["reviewer_first_error_step"])
        self.assertEqual(set(mp["counts"]), {"CONFIRM", "DISAGREE", "UNCLEAR"})
        with self.assertRaises(SystemExit):
            srv.load(P, 1)                                                    # Pass 1 frozen: page refuses
        self.assertNotEqual(self.freeze(w, 1).returncode, 0)                 # never re-frozen
        v2, _ = srv.load(P, 2)
        self.assertEqual(hr.audit_view(v2, POOL), [])
        self.assertNotEqual(self.freeze(w, 2).returncode, 0)                 # Pass 2 incomplete
        self.fill(P, 2, lambda x: (hr.PASS2_VERDICTS[int(C.sha256_text(x["item_id"])[:4], 16) % 3], "MEDIUM"))
        r = self.freeze(w, 2)
        self.assertIn(r.returncode, (0, 3), r.stdout[-3000:] + r.stderr[-3000:])
        filled_p = P["review_dir"] / "annotation_review_form_FILLED.csv"
        filled, blank = ar.read_form(filled_p), ar.read_form(P["review_dir"] / "annotation_review_form.csv")
        _, probs = ar.validate_and_decide(filled, blank, CFG)
        self.assertEqual(probs, [])
        self.assertTrue(all("HUMAN" in x["notes"] for x in filled))
        f2 = {x["item_id"]: x for x in C.read_json(P["final"][2])["rows"]}
        mrow = {x["item_id"]: x for x in mp["rows"]}
        for x in filled:
            self.assertEqual(x["first_error_verdict"], mrow[x["item_id"]]["first_error_verdict"])
            self.assertEqual(x["false_step_verdict"], f2[x["item_id"]]["judgment"])
            self.assertEqual(x["annotated_first_error_step"], str(k[x["item_id"]]))   # never re-pointed
        fl = C.read_json(P["final_lock"])
        for key, path in (("filled_form_sha256", filled_p), ("judgments_sha256", P["judgments"]),
                          ("pass1_log_sha256", P["log"][1]), ("pass2_log_sha256", P["log"][2]),
                          ("pass1_mapping_sha256", P["mapping"])):
            self.assertEqual(fl[key], C.sha256_file(path), key)
        rep = C.read_json(P["judgments"])["report"]
        self.assertEqual(r.returncode, 0 if rep["n_primary_stage2"] >= 50 else 3)
        self.assertEqual(sum(rep["first_error_verdicts"].values()), 76)
        self.assertNotEqual(self.freeze(w, 2).returncode, 0)                 # never overwritten
        self.assertFalse(P["requests"].exists())                              # no Stage-2 request built
        r = self.run_script("scripts/07b_freeze_annotation_review.py", "--reviewer", "Tester", "--work-dir", str(w))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)               # the unchanged 07b accepts the form

    def test_review_page_http(self):
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request
        hr = self.hr
        w = self.workdir("hr_http")
        P = hr.paths(w)
        r = self.run_script("scripts/18_export_human_review.py", "--work-dir", str(w))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        srv = self.server_module()
        view, sha = srv.load(P, 1)
        st = srv.State(P, 1, "Tester", view, sha, 500)
        httpd = srv.HTTPServer(("127.0.0.1", 0), srv.make_handler(st))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            x = view["items"][0]
            page = urllib.request.urlopen(base + "/").read().decode("utf-8")
            self.assertIn(x["item_id"], page)
            self.assertIn("NO_ERROR", page)
            for w_ in ("candidate", "annotat", "processbench", "stage 1", "stage-1"):
                self.assertNotIn(w_, page.lower(), w_)
            body = urllib.parse.urlencode({"item_id": x["item_id"], "idx": 0, "judgment": "2",
                                           "confidence": "MEDIUM", "reason": "r", "opened_at": "t"}).encode()
            urllib.request.urlopen(base + "/save", data=body).read()
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/save", data=urllib.parse.urlencode(
                    {"item_id": x["item_id"], "idx": 0, "judgment": "99", "confidence": "HIGH"}).encode())
            log = hr.read_log(P["log"][1])
            self.assertEqual([(e["item_id"], e["judgment"], e["reviewer"], e["opened_at"]) for e in log],
                             [(x["item_id"], 2, "Tester", "t")])
            page = urllib.request.urlopen(base + "/item/0").read().decode("utf-8")
            self.assertIn("1 save(s)", page)
            self.assertIn("1/76 judged", page)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestPass2WaiverA6(unittest.TestCase):
    """A6: separate Pass 2 waived; decisions from the frozen human Pass 1 only (simulated reviewer in TEST dirs)."""
    PASS1 = {"data/processed/human_review/pass1_log.jsonl": "665d552f690e1a4c35dcf6b2ecbec3bc6376add34d4ee709b26f3a6701b41682",
             "data/processed/human_review/pass1_final.json": "ae0cd5ba7f9336bf86683e18e51b6f513f4b9bbc478b9f406c787b232fe9152b",
             "data/processed/human_review/pass1_view.json": "50fcd8880380286679b26d0bbebc650f31c71ce2ce1a02ebf18f9552e53f6369",
             "data/manifests/HUMAN_REVIEW_PASS1_FROZEN.json": "bf074582214403804475ca03096b589920ce11e6e8c68501b2700bf997b10cbc"}

    def run_script(self, *args):
        import os
        return subprocess.run([sys.executable, *args], cwd=C.STAGE_ROOT, capture_output=True, text=True,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def test_amendment_registered_pass1_untouched(self):
        am = {a["id"]: a for a in CFG["protocol_amendments"]}["A6"]
        for must in ("waived for feasibility because it would duplicate the same reviewer's earlier judgment",
                     "Pass-1-implied false-step validity", "61", "15 DISAGREE",
                     "paired rescue difference V -> L_true vs V -> V"):
            self.assertIn(must, am["wording"])
        rec = {a["id"]: a for a in C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]}
        self.assertEqual(list(rec), ["A1", "A2", "A3", "A4", "A5", "A6", "A7"])
        a6 = rec["A6"]
        reg = C.read_json(C.FROZEN_HASHES)
        for r, h in self.PASS1.items():
            self.assertEqual((a6["pass1_files_sha256_unchanged"][r], reg["files"][r]), (h, h), r)
            if not (C.STAGE_ROOT.parent / "manifest.json").exists():
                self.assertEqual(C.sha256_file(C.STAGE_ROOT / r), h, r)
        self.assertFalse(any(e["file"] in self.PASS1 for e in reg["change_log"]))       # never re-registered
        self.assertTrue(s2r.rule_is_frozen(CFG))                                        # endpoint/rule unchanged
        self.assertEqual(CFG["analysis"]["primary_comparison"], ["L_true", "VV"])
        self.assertEqual(a6["interpretation_rule_sha256"], C.read_json(C.RULE_LOCK)["rule_sha256"])
        C.verify_frozen(C.AMENDED_PROTOCOL_FILES + list(a6["code_files_sha256"]) + [C.rel(C.PROTOCOL_AMENDMENTS)])

    def test_real_pass2_refused(self):
        r = self.run_script("scripts/19_human_review_server.py", "--pass", "2", "--reviewer", "x", "--no-browser")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("waived by amendment A6", r.stdout + r.stderr)
        r = self.run_script("scripts/20_freeze_human_review.py", "--pass", "2", "--reviewer", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("waived by amendment A6", r.stdout + r.stderr)

    def test_decision_rule(self):
        blank = [{"item_id": "a", "annotated_first_error_step": "3", "proposed_false_step": "2"},
                 {"item_id": "b", "annotated_first_error_step": "4", "proposed_false_step": "3"},
                 {"item_id": "c", "annotated_first_error_step": "2", "proposed_false_step": "1"}]
        filled = [dict(blank[0], first_error_verdict="CONFIRM", false_step_verdict="PASS1_IMPLIED"),
                  dict(blank[1], first_error_verdict="DISAGREE", false_step_verdict="NOT_REVIEWED",
                       reviewer_first_error_step="2"),
                  dict(blank[2], first_error_verdict="CONFIRM", false_step_verdict="CORRECT")]
        d, probs = ar.validate_and_decide(filled, blank, CFG)
        self.assertEqual(probs, [])
        self.assertEqual([(d[i]["primary_stage2"], d[i]["l_false_eligible"]) for i in "abc"],
                         [(True, True), (False, False), (True, True)])
        self.assertIn("Pass-1-implied false-step validity", d["a"]["false_step_validity_basis"])
        self.assertIsNone(d["b"]["false_step_validity_basis"])
        self.assertEqual(d["b"]["reviewer_first_error_step"], 2)

    def test_pass1_only_flow_to_stage2_requests(self):
        from src import human_review as hr
        a5 = TestHumanReviewA5("test_end_to_end_two_passes")
        a5.setUpClass()
        w = a5.workdir("hr_a6")
        P = hr.paths(w)
        r = self.run_script("scripts/18_export_human_review.py", "--work-dir", str(w))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = a5.k

        def judge1(x):
            h = int(C.sha256_text(x["item_id"])[:6], 16) % 10
            return (k[x["item_id"]] if h < 8 else ("NO_ERROR" if h == 8 else 1)), "HIGH"
        a5.fill(P, 1, judge1)
        r = a5.freeze(w, 1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pass1 = {p: C.sha256_file(p) for p in (P["log"][1], P["final"][1], P["mapping"], P["pass_lock"][1])}
        r = self.run_script("scripts/21_freeze_pass1_only_review.py", "--reviewer", "Other", "--work-dir", str(w))
        self.assertNotEqual(r.returncode, 0)                                  # must be the Pass-1 reviewer
        r = self.run_script("scripts/21_freeze_pass1_only_review.py", "--reviewer", "Tester", "--work-dir", str(w))
        self.assertIn(r.returncode, (0, 3), r.stdout[-3000:] + r.stderr[-3000:])
        self.assertEqual({p: C.sha256_file(p) for p in pass1}, pass1)        # Pass 1 untouched
        self.assertFalse(P["log"][2].exists())
        mp = {x["item_id"]: x for x in C.read_json(P["mapping"])["rows"]}
        filled = ar.read_form(P["review_dir"] / "annotation_review_form_FILLED.csv")
        for x in filled:
            conf = mp[x["item_id"]]["first_error_verdict"] == "CONFIRM"
            self.assertEqual(x["first_error_verdict"], mp[x["item_id"]]["first_error_verdict"])
            self.assertEqual(x["false_step_verdict"], "PASS1_IMPLIED" if conf else "NOT_REVIEWED")
            self.assertEqual(x["annotated_first_error_step"], str(k[x["item_id"]]))
            self.assertIn("Pass 2 waived", x["notes"])
        n_conf = sum(v["first_error_verdict"] == "CONFIRM" for v in mp.values())
        self.assertNotEqual(self.run_script("scripts/21_freeze_pass1_only_review.py", "--reviewer", "Tester",
                                            "--work-dir", str(w)).returncode, 0)      # never overwritten
        r = self.run_script("scripts/07b_freeze_annotation_review.py", "--reviewer", "Tester", "--work-dir", str(w))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_script("scripts/08_build_stage2_requests.py", "--work-dir", str(w), "--allow-underpowered")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        reqs = C.read_jsonl(P["requests"])
        man = C.read_json(P["manifest"])
        self.assertEqual(man["n_items"], n_conf)
        self.assertEqual(man["requests_by_condition"], {c: n_conf for c in ("L_false", "L_true", "N_true", "VV")})
        self.assertEqual(len(reqs), 4 * n_conf)
        self.assertTrue(all(mp[r_["item_id"]]["first_error_verdict"] == "CONFIRM" for r_ in reqs))
        self.assertIn("would duplicate the same reviewer's earlier", man["amendment_A6"]["statement"])
        self.assertEqual(list(man["l_false_validity_basis"]), [
            "Pass-1-implied false-step validity (A6; not independently Pass-2-validated)"])
        self.assertTrue(man["primary_endpoint"].startswith("paired rescue difference V -> L_true vs V -> V"))

    def test_refuses_when_pass2_judgments_exist(self):
        from src import human_review as hr
        a5 = TestHumanReviewA5("test_end_to_end_two_passes")
        a5.setUpClass()
        w = a5.workdir("hr_a6b")
        P = hr.paths(w)
        self.assertEqual(self.run_script("scripts/18_export_human_review.py", "--work-dir", str(w)).returncode, 0)
        a5.fill(P, 1, lambda x: ("UNCLEAR", "LOW"))
        self.assertEqual(a5.freeze(w, 1).returncode, 0)
        a5.fill(P, 2, lambda x: ("CORRECT", "LOW"))
        r = self.run_script("scripts/21_freeze_pass1_only_review.py", "--reviewer", "Tester", "--work-dir", str(w))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Pass-2 judgments exist", r.stdout + r.stderr)


class TestStage2BundleV8(unittest.TestCase):
    """Bundle v8: frozen A6 Stage-2 inputs (61 items, 244 requests), smoke selection, notebook (no model)."""

    def test_frozen_stage2_inputs(self):
        P = s2r.paths(None)
        by_key, order, _, _ = s2r.frozen_inputs(P)                      # verifies locks + registered hashes
        man = C.read_json(C.STAGE2_MANIFEST)
        C.verify_frozen([C.rel(C.STAGE2_MANIFEST), "data/processed/annotation_review_decisions.json",
                         "data/manifests/ANNOTATION_REVIEW_FROZEN.json", "data/manifests/HUMAN_REVIEW_FROZEN.json",
                         C.rel(C.RULE_LOCK), "data/manifests/STAGE1_FINAL_ARCHIVE_PROVENANCE.json"])
        self.assertEqual((man["n_items"], man["n_requests"], len(by_key)), (61, 244, 244))
        self.assertEqual(man["requests_by_condition"], {c: 61 for c in ("L_false", "L_true", "N_true", "VV")})
        self.assertTrue(man["amendment_A6"]["pass2_waived"])
        dec = {d["item_id"]: d for d in s2r.verify_review_lock(P)[0]["decisions"]}
        self.assertEqual(sorted({r["item_id"] for r in by_key.values()}),
                         sorted(i for i, d in dec.items() if d["first_error_verdict"] == "CONFIRM"))
        self.assertTrue(s2r.rule_is_frozen(CFG))
        prov = C.read_json(C.DATA_MAN / "STAGE1_FINAL_ARCHIVE_PROVENANCE.json")
        self.assertEqual(prov["archive_sha256"], C.read_json(C.STAGE1_FAILURES_LOCK)["source_sha256"])

    def test_smoke_selection(self):
        by_key, order, _, _ = s2r.frozen_inputs(s2r.paths(None))
        base = s2r.smoke_keys(by_key, order, CFG)
        self.assertEqual(sorted({by_key[k]["item_id"] for k in base}), ["sb_gsm8k-107", "sb_math-275", "sb_math-361"])
        fails = C.read_json(C.STAGE1_FAILURES)["failures"]
        keys, info = s2r.smoke_keys_with_truncation(by_key, order, CFG, fails)
        self.assertEqual(len(keys), 12)
        self.assertEqual(sorted({by_key[k]["item_id"] for k in keys}), ["sb_gsm8k-107", "sb_math-275", "sb_math-355"])
        self.assertEqual((info["replaced"], info["replacement"]), ("sb_math-361", "sb_math-355"))
        trunc = {f["item_id"] for f in fails if f["stage1_truncated"]}
        self.assertEqual(sorted(trunc & {by_key[k]["item_id"] for k in keys}), ["sb_math-355"])
        self.assertEqual(sorted({by_key[k]["condition"] for k in keys}), ["L_false", "L_true", "N_true", "VV"])
        sel = C.read_json(s2r.SMOKE_SELECTION)                              # frozen selection = rule output
        C.verify_frozen([C.rel(s2r.SMOKE_SELECTION)])
        self.assertEqual(sel["request_keys"], keys)
        self.assertEqual(sel["requests_sha256"], C.sha256_file(C.STAGE2_REQUESTS))
        # rule is a no-op when a truncated item is already selected
        k2, i2 = s2r.smoke_keys_with_truncation(by_key, order, CFG,
                                                [dict(f, stage1_truncated=f["item_id"] == "sb_math-361") for f in fails])
        self.assertEqual((k2, i2["replaced"]), (base, None))

    def test_notebook_cells_compile(self):
        import ast
        import importlib.util
        if str(C.STAGE_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(C.STAGE_ROOT / "scripts"))
        spec = importlib.util.spec_from_file_location("b13", C.STAGE_ROOT / "scripts" / "13_build_kaggle_bundle.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(m.BUNDLE_VERSION, 13)
        code = [src for kind, src in m.NOTEBOOK_CELLS if kind == "code"]
        for src in code:
            ast.parse(src)
        allc = "\n".join(code)
        self.assertIn("'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7'", allc)
        self.assertIn("(61, 244)", allc)
        self.assertIn("stage2_smoke_selection.json", allc)
        self.assertIn("APPROVED_STAGE2_SMOKE_SHA256", allc)
        self.assertIn('MODE = "stage2"', allc)
        self.assertIn('APPROVED_STAGE2_SMOKE_SHA256 = "0a287c77a3c60baa25a18875c28b13b34a0cc07dda4e5cc8ddb18aa9cd0b2e42"', allc)
        self.assertIn("STAGE2_SESSION_LIMIT = 122", allc)
        self.assertIn("'--resume-from'", allc)
        self.assertIn("APPROVED_STAGE2_SMOKE_SHA256 != NOGO_SMOKE_SHA256", allc)
        self.assertNotIn("15_llm_review_smoke", allc)


class TestDiagHeadroomV10(unittest.TestCase):
    """v9 Stage-2 smoke NO-GO preserved; DIAGNOSTIC-ONLY headroom generation (mock here - TEST ONLY)."""
    SHA = "c5bd1fb6ad615618e968847fe695fbb44cae9fbe375620c56b9c226aaf900eca"

    def run_script(self, *args):
        import os
        return subprocess.run([sys.executable, *args], cwd=C.STAGE_ROOT, capture_output=True, text=True,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def test_nogo_record_preserved(self):
        rec = C.read_json(C.DATA_MAN / "STAGE2_SMOKE_V9_NOGO.json")
        C.verify_frozen(["data/manifests/STAGE2_SMOKE_V9_NOGO.json"])
        self.assertEqual((rec["archive_sha256"], rec["gate_decision"]), (self.SHA, "NO-GO"))
        reg = C.read_json(C.FROZEN_HASHES)["files"]
        self.assertEqual(reg["data/smoke_records/stage2_smoke_v9/stage_b_stage2_smoke_RESULTS.zip"], self.SHA)
        if not (C.STAGE_ROOT.parent / "manifest.json").exists():
            self.assertEqual(C.sha256_file(C.SMOKE_RECORDS / "stage2_smoke_v9" / "stage_b_stage2_smoke_RESULTS.zip"),
                             self.SHA)
        for k in ("VV", "N_true", "L_true", "L_false"):
            self.assertEqual(rec["per_request"][f"s2::sb_math-355::{k}"], {"new_tokens": 2048, "finish_reason": "length"})
        by_key, _, _, _ = s2r.frozen_inputs(s2r.paths(None))
        self.assertEqual(by_key["s2::sb_math-355::VV"]["prompt_sha256"], rec["sb_math_355_VV_2048"]["prompt_sha256"])

    def test_mock_diagnostic_isolated(self):
        out = TMP / "diag"
        shutil.rmtree(out, ignore_errors=True)
        before = {p: C.sha256_file(p) for p in (C.STAGE2_REQUESTS, C.STAGE2_ORDER, C.CONFIG_PATH)}
        r = self.run_script("scripts/22_diag_headroom.py", "--solver", "mock", "--output-root", str(out))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        d = out / "diag_headroom_sb_math-355_VV_4096"
        diag, raw = C.read_json(d / "diagnostic.json"), C.read_json(d / "raw_output.json")
        self.assertTrue(diag["TEST_ONLY"] and "DIAGNOSTIC ONLY" in diag["LABEL"])
        self.assertEqual(raw["solver"]["max_new_tokens_diagnostic"], 4096)
        self.assertEqual(raw["solver"]["max_new_tokens_frozen_stage2"], 2048)
        for k in ("finish_reason", "generated_tokens", "final_answer_label_present", "repetition_full",
                  "prefix_identical_to_2048_smoke_output"):
            self.assertIn(k, diag)
        self.assertEqual({p: C.sha256_file(p) for p in before}, before)       # nothing frozen touched
        self.assertNotEqual(self.run_script("scripts/22_diag_headroom.py", "--solver", "mock", "--output-root",
                                            str(out)).returncode, 0)          # never overwritten

    def _diag_main_in_process(self, out):
        """Run scripts/22 main() in-process with --solver qwen while the pilot backend loader is booby-trapped:
        the test fails if anything ever tries to build the real backend."""
        import importlib.util
        from unittest import mock
        from src import frozen_sources as fsrc
        if str(C.STAGE_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(C.STAGE_ROOT / "scripts"))
        spec = importlib.util.spec_from_file_location("diag22", C.STAGE_ROOT / "scripts" / "22_diag_headroom.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        def trap(*a, **k):
            raise AssertionError("the real pilot backend loader was reached inside a test")
        with mock.patch.object(fsrc, "pilot_module", side_effect=trap),                 mock.patch.object(sys, "argv", ["22", "--solver", "qwen", "--output-root", str(out)]):
            with self.assertRaises(SystemExit) as cm:
                m.main()
        self.assertFalse((out / "diag_headroom_sb_math-355_VV_4096").exists())
        return str(cm.exception)

    def test_real_diagnostic_refused_off_kaggle_simulated(self):
        """Environment-independent: no Kaggle marker, no override, suite flag unset -> refused before any load."""
        from unittest import mock
        with mock.patch.dict(os.environ), mock.patch("os.path.isdir", return_value=False):
            for k in ("KAGGLE_KERNEL_RUN_TYPE", "STAGE_B_ALLOW_MODEL_LOAD", "STAGE_B_TEST_SUITE"):
                os.environ.pop(k, None)
            msg = self._diag_main_in_process(TMP / "diag_off_kaggle")
        self.assertIn("only runs on the GPU machine", msg)

    def test_real_diagnostic_never_loaded_by_test_suite(self):
        """Even where model loading is permitted (Kaggle marker / override), the suite flag refuses before any load."""
        from unittest import mock
        with mock.patch.dict(os.environ, {"KAGGLE_KERNEL_RUN_TYPE": "Batch", "STAGE_B_ALLOW_MODEL_LOAD": "1",
                                          "STAGE_B_TEST_SUITE": "1"}):
            msg = self._diag_main_in_process(TMP / "diag_on_kaggle_sim")
        self.assertIn("never loaded from inside the test suite", msg)

    def test_suite_flag_set(self):
        self.assertTrue(C.env_flag("STAGE_B_TEST_SUITE"))


class TestSmokeGateA7(unittest.TestCase):
    """A7: Stage-2 smoke gate on pipeline/integrity criteria; cap hits are model behaviour; full Stage 2 locked."""
    DIAG = "72af3dd8f4032c839a5d1387df11cdb96437c4ee73f1ca6b8e93ed0906a254e8"
    NOGO = "c5bd1fb6ad615618e968847fe695fbb44cae9fbe375620c56b9c226aaf900eca"
    GO = "0a287c77a3c60baa25a18875c28b13b34a0cc07dda4e5cc8ddb18aa9cd0b2e42"
    T = {"all_completed": True, "no_persistent_technical_failures": True, "prompts_match_frozen_sha": True}
    PS = {"every_output_persisted_once": True, "finish_reason_recorded_stop_or_length": True}

    def d(self, n_stop, n_stop_parsed, tech=None, pers=None):
        return sg.decide_stage2_a7(tech or self.T, pers or self.PS, 12, n_stop, n_stop_parsed, 1, 2)

    def test_cap_hits_do_not_decide(self):
        g = self.d(8, 8)                      # v9 pattern: 4 cap hits (no answer) + 8 normal, all parsed -> GO
        self.assertEqual((g["decision"], g["n_finished_normally"], g["gate_version"]), ("GO", 8, sg.A7_GATE))
        self.assertEqual(self.d(8, 7)["decision"], "GO")
        self.assertEqual(self.d(8, 6)["decision"], "REVIEW")
        self.assertEqual(self.d(8, 5)["decision"], "NO-GO")
        self.assertEqual(self.d(0, 0)["decision"], "GO")

    def test_integrity_failures_are_no_go(self):
        self.assertEqual(self.d(12, 12, tech=dict(self.T, no_persistent_technical_failures=False))["decision"], "NO-GO")
        g = self.d(12, 12, pers=dict(self.PS, every_output_persisted_once=False))
        self.assertEqual(g["decision"], "NO-GO")
        self.assertIn("persistence", g["reason"])

    def test_stage1_gate_unchanged(self):
        self.assertEqual(sg.decide({"a": True}, {"no_truncation": False}, 20, 20, 1, 2)["decision"], "NO-GO")

    def test_config_and_frozen_inputs_unchanged(self):
        am = {a["id"]: a for a in CFG["protocol_amendments"]}["A7"]
        self.assertIn("not automatically a technical smoke NO-GO", am["wording"])
        self.assertIn("pipeline/integrity criteria", am["wording"])
        self.assertEqual((am["trigger_archive_sha256"], am["trigger_kind"]), (self.DIAG, "stage2_smoke_gate_clarification"))
        self.assertEqual(CFG["stage2"]["smoke"]["gate"], sg.A7_GATE)
        self.assertEqual(CFG["solver"]["stage_b_overrides"], {"max_new_tokens": 2048})
        self.assertEqual(CFG["stage2"]["conditions"], ["VV", "N_true", "L_true", "L_false"])
        self.assertEqual(CFG["analysis"]["primary_comparison"], ["L_true", "VV"])
        self.assertTrue(s2r.rule_is_frozen(CFG))
        by_key, _, _, _ = s2r.frozen_inputs(s2r.paths(None))
        self.assertEqual(len(by_key), 244)
        sel = C.read_json(s2r.SMOKE_SELECTION)
        C.verify_frozen([C.rel(s2r.SMOKE_SELECTION)])
        self.assertEqual(sorted(sel["items"]), ["sb_gsm8k-107", "sb_math-275", "sb_math-355"])
        self.assertIn("sb_math-355", sel["items"])                     # NOT removed from the smoke
        self.assertEqual(sel["n_requests"], 12)

    def test_registered_records(self):
        rec = {a["id"]: a for a in C.read_json(C.PROTOCOL_AMENDMENTS)["amendments"]}["A7"]
        reg = C.read_json(C.FROZEN_HASHES)["files"]
        self.assertEqual(rec["max_new_tokens"], 2048)
        self.assertEqual(rec["interpretation_rule_sha256"], C.read_json(C.RULE_LOCK)["rule_sha256"])
        C.verify_frozen(["data/manifests/DIAG_HEADROOM_V11_RECORD.json", "data/manifests/STAGE2_SMOKE_V9_NOGO.json",
                         C.rel(C.PROTOCOL_AMENDMENTS)] + list(rec["files_after_sha256"]))
        for r, h in rec["unchanged_sha256_equal_to_bundle_that_ran"].items():
            self.assertEqual(C.sha256_file(C.STAGE_ROOT / r), h, r)
        diag = C.read_json(C.DATA_MAN / "DIAG_HEADROOM_V11_RECORD.json")
        self.assertIn("DIAGNOSTIC ONLY", diag["LABEL"])
        self.assertEqual(diag["archive_sha256"], self.DIAG)
        q = diag["diagnostic_quoted"]
        self.assertEqual((q["finish_reason"], q["generated_tokens"], q["final_answer_label_present"],
                          q["numeric_parse_status"], q["prefix_identical_to_2048_smoke_output"]),
                         ("length", 4096, False, "missing", True))
        self.assertEqual(reg["data/smoke_records/diag_headroom_v11/stage_b_diag_headroom_RESULTS.zip"], self.DIAG)
        self.assertEqual(reg["data/smoke_records/stage2_smoke_v9/stage_b_stage2_smoke_RESULTS.zip"], self.NOGO)
        self.assertEqual(C.read_json(C.DATA_MAN / "STAGE2_SMOKE_V9_NOGO.json")["gate_decision"], "NO-GO")
        if not (C.STAGE_ROOT.parent / "manifest.json").exists():
            self.assertEqual(C.sha256_file(C.SMOKE_RECORDS / "diag_headroom_v11" / "stage_b_diag_headroom_RESULTS.zip"),
                             self.DIAG)

    def test_mock_stage2_smoke_end_to_end_a7(self):
        p = Pipeline.build()
        out = TMP / "smoke_s2_a7"
        shutil.rmtree(out, ignore_errors=True)
        r = subprocess.run([sys.executable, "scripts/09_stage2_smoke_test.py", "--solver", "mock", "--work-dir",
                            str(p.work), "--output-root", str(out)], cwd=C.STAGE_ROOT, capture_output=True, text=True,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        gate = C.read_json(out / "smoke_stage2_mock" / "smoke_gate.json")
        self.assertEqual(r.returncode, sg.EXIT_CODES[gate["decision"]], r.stdout[-2000:] + r.stderr[-2000:])
        self.assertTrue(gate["TEST_ONLY"])
        self.assertEqual((gate["gate_version"], gate["protocol_amendment"]), (sg.A7_GATE, "A7"))
        self.assertTrue(all(gate["technical_checks"].values()), gate["technical_checks"])
        self.assertTrue(all(gate["persistence_checks"].values()), gate["persistence_checks"])
        self.assertIn("DESCRIPTIVE ONLY", gate["truncation_descriptive"]["LABEL"])
        self.assertEqual(len(gate["per_request"]), gate["n_requests"])
        self.assertNotIn("truncation_checks", gate)
        self.assertFalse(any("repair" in k or "rescue" in k for k in gate))       # integrity only

    def test_full_stage2_locked_without_go_record(self):
        from unittest import mock
        with mock.patch.object(s2r, "SMOKE_GO", C.DATA_MAN / "NO_SUCH_STAGE2_SMOKE_GO.json"):
            with self.assertRaises(SystemExit) as cm:
                s2r.require_smoke_go()
        self.assertIn("locked", str(cm.exception))

    def test_full_stage2_unlocked_only_by_registered_v12_go(self):
        rec = s2r.require_smoke_go()
        self.assertEqual((rec["archive_sha256"], rec["gate_decision"], rec["gate_version"]), (self.GO, "GO", sg.A7_GATE))
        reg = C.read_json(C.FROZEN_HASHES)["files"]
        self.assertEqual(reg["data/smoke_records/stage2_smoke_v12_go/stage_b_stage2_smoke_RESULTS.zip"], self.GO)
        self.assertEqual((rec["bundle_version"], rec["bundle_code_fingerprint"]),
                         (12, "9a58eac6258a453d059180b9989ded0c9c3beae1ea14816a86583c83d913e5dc"))
        self.assertEqual((rec["requests_sha256"], rec["execution_order_sha256"]),
                         (C.sha256_file(C.STAGE2_REQUESTS), C.sha256_file(C.STAGE2_ORDER)))
        self.assertEqual(rec["max_new_tokens"], 2048)
        f = rec["integrity_findings"]
        self.assertEqual((f["completed"], f["planned"], f["transport_failure_records"], f["retries_used"],
                          f["n_finished_normally"], f["n_finished_normally_parsed"], f["audit_problems"]),
                         (12, 12, 0, 0, 8, 8, []))
        self.assertTrue(all(f["technical_checks"].values()) and all(f["persistence_checks"].values()))
        self.assertEqual(sorted(f["cap_hits_descriptive_only"]["keys"]),
                         sorted(f"s2::sb_math-355::{c}" for c in ("L_false", "L_true", "N_true", "VV")))
        self.assertFalse(any("repair" in k or "rescue" in k for k in f))             # integrity only
        for r, h in rec["preserved_records_unchanged"].items():
            self.assertEqual(reg[r], h, r)
        for r, h in rec["protocol_files_sha256_equal_to_bundle_that_ran"].items():
            self.assertEqual(C.sha256_file(C.STAGE_ROOT / r), h, r)
        if not (C.STAGE_ROOT.parent / "manifest.json").exists():
            self.assertEqual(C.sha256_file(C.SMOKE_RECORDS / "stage2_smoke_v12_go" / "stage_b_stage2_smoke_RESULTS.zip"),
                             self.GO)

    def test_mock_two_session_resume(self):
        import zipfile
        p = Pipeline.build()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        o1, o2 = TMP / "s2_session1", TMP / "s2_session2"
        for o in (o1, o2):
            shutil.rmtree(o, ignore_errors=True)
        base = [sys.executable, "scripts/10_run_stage2.py", "--solver", "mock", "--work-dir", str(p.work)]
        r1 = subprocess.run(base + ["--output-root", str(o1), "--limit", "5"], cwd=C.STAGE_ROOT,
                            capture_output=True, text=True, env=env)
        self.assertEqual(r1.returncode, 3, r1.stdout[-1500:] + r1.stderr[-1500:])           # partial
        rd1 = o1 / "test_stage2_mock"
        z = TMP / "stage_b_stage2_PARTIAL.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for f in ("raw_outputs.jsonl", "run_manifest.json", "state.json"):
                zf.write(rd1 / f, f"test_stage2_mock/{f}")
        r2 = subprocess.run(base + ["--output-root", str(o2), "--resume-from", str(z)], cwd=C.STAGE_ROOT,
                            capture_output=True, text=True, env=env)
        self.assertEqual(r2.returncode, 0, r2.stdout[-1500:] + r2.stderr[-1500:])
        st = C.read_json(o2 / "test_stage2_mock" / "state.json")
        self.assertEqual(st["completed"], st["planned"])
        rows = C.read_jsonl(o2 / "test_stage2_mock" / "raw_outputs.jsonl")
        self.assertEqual(len(rows), len({r["request_key"] for r in rows}))
        self.assertEqual(rows[:5], C.read_jsonl(rd1 / "raw_outputs.jsonl"))              # session-1 rows kept as-is

    def test_full_run_script_checks_lock_before_loading(self):
        src = (C.STAGE_ROOT / "scripts" / "10_run_stage2.py").read_text(encoding="utf-8")
        self.assertLess(src.index("s2r.require_smoke_go()"), src.index("s2r.run("))
        s9 = (C.STAGE_ROOT / "scripts" / "09_stage2_smoke_test.py").read_text(encoding="utf-8")
        self.assertIn("sg.decide_stage2_a7(", s9)
        self.assertNotIn("sg.decide(", s9)


if __name__ == "__main__":
    unittest.main()
