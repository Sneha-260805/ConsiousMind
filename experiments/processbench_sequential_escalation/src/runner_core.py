"""Crash-safe, resumable inference loop shared by Stage 1 and Stage 2 (same design as the Stage-A runner).

Per request, in the frozen execution order:
  1. the globally unique request key is checked against raw_outputs.jsonl: a completed request is NEVER regenerated
  2. the prompt's SHA-256 is re-checked against the frozen request file
  3. one stateless call: backend.generate(prompt)  (no history, no system message, no item metadata)
  4. the row is appended to raw_outputs.jsonl, flushed and fsync'ed; state.json is updated atomically
  5. every `checkpoint_every` completed rows a checkpoint ZIP is written atomically
Retries: ONLY transport errors (pilot backends.TransportError) are retried, up to transport_retry_attempts. Wrong,
malformed, empty, refused or truncated outputs are observed outcomes: stored once, never retried. Any other
exception stops the run with a runtime_failure record; rerunning the same command resumes where it stopped.
A run directory is bound to one run manifest (identity fields). Resuming with anything different stops.
"""
import json
import time
import traceback
import zipfile
from pathlib import Path

from . import common as C
from . import frozen_sources as fs
from . import parser
from .persistence import RunStore, read_jsonl_tolerant

IDENTITY_FIELDS = ["run_id", "stage", "mode", "test_only", "backend", "solver_tag", "model", "model_revision",
                   "frozen_solver_config_sha256", "solver_overrides", "max_new_tokens", "enable_thinking",
                   "decoding", "requests_sha256", "execution_order_sha256", "items_sha256", "planned_keys_sha256",
                   "upstream_sha256", "parser_version", "parser_sha256", "protocol_amendments"]
META_FIELDS = ["stage", "item_id", "domain", "condition", "pointed_step", "true_error_step", "gold_answer",
               "original_answer", "stage1_request_key", "stage1_numeric_answer", "stage1_outcome",
               "stage1_response_sha256"]


def load_requests(requests_path, order_path):
    reqs = C.read_jsonl(requests_path)
    order = C.read_json(order_path)
    by_key = {r["request_key"]: r for r in reqs}
    if len(by_key) != len(reqs):
        raise SystemExit("STOP: duplicate request keys in the request file")
    if set(o["request_key"] for o in order["order"]) != set(by_key):
        raise SystemExit("STOP: execution order and request file disagree")
    if C.sha256_text("\n".join(o["request_key"] for o in order["order"])) != order["order_sha256"]:
        raise SystemExit("STOP: execution order hash mismatch")
    for r in reqs:
        if C.sha256_text(r["prompt"]) != r["prompt_sha256"]:
            raise SystemExit(f"STOP: prompt hash mismatch for {r['request_key']}")
    return by_key, order


def run_manifest(run_id, stage, mode, test_only, backend_name, cfg, solver_cfg, solver_sha, overrides, hashes, keys):
    return {
        "run_id": run_id, "stage": stage, "mode": mode, "test_only": test_only, "backend": backend_name,
        "solver_tag": cfg["solver"]["solver_tag"], "model": solver_cfg.get("model"),
        "model_revision": solver_cfg.get("model_revision"), "frozen_solver_config_sha256": solver_sha,
        "solver_overrides": overrides, "max_new_tokens": solver_cfg.get("max_new_tokens"),
        "enable_thinking": solver_cfg.get("enable_thinking"), "decoding": solver_cfg.get("decoding"),
        "requests_sha256": hashes["requests"], "execution_order_sha256": hashes["order"],
        "items_sha256": hashes["items"], "upstream_sha256": hashes.get("upstream"),
        "planned_keys_sha256": C.sha256_text("\n".join(keys)), "n_planned": len(keys),
        "parser_version": parser.parser_version(), "parser_sha256": C.sha256_file(C.STAGE_ROOT / "src" / "parser.py"),
        "protocol_amendments": [a["id"] for a in cfg.get("protocol_amendments", [])],
        "request_protocol": "stateless; one user message; no system message; no history; batch size 1",
        "retry_policy": "transport errors only; model outputs are never retried",
        "oracle_status": C.ORACLE_STATEMENT, "created": C.now(),
    }


def check_or_write_manifest(run_dir, manifest):
    p = Path(run_dir) / "run_manifest.json"
    if p.exists():
        old = json.loads(p.read_text(encoding="utf-8"))
        diff = [f for f in IDENTITY_FIELDS if old.get(f) != manifest.get(f)]
        if diff:
            raise SystemExit(f"STOP: {run_dir} belongs to a different run (differs in {diff}); refusing to resume "
                             "into it. Use a new --run-id.")
        return False
    C.write_json(p, manifest)
    return True


def import_previous(run_dir, source, manifest, by_key):
    """Seed a FRESH run directory with rows completed in an earlier session (run dir or checkpoint/final ZIP).
    Accepted only if the earlier manifest has the same identity and every row matches the frozen prompt."""
    run_dir, source = Path(run_dir), Path(source)
    if (run_dir / "raw_outputs.jsonl").exists():
        raise SystemExit("STOP: --resume-from needs an empty run directory (this one already has outputs)")
    if source.is_dir():
        raw_bytes = (source / "raw_outputs.jsonl").read_bytes()
        old_man = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    else:
        with zipfile.ZipFile(source) as z:
            names = z.namelist()
            raw_bytes = z.read(next(n for n in names if n.endswith("raw_outputs.jsonl")))
            old_man = json.loads(z.read(next(n for n in names if n.endswith("run_manifest.json"))))
    diff = [f for f in IDENTITY_FIELDS if old_man.get(f) != manifest.get(f)]
    if diff:
        raise SystemExit(f"STOP: previous outputs come from a different run (differs in {diff})")
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / "_import.jsonl"
    tmp.write_bytes(raw_bytes)
    rows, note = read_jsonl_tolerant(tmp)
    tmp.unlink()
    seen = set()
    for r in rows:
        k = r["request_key"]
        if k in seen or k not in by_key or r["prompt_sha256"] != by_key[k]["prompt_sha256"] \
                or C.sha256_text(r["prompt"]) != r["prompt_sha256"]:
            raise SystemExit(f"STOP: imported row {k} is duplicated, unknown or has a different prompt")
        seen.add(k)
    C.write_jsonl(run_dir / "raw_outputs.jsonl", rows)
    return len(rows), note


def execute(run_dir, by_key, order, keys, backend, manifest, settings, log=print, force_unlock=False, limit=None):
    TransportError = fs.pilot_module("backends").TransportError
    store = RunStore(run_dir, settings["checkpoint_every"])
    store.acquire(force_unlock)
    run_dir = Path(run_dir)
    logf = run_dir / "run_log.txt"

    def say(msg):
        line = f"{C.now()}  {msg}"
        with open(logf, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        log(line)
    try:
        check_or_write_manifest(run_dir, manifest)
        rows = store.completed_rows()
        if store.note:
            say("NOTE: " + store.note)
        done = {r["request_key"] for r in rows}
        unknown = done - set(keys)
        if unknown:
            raise SystemExit(f"STOP: raw_outputs.jsonl has rows outside this run's plan: {sorted(unknown)[:5]}")
        run_index = {o["request_key"]: o["run_index"] for o in order["order"]}
        todo = [k for k in keys if k not in done]
        if limit is not None:
            todo = todo[:limit]          # debugging only: the run's plan (and identity) is unchanged
        say(f"{'RESUMING' if done else 'STARTING'} run {manifest['run_id']} (stage {manifest['stage']}, "
            f"{manifest['mode']}, backend {manifest['backend']}{', TEST ONLY' if manifest['test_only'] else ''}): "
            f"{len(done)}/{len(keys)} already completed, {len(todo)} to go. Completed requests are never regenerated.")
        stats = {"generated": 0, "transport_failures": 0, "skipped_after_transport_failures": 0}
        for k in todo:
            req = by_key[k]
            if C.sha256_text(req["prompt"]) != req["prompt_sha256"]:
                raise SystemExit(f"STOP: prompt hash mismatch for {k}")
            attempt = 0
            while True:
                attempt += 1
                t0 = time.perf_counter()
                try:
                    res = backend.generate(req["prompt"])
                except TransportError as e:
                    stats["transport_failures"] += 1
                    store.transport_failure({"timestamp": C.now(), "request_key": k, "attempt": attempt,
                                             "error": f"{type(e).__name__}: {e}"})
                    if attempt >= settings["transport_retry_attempts"]:
                        stats["skipped_after_transport_failures"] += 1
                        say(f"transport failure x{attempt} on {k}; left incomplete (rerun to resume)")
                        break
                    time.sleep(settings["transport_retry_backoff_seconds"])
                    continue
                latency = time.perf_counter() - t0
                row = {"run_id": manifest["run_id"], "run_index": run_index[k], "request_key": k,
                       **{f: req.get(f) for f in META_FIELDS},
                       "prompt": req["prompt"], "prompt_sha256": req["prompt_sha256"],
                       "raw_response": res.text,
                       "model_metadata": {"response": res.metadata, "backend": manifest["backend"],
                                          "model": manifest["model"], "model_revision": manifest["model_revision"],
                                          "max_new_tokens": manifest["max_new_tokens"],
                                          "enable_thinking": manifest["enable_thinking"]},
                       "attempt_count": attempt, "latency_seconds": round(latency, 3), "timestamp": C.now(),
                       "solver_tag": manifest["solver_tag"], "test_only": manifest["test_only"]}
                store.append(row, done)                 # durable (fsync) before anything else
                stats["generated"] += 1
                try:                                    # derived files: a failure here never stops generation
                    store.write_state(len(done), len(keys), k)
                    for c in store.checkpoints_due(len(done)):
                        store.checkpoint(c)
                        say(f"CHECKPOINT {c}/{len(keys)} written")
                except OSError as e:
                    stats["derived_file_warnings"] = stats.get("derived_file_warnings", 0) + 1
                    say(f"WARNING: state/checkpoint write failed ({type(e).__name__}: {e}); generation continues")
                if stats["generated"] % 10 == 0 or len(done) == len(keys):
                    say(f"[{len(done)}/{len(keys)}] completed (last latency {latency:.1f}s)")
                break
        stats.update(completed=len(done), planned=len(keys), complete=len(done) == len(keys))
        if stats["complete"]:
            store.checkpoint(len(done), name="checkpoint_FINAL.zip")
        say(f"pass finished: {stats}")
        return stats
    except BaseException as e:
        if not isinstance(e, SystemExit):
            n = len(store.completed_keys()) if store.raw.exists() else 0
            C.write_json(run_dir / f"runtime_failure_{int(time.time())}.json",
                         {"failed": C.now(), "error": f"{type(e).__name__}: {e}",
                          "traceback": traceback.format_exc(), "completed_rows_on_disk": n,
                          "policy": "files kept; nothing regenerated; rerun the same command to resume"})
        raise
    finally:
        store.release()


def score_run(run_dir):
    """Parse + score every completed row; write scored_outputs.jsonl and run_diagnostics.json (integrity only)."""
    import re
    import statistics
    from collections import Counter
    from . import scorer
    run_dir = Path(run_dir)
    raw, note = read_jsonl_tolerant(run_dir / "raw_outputs.jsonl", repair=False)
    scored = scorer.score_rows(raw)
    C.write_jsonl(run_dir / "scored_outputs.jsonl", scored)
    man = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    tf = run_dir / "transport_failures.jsonl"
    toks = [s["new_tokens"] for s in scored if s["new_tokens"] is not None]
    ptoks = [s["prompt_tokens"] for s in scored if s["prompt_tokens"] is not None]
    diag = {
        "LABEL": "INTEGRITY CHECK ONLY - no repair/rescue rates or condition comparisons",
        "TEST_ONLY": man["test_only"], "run_id": man["run_id"], "stage": man["stage"], "mode": man["mode"],
        "backend": man["backend"], "planned": man["n_planned"], "completed": len(raw),
        "duplicate_keys": len(raw) - len({r["request_key"] for r in raw}), "raw_file_note": note,
        "transport_failure_records": len(tf.read_text(encoding="utf-8").splitlines()) if tf.exists() else 0,
        "retries_used": sum(r["attempt_count"] - 1 for r in raw),
        "completed_by_condition": dict(Counter(r["condition"] for r in raw)),
        "numeric_parse_status": dict(Counter(s["numeric_parse_status"] for s in scored)),
        "strict_format_compliant": sum(s["strict_format_compliant"] for s in scored),
        "truncated_at_max_new_tokens": [s["request_key"] for s in scored if s["truncated"]],
        "truncated_without_valid_answer": [s["request_key"] for s in scored if s["truncated_without_valid_answer"]],
        "visible_think_tags": [r["request_key"] for r in raw if re.search(r"</?think>", r["raw_response"])
                               or ((r["model_metadata"]["response"] or {}).get("think_tag_in_output"))],
        "prompts_match_sha": all(r["prompt_sha256"] == C.sha256_text(r["prompt"]) for r in raw),
        "generated_tokens_mean": round(statistics.mean(toks), 1) if toks else None,
        "generated_tokens_max": max(toks) if toks else None,
        "prompt_tokens_max": max(ptoks) if ptoks else None,
        "scorer_version": scored[0]["scorer_version"] if scored else None,
        "parser_version": scored[0]["parser_version"] if scored else None,
    }
    C.write_json(run_dir / "run_diagnostics.json", diag)
    return diag, scored
