"""Run the Stage-B test suite; on success record tests/LAST_PASS.json (code fingerprint) - the Kaggle bundle builder
refuses to build from code that has not passed.   python tests/run_all.py
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import common as C  # noqa: E402

os.environ["STAGE_B_TEST_SUITE"] = "1"     # H2: no real model load from any test, on any host


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    shutil.rmtree(C.TEST_RUNS_ROOT / "_unittest", ignore_errors=True)
    ok = res.wasSuccessful()
    if ok:
        C.write_json(ROOT / "tests" / "LAST_PASS.json", {"passed": C.now(), "tests_run": res.testsRun,
                                                         "code_fingerprint": C.code_fingerprint()})
    print(json.dumps({"tests_run": res.testsRun, "failures": len(res.failures), "errors": len(res.errors),
                      "skipped": len(res.skipped), "passed": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
