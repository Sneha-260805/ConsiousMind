"""Run the Information-or-Placebo pilot prompts through ONE model configuration.

  python scripts/run_pilot.py --config configs/<model>.json --run-id <id> [--mode full|primary|smoke]
                              [--dry-run] [--sequential] [--max-requests N]

Guarantees
  * one fresh, single-message request per prompt; the backend receives only the prompt string
  * the exact frozen prompt text is sent and stored (its SHA-256 is checked before every call)
  * execution order: the 225 requests shuffled once with seed 2026 (outputs/pilot_execution_order.json);
    --sequential uses source-file order instead (recorded in config)
  * every completed response is appended and fsync'ed to raw_outputs.jsonl immediately
  * resume: rerunning the same command skips completed requests, never duplicates one, and stops if
    the input hashes, execution order, model config or request selection changed
  * transport failures (timeout, connection, 429, 5xx) are retried with the SAME prompt and settings
    up to max_attempts; wrong/empty/unparseable/refused model output is NEVER retried
  * mock and dry runs can only write under outputs/test_runs/ and are labelled TEST ONLY;
    real backends can only write under outputs/runs/

Modes
  full     225 requests (200 primary + 25 L_false_far sensitivity)
  primary  200 primary requests
  smoke    10 requests: 2 items x 5 primary conditions (one item with the error at Step 2, one at Step >= 4)
--dry-run does everything except instantiate or call a backend: it writes config.json and
planned_requests.jsonl (exact prompts + the request settings) to a TEST ONLY run directory.
"""
import argparse
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends as be  # noqa: E402
import pilot_common as pc  # noqa: E402

REQUIRED_CONFIG = ["backend", "model", "model_revision", "temperature", "max_new_tokens", "seed",
                   "quantization", "device", "notes"]
# keys that may legitimately change between resume attempts (they do not affect generation)
RESUME_MUTABLE = {"notes", "max_attempts", "retry_backoff_seconds", "timeout_seconds", "api_key_env",
                  "min_free_ram_gb"}
SMOKE_SEED = 2026


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(run_dir, msg, echo=True):
    line = f"{now()}  {msg}"
    with open(run_dir / "run_log.txt", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    if echo:
        print(line)


def load_model_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_CONFIG if k not in cfg]
    if missing:
        raise SystemExit(f"config {path} is missing fields: {missing}")
    if cfg["backend"] not in be.BACKENDS:
        raise SystemExit(f"unknown backend {cfg['backend']!r}; choose one of {sorted(be.BACKENDS)}")
    if cfg["backend"] != "mock" and not cfg["model"]:
        raise SystemExit("config has an empty 'model'; fill in the real model before a real run")
    cfg.setdefault("max_attempts", 5)
    cfg.setdefault("retry_backoff_seconds", 5.0)
    return cfg


def smoke_items(requests):
    """Deterministic: one item with the true error at Step 2 and one at Step >= 4 (seed 2026)."""
    prim = {r["item_id"]: r["true_error_step"] for r in requests if r["source_file"] == pc.PRIMARY}
    rng = random.Random(SMOKE_SEED)
    step2 = sorted(i for i, t in prim.items() if t == 2)
    late = sorted(i for i, t in prim.items() if t >= 4)
    return [rng.choice(step2), rng.choice(late)]


def select(requests, order, mode):
    by_key = {r["request_key"]: r for r in requests}
    ordered = [by_key[o["request_key"]] | {"run_index": o["run_index"]} for o in order]
    if mode == "full":
        return ordered
    if mode == "primary":
        return [r for r in ordered if r["source_file"] == pc.PRIMARY]
    if mode == "smoke":
        keep = set(smoke_items(requests))
        return [r for r in ordered if r["source_file"] == pc.PRIMARY and r["item_id"] in keep]
    raise SystemExit(f"unknown mode {mode}")


def run_root_for(cfg, dry_run, output_root):
    test_only = dry_run or cfg["backend"] == "mock"
    root = Path(output_root) if output_root else (pc.TEST_RUNS_ROOT if test_only else pc.RUNS_ROOT)
    in_test_tree = "test_runs" in root.resolve().parts
    if test_only and not in_test_tree:
        raise SystemExit(f"mock/dry runs must write under a 'test_runs' directory, not {root}")
    if not test_only and in_test_tree:
        raise SystemExit(f"real runs must not write under a 'test_runs' directory ({root})")
    return root, test_only


def config_document(run_id, mode, cfg, selected, hashes, order_doc, test_only, dry_run, shuffled, effective,
                    versions):
    return {
        "run_id": run_id,
        "TEST_ONLY": test_only,
        "dry_run": dry_run,
        "mode": mode,
        "created": now(),
        "model_config": cfg,
        "effective_generation_settings": effective,
        "input_sha256": hashes,
        "execution_order": {"file": "outputs/pilot_execution_order.json", "seed": order_doc["seed"],
                            "shuffled": shuffled, "order_sha256": order_doc["order_sha256"]},
        "execution_order_known_limitation": order_limitation(order_doc),
        "condition_alias": pc.ANALYSIS_ALIAS,
        "request_protocol": "one fresh request per prompt; single user message; no system prompt; no history",
        "retry_policy": {"retry_on": "transport errors only (timeout, connection, HTTP 408/429/5xx)",
                         "never_retry_on": "any model output, including wrong, empty, malformed or refused",
                         "max_attempts": cfg["max_attempts"]},
        "environment": be.environment_info(),
        "package_versions": versions,
        "smoke_items": smoke_items_from(selected) if mode == "smoke" else None,
        "selected_requests": [{k: r[k] for k in ("run_index", "request_key", "item_id", "condition_source",
                                                 "analysis_condition", "source_file", "row_index",
                                                 "prompt_sha256")} for r in selected],
    }


def order_limitation(order_doc):
    """Recorded, not corrected: the order is a plain seeded shuffle, not position-stratified."""
    pos = {}
    for o in order_doc["order"]:
        pos.setdefault(o["analysis_condition"], []).append(o["run_index"])
    n = len(order_doc["order"])
    return {"description": "plain seeded shuffle (not stratified by position); conditions are not exactly "
                           "balanced across early/late positions",
            "expected_mean_run_index": (n - 1) / 2,
            "mean_run_index_by_condition": {c: round(sum(v) / len(v), 1) for c, v in sorted(pos.items())},
            "quartile_counts_by_condition": {c: [sum(1 for i in v if min(i * 4 // n, 3) == q) for q in range(4)]
                                             for c, v in sorted(pos.items())}}


def smoke_items_from(selected):
    return sorted({(r["item_id"], r["true_error_step"]) for r in selected})


def check_resume(existing, new):
    problems = []
    if existing["input_sha256"] != new["input_sha256"]:
        problems.append("input file hashes changed")
    if existing["execution_order"] != new["execution_order"]:
        problems.append("execution order changed")
    if existing["mode"] != new["mode"] or existing["dry_run"] != new["dry_run"]:
        problems.append("mode changed")
    if existing["selected_requests"] != new["selected_requests"]:
        problems.append("request selection changed")
    a = {k: v for k, v in existing["model_config"].items() if k not in RESUME_MUTABLE}
    b = {k: v for k, v in new["model_config"].items() if k not in RESUME_MUTABLE}
    if a != b:
        problems.append(f"model config changed: {a} -> {b}")
    if problems:
        raise SystemExit("refusing to resume run '%s': %s" % (existing["run_id"], "; ".join(problems)))


class RunLock:
    def __init__(self, run_dir):
        self.path = run_dir / "run.lock"

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit(f"{self.path} exists: another process may be running this run. Remove the lock "
                             "only if you are sure no other process is using this directory.")
        os.write(fd, f"pid {os.getpid()} {now()}\n".encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def raw_row(run_id, req, result, attempts, latency, cfg, test_only):
    return {
        "run_id": run_id, "run_index": req["run_index"], "request_key": req["request_key"],
        "item_id": req["item_id"], "condition_source": req["condition_source"],
        "analysis_condition": req["analysis_condition"], "source_file": req["source_file"],
        "row_index": req["row_index"], "prompt": req["prompt"], "prompt_sha256": req["prompt_sha256"],
        "raw_response": result.text, "gold_answer": req["gold_answer"], "corrupted_answer": req["corrupted_answer"],
        "true_error_step": req["true_error_step"], "pointed_step": req["pointed_step"],
        "status": "completed", "attempt_count": attempts, "latency_seconds": round(latency, 4),
        "timestamp": now(),
        "model_metadata": {"backend": cfg["backend"], "model": cfg["model"], "model_revision": cfg["model_revision"],
                           "quantization": cfg["quantization"], "device": cfg["device"],
                           "temperature": cfg["temperature"], "max_new_tokens": cfg["max_new_tokens"],
                           "seed": cfg["seed"], "dtype": cfg.get("dtype"),
                           "enable_thinking": cfg.get("enable_thinking"), "response": result.metadata},
        "test_only": test_only,
    }


def execute(args, backend=None, files=None, order_path=None, sleep=time.sleep):
    """Programmatic entry point (the tests call this with a mock backend and temp paths)."""
    files = files or pc.INPUT_FILES
    cfg = load_model_config(args.config)
    hashes = pc.input_hashes(files)
    requests = pc.load_requests(files)
    shuffled = not args.sequential
    order_doc = pc.order_document(requests, hashes, shuffle=shuffled)
    if shuffled:
        pc.write_or_verify_order(order_doc, order_path or pc.ORDER_FILE)
    selected = select(requests, order_doc["order"], args.mode)
    if args.max_requests is not None:
        selected = selected[: args.max_requests]

    root, test_only = run_root_for(cfg, args.dry_run, args.output_root)
    run_dir = root / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        effective, versions = {"note": "dry run: backend not instantiated"}, {}
    else:
        backend = backend or be.make_backend(cfg)
        effective, versions = backend.effective_settings(), backend.versions()
    doc = config_document(args.run_id, args.mode, cfg, selected, hashes, order_doc, test_only, args.dry_run,
                          shuffled, effective, versions)
    cfg_path = run_dir / "config.json"
    if cfg_path.exists():
        check_resume(json.loads(cfg_path.read_text(encoding="utf-8")), doc)
        resumed = True
    else:
        cfg_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        resumed = False

    with RunLock(run_dir):
        if test_only:
            log(run_dir, "TEST ONLY run (mock backend or dry run): never mix with experimental results")
        if args.dry_run:
            pc.write_jsonl(run_dir / "planned_requests.jsonl",
                           [{"run_index": r["run_index"], "request_key": r["request_key"], "item_id": r["item_id"],
                             "analysis_condition": r["analysis_condition"], "prompt": r["prompt"],
                             "prompt_sha256": r["prompt_sha256"],
                             "request_settings": {k: cfg[k] for k in ("backend", "model", "temperature",
                                                                      "max_new_tokens", "seed")}}
                            for r in selected])
            log(run_dir, f"DRY RUN: {len(selected)} requests planned, 0 sent")
            return run_dir, {"planned": len(selected), "sent": 0}

        raw_path = run_dir / "raw_outputs.jsonl"
        done_rows, note = pc.read_jsonl_tolerant(raw_path)
        if note:
            log(run_dir, "RECOVERY: " + note)
        done = {r["request_key"] for r in done_rows}
        if len(done) != len(done_rows):
            raise SystemExit(f"{raw_path} contains duplicate request keys; refusing to continue")
        pending = [r for r in selected if r["request_key"] not in done]
        log(run_dir, f"{'RESUME' if resumed else 'START'} run {args.run_id}: {len(selected)} selected, "
                     f"{len(done)} already completed, {len(pending)} pending")

        source_prompts = {r["request_key"]: r["prompt"] for r in pc.load_requests(files)}
        stats = {"completed_now": 0, "transport_failed": 0, "retries": 0}
        for req in pending:
            if pc.sha256_text(req["prompt"]) != req["prompt_sha256"] or source_prompts[req["request_key"]] != req["prompt"]:
                raise SystemExit(f"prompt for {req['request_key']} no longer matches the frozen source; stopping")
            attempts, result = 0, None
            t0 = time.perf_counter()
            while attempts < cfg["max_attempts"]:
                attempts += 1
                try:
                    t0 = time.perf_counter()
                    result = backend.generate(req["prompt"])
                    latency = time.perf_counter() - t0
                    break
                except be.TransportError as e:
                    log(run_dir, f"transport error on {req['request_key']} attempt {attempts}/{cfg['max_attempts']}: {e}")
                    if attempts < cfg["max_attempts"]:
                        stats["retries"] += 1
                        sleep(cfg["retry_backoff_seconds"] * 2 ** (attempts - 1))
            if result is None:
                stats["transport_failed"] += 1
                pc.append_jsonl(run_dir / "transport_failures.jsonl",
                                {"run_id": args.run_id, "request_key": req["request_key"], "run_index": req["run_index"],
                                 "attempt_count": attempts, "timestamp": now(),
                                 "note": "no model response; will be retried on resume"})
                continue
            pc.append_jsonl(raw_path, raw_row(args.run_id, req, result, attempts, latency, cfg, test_only))
            stats["completed_now"] += 1
            log(run_dir, f"done {req['request_key']} run_index={req['run_index']} {req['analysis_condition']} "
                         f"attempts={attempts} latency={latency:.3f}s", echo=False)

        log(run_dir, f"finished pass: {stats}")

    from score_pilot import score_run
    summary, _ = score_run(run_dir)
    log(run_dir, f"parsed and scored {summary['n_completed']}/{summary['n_planned']} completed requests")
    return run_dir, stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--mode", choices=["full", "primary", "smoke"], default="full")
    ap.add_argument("--smoke-test", action="store_true", help="alias for --mode smoke")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sequential", action="store_true", help="source-file order instead of the seeded shuffle")
    ap.add_argument("--max-requests", type=int, default=None)
    ap.add_argument("--output-root", default=None)
    args = ap.parse_args(argv)
    if args.smoke_test:
        args.mode = "smoke"
    try:
        run_dir, stats = execute(args)
    except be.BackendConfigError as e:
        raise SystemExit(f"backend configuration error (not retried): {e}")
    except Exception:
        traceback.print_exc()
        raise SystemExit("run aborted; completed responses are on disk and the run can be resumed")
    print(f"run directory: {run_dir}\n{stats}")


if __name__ == "__main__":
    main()
