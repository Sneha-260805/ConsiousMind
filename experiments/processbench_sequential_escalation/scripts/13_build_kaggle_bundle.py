"""Build the Kaggle upload bundle + notebook (nothing is run, nothing is uploaded).

    python scripts/13_build_kaggle_bundle.py
Writes kaggle/stage_b_bundle.zip (upload as a Kaggle Dataset) and kaggle/stage_b_run.ipynb. The bundle holds this
Stage-B folder (code, configs, tests, frozen data, the raw sources the tests re-derive from; no outputs, no parquet),
byte-identical frozen pilot files under pilot_frozen/ and the two read-only Stage-A files under stage_a_frozen/.
manifest.json records every file's SHA-256; the notebook verifies all of them. Refuses to build unless the local test
suite passed on the current code (tests/LAST_PASS.json).

Notebook MODE (set in Cell 1, then Save Version -> Save & Run All) - bundle v13 = FULL Stage 2 under A5/A6/A7,
unlocked ONLY by the registered v12 smoke GO (data/manifests/STAGE2_SMOKE_GO.json, archive 0a287c77...):
  "stage2"  all 244 frozen requests (61 items x 4) in TWO Kaggle sessions (STAGE2_SESSION_LIMIT new generations per
            session; the run plan / identity is unchanged). Session 1 -> stage_b_stage2_PARTIAL.zip; session 2 resumes
            from it (RESUME_FROM_SHA256) -> stage_b_stage2_qwen3_4b_FINAL.zip. Smoke / diagnostic modes are closed.
  (Stage 1 and the annotation review are complete; the LLM reviewer is stopped (A5); Pass 2 is waived (A6).)
"""
import json
import sys
import zipfile

import _bootstrap  # noqa: F401
from src import common as C
from src import frozen_sources as fs

BUNDLE_ID = "stage-b-sequential-escalation-bundle"
BUNDLE_VERSION = 13
KAGGLE_DIR = C.STAGE_ROOT / "kaggle"
STAGE_PARTS = ["src", "scripts", "configs", "tests", "data/processed", "data/manifests"]
RAW_FILES = ["data/raw/processbench_3bdcd537/README.md", "data/raw/processbench_3bdcd537/gsm8k.json",
             "data/raw/processbench_3bdcd537/math.json", "data/raw/processbench_3bdcd537/olympiadbench.json",
             "data/raw/processbench_3bdcd537/omnimath.json", "data/raw/math500_6e4ed1a2/test.jsonl",
             "data/raw/hendrycks_math_21a56338/test_rows_api.jsonl"]
PILOT_FILES = ["scripts/parse_answers.py", "scripts/backends.py", "scripts/kaggle_runner.py",
               "configs/qwen3_4b_kaggle.json", "data/raw/test.jsonl", "data/raw/train.jsonl"]
STAGE_A_FILES = ["configs/stage_a.json", "data/manifests/FROZEN_HASHES.json"]


def cell(src, kind="code"):
    c = {"cell_type": kind, "metadata": {}, "source": src.strip("\n").splitlines(keepends=True)}
    if kind == "code":
        c.update(execution_count=None, outputs=[])
    return c


NOTEBOOK_CELLS = [
    ("markdown", f"""
# Stage B: Stage 2 (V -> X repair) on the 61 review-eligible Stage-1 failures - Qwen3-4B (frozen pilot protocol)

**Run with: File -> Save Version -> Save & Run All (Commit).** Settings: Accelerator **GPU T4 x2** · Internet **On** ·
Add Input -> Dataset **{BUNDLE_ID}** (v{BUNDLE_VERSION}).

Frozen state carried by this bundle: Stage 1 complete (265/265; 76 failures; FINAL archive sha `2042a50f...`,
provenance in `data/manifests/STAGE1_FINAL_ARCHIVE_PROVENANCE.json`). Annotation-fidelity review = blinded **human**
Pass 1 (amendment A5; 61 CONFIRM / 15 DISAGREE). Amendment **A6**: a separate Pass-2 human recheck was waived for
feasibility because it would duplicate the same reviewer's earlier judgment; L_false validity on the 61 items is
**"Pass-1-implied false-step validity"** (not independently Pass-2-validated). The 15 DISAGREE items get no Stage-2
request. Primary endpoint (unchanged, rule frozen): paired rescue difference **V -> L_true vs V -> V**.

Solver (frozen pilot protocol): Qwen/Qwen3-4B @ 3101254b · FP16 · cuda:0 · greedy · enable_thinking=False · batch 1 ·
one user message · max_new_tokens 2048 (A2). 244 frozen requests = 61 items x {{VV, N_true, L_true, L_false}}.

Amendment **A7** (smoke-gate clarification): the v11 DIAGNOSTIC-ONLY run showed sb_math-355 VV still looping at
4096 tokens, so the cap stays 2048. A Stage-2 response reaching the 2048-token cap is genuine model behaviour (a failed
repair if no valid final answer parses), not a technical NO-GO, if it completed normally and was persisted correctly;
the smoke gate decides on pipeline / integrity criteria. The v9 NO-GO record is preserved and never rescored.

The v12 Stage-2 smoke (archive `0a287c77...`) received **GO under A7** and is registered in
`data/manifests/STAGE2_SMOKE_GO.json`; the full run is unlocked only against that record.

MODE `"stage2"`: all **244** frozen requests in two sessions of at most 122 new generations each (cap hits take ~5.4
min each on a T4, so one session could exceed Kaggle's 12-hour limit). Session 1 ends with `stage_b_stage2_PARTIAL.zip`;
attach it to session 2 and paste its SHA-256 into `RESUME_FROM_SHA256`. Session 2 ends with the FINAL archive.
"""),
    ("code", """
# CELL 1 - parameters (edit before Save Version)
MODE = "stage2"                       # v13: FULL Stage 2 (244 requests), unlocked by the registered v12 smoke GO
APPROVED_STAGE2_SMOKE_SHA256 = "0a287c77a3c60baa25a18875c28b13b34a0cc07dda4e5cc8ddb18aa9cd0b2e42"   # v12 GO smoke (A7)
STAGE2_SESSION_LIMIT = 122            # max NEW generations this session (122 + 122 = 244); None = no limit
RESUME_FROM_SHA256 = ""               # session 1: "" | session 2: SHA-256 of the attached stage_b_stage2_PARTIAL.zip
"""),
    ("code", f"""
# CELL 2 - locate + verify the bundle, stage it in /kaggle/working (every file hash checked)
import hashlib, json, os, shutil, subprocess, sys, zipfile
from pathlib import Path
hits = [m.parent for m in Path('/kaggle/input').rglob('manifest.json')
        if json.loads(m.read_text()).get('bundle_id') == '{BUNDLE_ID}']
assert len(hits) == 1, f'STOP: attach exactly one {BUNDLE_ID} dataset (found {{hits}})'
SRC = hits[0]
MAN = json.loads((SRC / 'manifest.json').read_text())
assert MAN['bundle_version'] == {BUNDLE_VERSION}, f"STOP: bundle v{{MAN['bundle_version']}} attached, notebook is for v{BUNDLE_VERSION}"
WORK = Path('/kaggle/working/stage_b_bundle')
if WORK.exists():
    shutil.rmtree(WORK)
shutil.copytree(SRC, WORK)
bad = [r for r, h in MAN['files'].items() if hashlib.sha256((WORK / r).read_bytes()).hexdigest() != h]
assert not bad, f'STOP: hash mismatch {{bad}}'
STAGE = WORK / 'stage_b'
os.environ['STAGE_B_PILOT_ROOT'] = str(WORK / 'pilot_frozen')
os.environ['STAGE_B_STAGE_A_ROOT'] = str(WORK / 'stage_a_frozen')
OUT = Path('/kaggle/working/stage_b_runs'); OUT.mkdir(parents=True, exist_ok=True)
print('bundle v', MAN['bundle_version'], 'verified:', len(MAN['files']), 'files | code fingerprint', MAN['code_fingerprint'][:16])
def run(*args, ok=(0,)):
    print('$ python', *args, flush=True)
    r = subprocess.run([sys.executable, *args], cwd=STAGE, env={{**os.environ, 'PYTHONIOENCODING': 'utf-8'}})
    if r.returncode not in ok:
        raise RuntimeError(f'STOP: {{args[0]}} exited with {{r.returncode}} (all files written so far are kept)')
    return r.returncode
def sha(f):
    return hashlib.sha256(Path(f).read_bytes()).hexdigest()
"""),
    ("code", """
# CELL 3 - GPU check (Tesla T4) + pinned transformers/accelerate (frozen pilot helpers)
sys.path.insert(0, str(WORK / 'pilot_frozen' / 'scripts'))
import kaggle_runner as kr
GPU = kr.gpu_check()
assert 'T4' in GPU['device_name'], 'STOP: the frozen protocol ran on a Kaggle Tesla T4'
VERSIONS = kr.ensure_packages()
print(VERSIONS)
"""),
    ("code", """
# CELL 4 - local test suite on the GPU machine (mock only, no model); must pass before any request is sent
run('tests/run_all.py')
"""),
    ("code", """
# CELL 5 - frozen A6/A7 state + registered smoke-GO checks, then the FULL STAGE 2
import platform, torch
CFG = json.loads((STAGE / 'configs' / 'stage_b.json').read_text(encoding='utf-8'))
assert [a['id'] for a in CFG['protocol_amendments']] == ['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7'], 'STOP: not the A7 protocol'
assert CFG['stage2']['smoke']['gate'] == 'A7_stage2_pipeline_integrity_gate', 'STOP: not the A7 smoke gate'
assert CFG['solver']['stage_b_overrides']['max_new_tokens'] == 2048, 'STOP: the frozen cap is 2048'
assert CFG['annotation_review']['pass2_waiver_A6']['pass2_waived'] is True
assert CFG['analysis']['primary_comparison'] == ['L_true', 'VV'], 'STOP: primary endpoint changed'
FH = json.loads((STAGE / 'data' / 'manifests' / 'FROZEN_HASHES.json').read_text())['files']
for r in ('configs/stage_b.json', 'src/parser.py', 'data/processed/stage_b_items.json',
          'data/processed/stage2_requests.jsonl', 'data/processed/stage2_execution_order.json',
          'data/manifests/stage2_request_manifest.json', 'data/processed/annotation_review_decisions.json',
          'data/manifests/ANNOTATION_REVIEW_FROZEN.json', 'data/manifests/INTERPRETATION_RULE_FROZEN.json',
          'data/manifests/HUMAN_REVIEW_FROZEN.json', 'data/manifests/STAGE1_FAILURES_FROZEN.json',
          'data/manifests/stage2_smoke_selection.json', 'data/manifests/STAGE2_SMOKE_V9_NOGO.json',
          'data/manifests/DIAG_HEADROOM_V11_RECORD.json', 'src/smoke_gate.py', 'scripts/09_stage2_smoke_test.py',
          'data/manifests/STAGE2_SMOKE_GO.json', 'src/stage2_runner.py', 'scripts/10_run_stage2.py'):
    assert sha(STAGE / r) == FH[r], f'STOP: {r} differs from FROZEN_HASHES'
S2M = json.loads((STAGE / 'data' / 'manifests' / 'stage2_request_manifest.json').read_text())
assert (S2M['n_items'], S2M['n_requests']) == (61, 244), 'STOP: not the frozen 61-item / 244-request set'
assert S2M['requests_by_condition'] == {'L_false': 61, 'L_true': 61, 'N_true': 61, 'VV': 61}
assert S2M['amendment_A6']['pass2_waived'] is True
SEL = json.loads((STAGE / 'data' / 'manifests' / 'stage2_smoke_selection.json').read_text())
assert SEL['n_requests'] == 12 and SEL['stage1_truncated_items'], 'STOP: smoke selection must include a Stage-1-truncated item'
assert 'sb_math-355' in SEL['items'], 'STOP: sb_math-355 must stay in the smoke'
print('smoke items:', SEL['items'], '| Stage-1-truncated:', SEL['stage1_truncated_items'])
ENV = {'gpu': GPU, 'package_versions': VERSIONS, 'python': sys.version, 'platform': platform.platform(),
       'torch_cuda': torch.version.cuda, 'kaggle_docker_image': os.environ.get('KAGGLE_DOCKER_IMAGE'),
       'bundle_version': MAN['bundle_version'], 'bundle_code_fingerprint': MAN['code_fingerprint']}
NOGO_SMOKE_SHA256 = json.loads((STAGE / 'data' / 'manifests' / 'STAGE2_SMOKE_V9_NOGO.json').read_text())['archive_sha256']
if MODE in ('diag_headroom', 'stage2_smoke'):
    raise SystemExit(f'STOP: MODE {MODE} is closed (v11 diagnostic + v12 GO smoke are preserved); bundle v13 runs MODE stage2')
elif MODE == 'stage2':
    GO = STAGE / 'data' / 'manifests' / 'STAGE2_SMOKE_GO.json'
    assert GO.exists() and sha(GO) == FH['data/manifests/STAGE2_SMOKE_GO.json'], 'STOP: no registered Stage-2 smoke GO record'
    GOREC = json.loads(GO.read_text())
    assert (GOREC['gate_decision'], GOREC['gate_version']) == ('GO', 'A7_stage2_pipeline_integrity_gate'), 'STOP: not an A7 GO'
    assert len(APPROVED_STAGE2_SMOKE_SHA256) == 64, 'STOP: the full Stage-2 run needs the APPROVED smoke archive SHA-256 in Cell 1'
    assert APPROVED_STAGE2_SMOKE_SHA256 == GOREC['archive_sha256'], 'STOP: not the registered GO smoke archive'
    assert APPROVED_STAGE2_SMOKE_SHA256 != NOGO_SMOKE_SHA256, 'STOP: that is the v9 NO-GO smoke archive'
    assert (GOREC['requests_sha256'], GOREC['execution_order_sha256']) == (FH['data/processed/stage2_requests.jsonl'],
           FH['data/processed/stage2_execution_order.json']), 'STOP: GO smoke ran different requests / order'
    rd = OUT / 'stage_b_stage2_qwen3_4b'
    args = ['scripts/10_run_stage2.py', '--solver', 'qwen', '--output-root', str(OUT)]
    session = 1
    if RESUME_FROM_SHA256:
        # the PARTIAL zip (e.g. attached as this notebook's previous output) OR its extracted run folder (Kaggle datasets
        # unzip uploads): matched by the zip SHA-256 or by the raw_outputs.jsonl SHA-256 printed at the end of session 1
        hits = [p for p in Path('/kaggle/input').rglob('stage_b_stage2_PARTIAL*.zip') if sha(p) == RESUME_FROM_SHA256]
        hits += [p.parent for p in Path('/kaggle/input').rglob('raw_outputs.jsonl')
                 if sha(p) == RESUME_FROM_SHA256 and (p.parent / 'run_manifest.json').exists()]
        assert len(hits) == 1, f'STOP: attach exactly one stage_b_stage2_PARTIAL.zip (or its extracted folder) matching {RESUME_FROM_SHA256}: {hits}'
        assert not (rd / 'raw_outputs.jsonl').exists(), 'STOP: resume needs a fresh session (empty run directory)'
        args += ['--resume-from', str(hits[0])]
        session = 2
    if STAGE2_SESSION_LIMIT:
        args += ['--limit', str(int(STAGE2_SESSION_LIMIT))]
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f'environment_session{session}.json').write_text(json.dumps({**ENV, 'approved_stage2_smoke_sha256': APPROVED_STAGE2_SMOKE_SHA256,
        'session': session, 'session_limit': STAGE2_SESSION_LIMIT, 'resume_from_sha256': RESUME_FROM_SHA256 or None},
        indent=2, default=str))
    rc = run(*args, ok=(0, 3))
    st = json.loads((rd / 'state.json').read_text())
    print('completed', st['completed'], '/', st['planned'])
    if rc == 0 and st['completed'] == st['planned']:
        run('scripts/12_package_results.py', '--run-dir', str(rd))
        z = OUT / 'stage_b_stage2_qwen3_4b_FINAL.zip'
        print('STAGE 2 COMPLETE. ZIP:', z, '| SHA-256:', sha(z), '-> download it (+ .sha256.txt) and give it to Claude. STOP.')
    else:
        z = OUT / 'stage_b_stage2_PARTIAL.zip'
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(rd.rglob('*')):
                if f.is_file() and f.name != 'run.lock' and 'checkpoints' not in f.parts:
                    zf.write(f, f'{rd.name}/{f.relative_to(rd).as_posix()}')
            zf.writestr('bundle_manifest.json', json.dumps(MAN, indent=2))
        Path(str(z) + '.sha256.txt').write_text(f'{sha(z)}  {z.name}\\n')
        print(f'SESSION {session} DONE - PARTIAL ({st["completed"]}/{st["planned"]}). ZIP:', z, '| ZIP SHA-256:', sha(z),
              '| raw_outputs.jsonl SHA-256:', sha(rd / 'raw_outputs.jsonl'),
              '-> download it (+ .sha256.txt); attach it to the next session and set RESUME_FROM_SHA256. No analysis. STOP.')
else:
    raise SystemExit(f'STOP: MODE {MODE} is not part of this bundle (stage2)')
"""),
]


def build():
    lp = C.STAGE_ROOT / "tests" / "LAST_PASS.json"
    fpnt = C.code_fingerprint()
    if not lp.exists() or C.read_json(lp).get("code_fingerprint") != fpnt:
        raise SystemExit("STOP: run python tests/run_all.py first; the bundle is only built from code that passed")
    cfg = C.load_config()
    files = {}
    for part in STAGE_PARTS:
        for p in sorted((C.STAGE_ROOT / part).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and ".rebuild_check" not in p.name:
                files[f"stage_b/{p.relative_to(C.STAGE_ROOT).as_posix()}"] = p.read_bytes()
    for r in RAW_FILES:
        files[f"stage_b/{r}"] = (C.STAGE_ROOT / r).read_bytes()
    for doc in ("README.md", "STAGE_B_PLAN.md", "RUNBOOK.md"):
        if (C.STAGE_ROOT / doc).exists():
            files[f"stage_b/{doc}"] = (C.STAGE_ROOT / doc).read_bytes()
    for r in PILOT_FILES:
        files[f"pilot_frozen/{r}"] = fs.pilot_path(r, cfg).read_bytes()
    for r in STAGE_A_FILES:
        files[f"stage_a_frozen/{r}"] = fs.stage_a_path(r, cfg).read_bytes()
    manifest = {"bundle_id": BUNDLE_ID, "bundle_version": BUNDLE_VERSION, "phase": "stage2_full_A7", "created": C.now(),
                "code_fingerprint": fpnt, "files": {k: C.sha256_bytes(v) for k, v in sorted(files.items())}}
    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
    zpath = KAGGLE_DIR / "stage_b_bundle.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in sorted(files.items()):
            z.writestr(k, v)
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    nb = {"cells": [cell(src, kind) for kind, src in NOTEBOOK_CELLS],
          "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                       "language_info": {"name": "python"}}, "nbformat": 4, "nbformat_minor": 5}
    npath = KAGGLE_DIR / "stage_b_run.ipynb"
    npath.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    info = {"bundle": C.rel(zpath), "bundle_sha256": C.sha256_file(zpath), "files": len(files),
            "notebook": C.rel(npath), "notebook_sha256": C.sha256_file(npath), "code_fingerprint": fpnt}
    C.write_json(KAGGLE_DIR / "BUNDLE_INFO.json", info)
    return info


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
    sys.exit(0)
