"""Shared helpers for the pilot run harness: frozen inputs, provenance hashes, the execution order,
and crash-safe JSONL persistence. Standard library only."""
import hashlib
import json
import os
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Frozen inputs (read-only). Keys are the short names used in configs and logs.
INPUT_FILES = {
    "m3_pilot_40.json": ROOT / "data" / "pilot" / "m3_pilot_40.json",
    "m3_pilot_40_conditions.jsonl": ROOT / "data" / "pilot" / "m3_pilot_40_conditions.jsonl",
    "m3_pilot_40_far_false_sensitivity.jsonl": ROOT / "data" / "pilot" / "m3_pilot_40_far_false_sensitivity.jsonl",
}
PRIMARY = "m3_pilot_40_conditions.jsonl"
SENSITIVITY = "m3_pilot_40_far_false_sensitivity.jsonl"
SOURCE_TAG = {PRIMARY: "primary", SENSITIVITY: "sensitivity"}

ORDER_FILE = ROOT / "outputs" / "pilot_execution_order.json"
ORDER_SEED = 2026
RUNS_ROOT = ROOT / "outputs" / "runs"            # real experiment runs only
TEST_RUNS_ROOT = ROOT / "outputs" / "test_runs"  # mock backend and dry runs only (TEST ONLY)

# The frozen primary file says "L_false"; analysis calls it L_false_near. The file is never rewritten.
ANALYSIS_ALIAS = {"L_false": "L_false_near"}
PRIMARY_CONDITIONS = ["L0", "V", "L_false_near", "L", "A"]
SENSITIVITY_CONDITIONS = ["L_false_far"]
EXPECTED_ROWS = {PRIMARY: 200, SENSITIVITY: 25}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_hashes(files=None):
    files = files or INPUT_FILES
    return {name: sha256_file(path) for name, path in files.items()}


def analysis_condition(condition_source):
    return ANALYSIS_ALIAS.get(condition_source, condition_source)


def request_key(source_file, row_index):
    return f"{SOURCE_TAG[source_file]}:{row_index:03d}"


def load_requests(files=None):
    """All 225 requests in source-file order, with the exact prompt text from the frozen files."""
    files = files or INPUT_FILES
    out = []
    for source in (PRIMARY, SENSITIVITY):
        lines = Path(files[source]).read_text(encoding="utf-8").splitlines()
        if len(lines) != EXPECTED_ROWS[source]:
            raise SystemExit(f"{source}: expected {EXPECTED_ROWS[source]} rows, found {len(lines)}")
        for i, line in enumerate(lines):
            r = json.loads(line)
            pointed = r.get("pointed_step") if source == PRIMARY else r["false_step_far"]
            out.append({
                "request_key": request_key(source, i),
                "source_file": source,
                "row_index": i,
                "item_id": r["item_id"],
                "condition_source": r["condition"],
                "analysis_condition": analysis_condition(r["condition"]),
                "prompt": r["prompt"],
                "prompt_sha256": sha256_text(r["prompt"]),
                "gold_answer": r["gold_answer"],
                "corrupted_answer": r["corrupted_answer"],
                "true_error_step": r["true_error_step"],
                "pointed_step": pointed,
                "trace_length": r["trace_length"],
            })
    return out


def build_execution_order(requests, seed=ORDER_SEED, shuffle=True):
    keyed = sorted(requests, key=lambda r: r["request_key"])
    if shuffle:
        random.Random(seed).shuffle(keyed)
    return [{"run_index": i, "request_key": r["request_key"], "item_id": r["item_id"],
             "condition_source": r["condition_source"], "analysis_condition": r["analysis_condition"],
             "source_file": r["source_file"], "row_index": r["row_index"]}
            for i, r in enumerate(keyed)]


def order_document(requests, hashes, seed=ORDER_SEED, shuffle=True):
    order = build_execution_order(requests, seed, shuffle)
    body = json.dumps(order, sort_keys=True)
    return {"seed": seed, "shuffled": shuffle, "n_requests": len(order),
            "input_sha256": hashes, "order_sha256": sha256_text(body),
            "condition_alias": ANALYSIS_ALIAS, "order": order}


def write_or_verify_order(doc, path=ORDER_FILE):
    """The order file is written once; later calls must reproduce it exactly or stop."""
    path = Path(path)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("order_sha256") != doc["order_sha256"] or existing.get("input_sha256") != doc["input_sha256"]:
            raise SystemExit(f"{path} exists but differs from the order rebuilt from the current inputs; stopping")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


# ------------------------------------------------------------------ crash-safe JSONL
def append_jsonl(path, row):
    """Append one row and force it to disk before returning, so a crash loses at most the row in flight."""
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl_tolerant(path):
    """Read a JSONL file written by append_jsonl. A torn final line (crash mid-write) is cut off and
    reported; a malformed line anywhere else is an error."""
    path = Path(path)
    if not path.exists():
        return [], None
    data = path.read_bytes()
    lines = data.split(b"\n")
    rows, good_bytes = [], 0
    for i, line in enumerate(lines):
        if not line:
            good_bytes += 1 if i < len(lines) - 1 else 0
            continue
        try:
            rows.append(json.loads(line.decode("utf-8")))
            good_bytes += len(line) + 1
        except (json.JSONDecodeError, UnicodeDecodeError):
            if i == len(lines) - 1 or all(not l for l in lines[i + 1:]):
                with open(path, "r+b") as fh:
                    fh.truncate(good_bytes)
                return rows, f"truncated a torn final line ({len(line)} bytes) from {path.name}"
            raise SystemExit(f"{path}: malformed line {i + 1} that is not the final line; refusing to continue")
    return rows, None


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
