"""Shared Stage-B helpers: paths, config, SHA-256, deterministic JSON. Standard library only.
(Same conventions as experiments/stage_a_reliability_gating/src/common.py; Stage A itself is never imported.)"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STAGE_ROOT = Path(__file__).resolve().parent.parent          # experiments/stage_b_sequential_escalation
REPO_ROOT = STAGE_ROOT.parent.parent                          # ConsiousMind
CONFIG_PATH = STAGE_ROOT / "configs" / "stage_b.json"

DATA_RAW = STAGE_ROOT / "data" / "raw"
DATA_PROC = STAGE_ROOT / "data" / "processed"
DATA_MAN = STAGE_ROOT / "data" / "manifests"
OUTPUTS = STAGE_ROOT / "outputs"
RUNS_ROOT = OUTPUTS / "runs"             # real model runs only
TEST_RUNS_ROOT = OUTPUTS / "test_runs"   # mock runs only (TEST ONLY)

# frozen Stage-B artifacts (written once, then only read)
INSPECTION_FILE = DATA_MAN / "processbench_inspection.json"
CANDIDATES_FILE = DATA_PROC / "candidates_all.jsonl"          # every source item + eligibility verdict
ITEMS_FILE = DATA_PROC / "stage_b_items.json"                 # the frozen Stage-B pool (+ ordered reserve)
DATASET_MANIFEST = DATA_MAN / "dataset_manifest.json"
STAGE1_REQUESTS = DATA_PROC / "stage1_requests.jsonl"
STAGE1_ORDER = DATA_PROC / "stage1_execution_order.json"
STAGE1_MANIFEST = DATA_MAN / "stage1_request_manifest.json"
STAGE1_FAILURES = DATA_PROC / "stage1_failures.json"          # written only from a verified real Stage-1 archive
STAGE1_FAILURES_LOCK = DATA_MAN / "STAGE1_FAILURES_FROZEN.json"
STAGE2_REQUESTS = DATA_PROC / "stage2_requests.jsonl"
STAGE2_ORDER = DATA_PROC / "stage2_execution_order.json"
STAGE2_MANIFEST = DATA_MAN / "stage2_request_manifest.json"
RULE_LOCK = DATA_MAN / "INTERPRETATION_RULE_FROZEN.json"
FROZEN_HASHES = DATA_MAN / "FROZEN_HASHES.json"
PROTOCOL_AMENDMENTS = DATA_MAN / "PROTOCOL_AMENDMENTS.json"
SMOKE_RECORDS = STAGE_ROOT / "data" / "smoke_records"           # real smoke archives, preserved unaltered
# protocol files frozen by amendments A1 (max_new_tokens 1536; Stage-B parser) and A2 (max_new_tokens 2048); runners
# verify them
AMENDED_PROTOCOL_FILES = ["configs/stage_b.json", "src/parser.py"]

TEST_ONLY_BANNER = "NO REAL MODEL RESULTS YET – TEST ONLY"
ORACLE_STATEMENT = ("ORACLE STATUS: Stage B uses two oracle components - (1) the gold answer decides whether Stage 1 "
                    "failed and therefore whether escalation is triggered; (2) L_true/N_true use the expert-annotated "
                    "first-error step. This is NOT a deployable system: it tests whether diagnostic escalation has "
                    "potential value IF failure detection and localisation were available.")

LIMITATIONS = ("LIMITATION (possible training contamination): items come from the GSM8K-test and MATH-test source "
               "problems, which Qwen3-4B may have seen in training. Absolute accuracy/repair rates are NOT "
               "uncontaminated capability estimates; the quantities of interest are paired within-item differences "
               "between feedback conditions.")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path=CONFIG_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dumps(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(obj), encoding="utf-8", newline="\n")
    return sha256_file(path)


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return sha256_file(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def write_frozen(path, writer, obj, rebuild=False):
    """Write a frozen artifact once. If it already exists the rebuilt content must be byte-identical
    (determinism check); a different result stops instead of silently overwriting."""
    path = Path(path)
    if path.exists():
        old = path.read_bytes()
        tmp = path.with_name(path.name + ".rebuild_check")
        writer(tmp, obj)
        new = tmp.read_bytes()
        tmp.unlink()
        if old != new:
            if not rebuild:
                raise SystemExit(f"STOP: {path.name} already exists and the rebuild differs. Frozen artifacts are "
                                 "never overwritten silently. Investigate, or pass --rebuild deliberately.")
            writer(path, obj)
            return sha256_file(path), "REBUILT (content changed)"
        return sha256_file(path), "unchanged (rebuild is byte-identical)"
    writer(path, obj)
    return sha256_file(path), "written"


def rel(path):
    try:
        return Path(path).resolve().relative_to(STAGE_ROOT).as_posix()
    except ValueError:
        return str(path)


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def code_fingerprint():
    """SHA-256 over every Stage-B code/config/test file (ties a passing test run to the exact code bundled)."""
    files = sorted(p for part in ("src", "scripts", "configs", "tests") for p in (STAGE_ROOT / part).rglob("*")
                   if p.is_file() and p.suffix in (".py", ".json") and p.name != "LAST_PASS.json"
                   and "__pycache__" not in p.parts)
    return sha256_text("\n".join(f"{p.relative_to(STAGE_ROOT).as_posix()}:{sha256_file(p)}" for p in files))


def real_runs_exist():
    return RUNS_ROOT.exists() and any(p.is_dir() for p in RUNS_ROOT.iterdir())


def update_frozen_hashes(entries, change_reason=None, previous=None):
    """Merge {relpath: sha256} into data/manifests/FROZEN_HASHES.json. An existing entry may change ONLY with an
    explicit change_reason (a researcher-approved protocol change) and only while no real model run exists; every
    such change is appended to the file's change_log (old -> new sha). `previous` = {relpath: sha256} for a file that
    was in use but not yet registered (e.g. the config / parser that ran smoke1): its first registration is then
    logged as old -> new too."""
    doc = read_json(FROZEN_HASHES) if FROZEN_HASHES.exists() else {"files": {}, "change_log": []}
    cur, log = doc["files"], doc.get("change_log", [])
    for k, v in entries.items():
        if k in cur and cur[k] != v:
            if not change_reason:
                raise SystemExit(f"STOP: frozen artifact {k} changed ({cur[k][:12]} -> {v[:12]})")
            if real_runs_exist():
                raise SystemExit(f"STOP: {k} cannot change: real model outputs already exist in {RUNS_ROOT}")
            log.append({"time": now(), "file": k, "old_sha256": cur[k], "new_sha256": v, "reason": change_reason})
        elif k not in cur and (previous or {}).get(k) not in (None, v):
            if not change_reason:
                raise SystemExit(f"STOP: {k} differs from the version previously in use; a change_reason is required")
            if real_runs_exist():
                raise SystemExit(f"STOP: {k} cannot change: real model outputs already exist in {RUNS_ROOT}")
            log.append({"time": now(), "file": k, "old_sha256": previous[k], "new_sha256": v,
                        "reason": change_reason, "note": "first registration; old = version in use before"})
        cur[k] = v
    write_json(FROZEN_HASHES, {"note": "SHA-256 of every frozen Stage-B artifact; runners verify these before use.",
                               "updated": now(), "files": dict(sorted(cur.items())), "change_log": log})


def verify_frozen(relpaths):
    cur = read_json(FROZEN_HASHES)["files"]
    for r in relpaths:
        if r not in cur:
            raise SystemExit(f"STOP: {r} is not registered in FROZEN_HASHES.json")
        got = sha256_file(STAGE_ROOT / r)
        if got != cur[r]:
            raise SystemExit(f"STOP: frozen file {r} changed (sha256 {got} != {cur[r]})")
    return {r: cur[r] for r in relpaths}
