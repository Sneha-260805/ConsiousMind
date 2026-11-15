"""Fresh-session Kaggle runner for the frozen 225-request pilot (bundle v5), built for
Kaggle -> Save Version -> Save & Run All (background run whose /kaggle/working is kept as the version Output).

Used by kaggle/run_full_768_persistent.ipynb (11 cells, top to bottom, no manual steps). Nothing scientific is
new: generation goes through the same audited run_pilot.execute + backends.HFTransformersBackend +
parse_answers + score_pilot with the frozen config (configs/qwen3_4b_kaggle.json, max_new_tokens 768), the
frozen prompts, conditions, false-hint assignments and execution order (seed 2026).

Added here (persistence only; never touches a prompt, a setting or a model output):
  * a stage guard: every cell refuses to run unless the previous cell completed (so a failed check can
    never be followed by experimental requests, even in a non-interactive run)
  * a fresh-runtime check: no previous /kaggle/working results, model objects or bridge cells are used
  * checkpoint ZIPs /kaggle/working/checkpoints/checkpoint_025.zip ... checkpoint_225.zip, each holding EXACTLY
    the first N completed rows (raw_outputs.jsonl is still appended + fsync'ed per request by run_pilot)
  * /kaggle/working/results_full_768_FINAL.zip with the whole results/full_768/ directory, verified
  * on a mid-run failure: all files kept, runtime_failure_*.json written, partial ZIP made, run stops
    (no retries, no setting changes)
No repair rates, CCA, condition comparisons or tests are computed or printed here.
"""
import argparse
import hashlib
import inspect
import json
import os
import re
import statistics
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import kaggle_runner as kr

BUNDLE_VERSION = 5
PIN = "3101254bbe4169895668a0e7653c3fd1f313576e"
ORDER_SHA = "2b6e7e0f0895b002976358df618cba33c8bc6dbf38a31f0c9567dc684466ec91"
CONFIG_SHA = "3118f86d5e0a1fae7228c7db086b2e2ba15210c23d67ee3dc0560ab01c98a77f"   # frozen 768 config (since v3)
RUN_ID, N_TOTAL, CHECKPOINT_EVERY = "full_768", 225, 25
MILESTONES = list(range(CHECKPOINT_EVERY, N_TOTAL + 1, CHECKPOINT_EVERY))          # 25, 50, ..., 225
EXPECTED = {"L0": 40, "V": 40, "L_false_near": 40, "L": 40, "A": 40, "L_false_far": 25}
PROTOCOL = {"model": "Qwen/Qwen3-4B", "model_revision": PIN, "dtype": "float16", "device": "cuda:0",
            "temperature": 0, "enable_thinking": False, "max_new_tokens": 768, "max_attempts": 1,
            "decoding": "greedy", "quantization": "none", "seed": 2026}
REQUIRED_RUN_FILES = ["raw_outputs.jsonl", "parsed_outputs.jsonl", "scored_outputs.jsonl", "config.json",
                      "environment.json", "run_log.txt", "summary.json", "full_run_diagnostics.json",
                      "paired_primary.csv", "paired_far_subset.csv", "provenance.json"]
WORKING = Path("/kaggle/working")
RUN_DIR = kr.RESULTS / RUN_ID
CKPT_DIR = WORKING / "checkpoints"
FINAL_ZIP = WORKING / "results_full_768_FINAL.zip"
_STATE = {"done": 0}


class StopRun(RuntimeError):
    """Raised for every STOP: a normal exception, so a Save & Run All execution halts at that cell."""


def stop(msg):
    raise StopRun("STOP: " + msg)


def enter(n):
    if _STATE["done"] != n - 1:
        stop(f"cell {n} cannot run: cell {n - 1} did not complete in this session (last completed: "
             f"{_STATE['done']}). Nothing was sent.")


def done(n):
    _STATE["done"] = n
    print(f"\n[CELL {n} COMPLETE]", flush=True)


def _sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_zip(dest, members):
    """members: [(arcname, bytes)]. Written to <dest>.tmp, fsync'ed, then renamed over dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, data in members:
                z.writestr(arc, data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    _fsync_dir(dest.parent)
    return dest


def _complete_lines():
    """Every complete line of raw_outputs.jsonl (never a row still being written)."""
    p = RUN_DIR / "raw_outputs.jsonl"
    if not p.exists():
        return []
    data = p.read_bytes()
    return [l + b"\n" for l in data[: data.rfind(b"\n") + 1].split(b"\n")[:-1] if l]


def completed_rows():
    out = []
    for line in _complete_lines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# ============================================================ CELL 1
def cell1_environment():
    enter(1)
    leftovers = [str(p) for p in (kr.RESULTS, CKPT_DIR, FINAL_ZIP) if p.exists()]
    if leftovers:
        stop(f"not a fresh runtime: {leftovers} already exist. This notebook never re-uses earlier "
             "/kaggle/working files. Use Save Version -> Save & Run All (always a fresh runtime), or restart "
             "the session / factory-reset the runtime.")
    gpu = kr.gpu_check()
    gpu["kaggle_kernel_run_type"] = os.environ.get("KAGGLE_KERNEL_RUN_TYPE")
    if "T4" not in gpu["device_name"]:
        stop(f"GPU is {gpu['device_name']}; the frozen protocol uses a Kaggle Tesla T4 (Settings -> "
             "Accelerator -> GPU T4 x2)")
    print("run type:", gpu["kaggle_kernel_run_type"], "(Batch = Save & Run All; persisted as version output)")
    done(1)
    return gpu


# ============================================================ CELL 2
def cell2_packages():
    enter(2)
    versions = kr.ensure_packages()
    done(2)
    return versions


# ============================================================ CELL 3
def cell3_stage():
    enter(3)
    manifest = kr.stage_and_verify()
    if manifest.get("bundle_version") != BUNDLE_VERSION:
        stop(f"bundle version {manifest.get('bundle_version')} attached; this notebook needs v{BUNDLE_VERSION}")
    here = Path(__file__).resolve()
    if kr.sha256_file(here) != manifest["files"]["scripts/kaggle_full_run.py"]:
        stop("kaggle_full_run.py hash mismatch")
    if kr.sha256_file(kr.WORK / kr.CONFIG_REL) != CONFIG_SHA:
        stop("config file is not the frozen 768 config")
    done(3)
    return manifest


# ============================================================ CELL 4
def cell4_load():
    enter(4)
    import gc
    import warnings
    import backends as be
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if any(isinstance(o, be.HFTransformersBackend) for o in gc.get_objects()):
            stop("a model object already exists in this runtime; this notebook needs a fresh runtime")
    backend = kr.load_model()                 # Hub SHA verified before loading; loads snapshots/<pin>
    done(4)
    return backend


# ============================================================ CELL 5
def cell5_verify(backend):
    enter(5)
    import torch
    template = kr.template_report(backend)
    cfg = json.loads((kr.WORK / kr.CONFIG_REL).read_text(encoding="utf-8"))
    eff = backend.effective_settings()
    gen_src = inspect.getsource(type(backend).generate)
    checks = {
        "config_matches_frozen_protocol": all(cfg.get(k) == v for k, v in PROTOCOL.items()),
        "backend_cfg_identical_to_config_file": backend.cfg == cfg,
        "fp16": backend.model.dtype == torch.float16 and "float16" in eff["dtype"],
        "cuda0": str(backend.model.device) == "cuda:0" == eff["device"],
        "enable_thinking_false": backend.enable_thinking is False and eff["enable_thinking"] is False,
        "greedy_do_sample_false_num_beams_1": "do_sample=False, num_beams=1, temperature=None" in gen_src
                                              and eff["decoding"] == "greedy (do_sample=False, num_beams=1)",
        "generate_not_patched": "generate" not in vars(backend),
        "max_new_tokens_768": backend.cfg["max_new_tokens"] == eff["max_new_tokens"] == 768,
        "single_user_message_no_system": "system" not in backend.template_check["prefix"]
                                         and eff["system_prompt"] is None,
        "batch_size_1": "input_ids=ids" in gen_src and "enc = self.tok(text_in" in gen_src,
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    kr.save(kr.RESULTS / "diagnostics" / "backend_verification.json",
            {"checked": kr.now(), "checks": checks, "effective_settings": eff})
    if not all(checks.values()):
        stop("backend verification failed")
    done(5)
    return template


# ============================================================ CELL 6
def cell6_canary(backend):
    enter(6)
    out = kr.canary(backend)                  # non-experimental; results/diagnostics/canary.json only
    done(6)
    return out


# ============================================================ CELL 7
def cell7_preflight(backend, gpu, versions, manifest):
    enter(7)
    import torch
    from huggingface_hub import HfApi
    import pilot_common as pc
    import run_pilot as rp
    pf = {}
    cfg_path = kr.WORK / kr.CONFIG_REL
    cfg, cfg_sha = json.loads(cfg_path.read_text(encoding="utf-8")), kr.sha256_file(cfg_path)
    eff = backend.effective_settings()
    pf["01_config_file_is_frozen_768_config"] = cfg_sha == CONFIG_SHA
    pf["02_config_matches_frozen_protocol"] = all(cfg.get(k) == v for k, v in PROTOCOL.items())
    pf["03_backend_cfg_identical_to_config_file"] = backend.cfg == cfg
    hub_sha = HfApi().model_info("Qwen/Qwen3-4B", revision=PIN).sha
    pf["04_hub_revision_exact"] = hub_sha == PIN == eff.get("hub_resolved_revision")
    pf["05_fp16_cuda0"] = backend.model.dtype == torch.float16 and str(backend.model.device) == "cuda:0"
    pf["06_gpu_tesla_t4"] = "T4" in gpu["device_name"]
    pf["07_enable_thinking_false_greedy"] = (backend.enable_thinking is False and cfg["temperature"] == 0
                                             and cfg["max_attempts"] == 1 and "generate" not in vars(backend))
    pf["08_frozen_file_hashes"] = all(kr.sha256_file(kr.WORK / rel) == h for rel, h in kr.FROZEN_SHA256.items())
    pf["09_all_bundle_files_match_manifest"] = all(kr.sha256_file(kr.WORK / rel) == h
                                                   for rel, h in manifest["files"].items())
    requests = pc.load_requests()
    order_doc = pc.order_document(requests, pc.input_hashes())
    order_file = json.loads(pc.ORDER_FILE.read_text(encoding="utf-8"))
    pf["10_execution_order_hash"] = order_doc["order_sha256"] == order_file["order_sha256"] == ORDER_SHA
    tc, render_ok, roundtrip_ok = backend.template_check, True, True
    for r in requests:                                   # all 225 frozen prompts, rendered + tokenised only
        text = backend.render(r["prompt"], False)
        render_ok &= text == tc["prefix"] + r["prompt"] + tc["suffix"] and text.count(r["prompt"]) == 1
        ids = backend.tok(text, add_special_tokens=False).input_ids
        roundtrip_ok &= backend.tok.decode(ids, skip_special_tokens=False,
                                           clean_up_tokenization_spaces=False) == text
    pf["11_frozen_prompts_byte_identical_in_requests"] = bool(render_ok and roundtrip_ok)
    selected = rp.select(requests, order_doc["order"], "full")
    counts = {c: sum(r["analysis_condition"] == c for r in selected) for c in EXPECTED}
    pf["12_selection_225_exact"] = (len(selected) == N_TOTAL == len({r["request_key"] for r in selected})
                                    and counts == EXPECTED)
    pf["13_prompt_sha256_match"] = all(pc.sha256_text(r["prompt"]) == r["prompt_sha256"] for r in selected)
    canary = json.loads((kr.RESULTS / "diagnostics" / "canary.json").read_text(encoding="utf-8"))
    pf["14_template_and_canary_passed_this_session"] = canary.get("passed") is True
    pf["15_no_previous_run_outputs"] = not RUN_DIR.exists() and not CKPT_DIR.exists()
    print("PRE-FLIGHT CHECK (225-request experiment)")
    for k, v in pf.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print("planned:", N_TOTAL, counts)
    kr.save(kr.RESULTS / "diagnostics" / "full_768_preflight.json",
            {"checked": kr.now(), "checks": pf, "config_sha256": cfg_sha, "hub_resolved_revision": hub_sha,
             "planned_counts": counts})
    if not all(pf.values()):
        stop("pre-flight failed; NO experimental request was sent")
    prov = {"model": cfg["model"], "requested_revision": PIN, "hub_resolved_revision": hub_sha,
            "transformers_commit_hash": eff.get("transformers_commit_hash"),
            "snapshot_path": eff.get("snapshot_path"), "dtype": eff["dtype"], "device": eff["device"],
            "gpu": gpu.get("device_name"), "decoding": eff["decoding"], "temperature": cfg["temperature"],
            "enable_thinking": eff["enable_thinking"], "max_new_tokens": eff["max_new_tokens"], "batch_size": 1,
            "request_protocol": "stateless; one user message; no system message; no history",
            "config_sha256": cfg_sha, "execution_order_sha256": ORDER_SHA,
            "frozen_input_sha256": kr.FROZEN_SHA256, "input_sha256": pc.input_hashes(),
            "bundle_version": manifest.get("bundle_version"), "bundle_files_sha256": manifest.get("files"),
            "kaggle_kernel_run_type": os.environ.get("KAGGLE_KERNEL_RUN_TYPE"),
            "note": "fresh-session rerun of the frozen 225-request pilot (first run's files were lost); "
                    "no protocol change"}
    done(7)
    return {"cfg": cfg, "cfg_path": cfg_path, "cfg_sha": cfg_sha, "hub_sha": hub_sha, "requests": requests,
            "selected": selected, "counts": counts, "pf": pf, "prov": prov, "gpu": gpu, "versions": versions}


# ============================================================ CHECKPOINTS
def _run_files_members(names):
    out = []
    for name in names:
        p = RUN_DIR / name
        if p.exists():
            out.append((f"full_768/{name}", p.read_bytes()))
    return out


def write_checkpoint(k, ctx, name=None):
    """Checkpoint with EXACTLY the first k completed rows. Never modifies anything in results/."""
    lines = _complete_lines()
    if len(lines) < k:
        raise ValueError(f"only {len(lines)} rows on disk, cannot checkpoint {k}")
    raw = b"".join(lines[:k])
    members = [("full_768/raw_outputs.jsonl", raw)]
    members += _run_files_members(["config.json", "environment.json", "run_log.txt", "provenance.json",
                                   "transport_failures.jsonl"]
                                  + sorted(p.name for p in RUN_DIR.glob("runtime_failure_*.json")))
    for p in sorted((kr.RESULTS / "diagnostics").glob("*.json")):
        members.append((f"diagnostics/{p.name}", p.read_bytes()))
    rows = [json.loads(l) for l in lines[:k]]
    index = {"request_count": k, "of": N_TOTAL, "created": kr.now(),
             "keys_in_planned_order": [r["request_key"] for r in rows]
                                      == [r["request_key"] for r in ctx["selected"][:k]],
             "condition_counts": {c: sum(r["analysis_condition"] == c for r in rows) for c in EXPECTED},
             "config_sha256": ctx["cfg_sha"], "execution_order_sha256": ORDER_SHA, "model_revision": PIN,
             "frozen_input_sha256": kr.FROZEN_SHA256,
             "members_sha256": {a: _sha_bytes(b) for a, b in members}}
    members.append(("CHECKPOINT_INDEX.json", json.dumps(index, indent=2).encode()))
    dest = _atomic_zip(CKPT_DIR / (name or f"checkpoint_{k:03d}.zip"), members)
    with open(CKPT_DIR / "checkpoint_log.txt", "a", encoding="utf-8") as fh:
        fh.write(f"{kr.now()}  {dest.name}  rows={k}  bytes={dest.stat().st_size}  sha256={kr.sha256_file(dest)}\n")
        fh.flush()
        os.fsync(fh.fileno())
    print(f"\n*** CHECKPOINT READY: {k}/{N_TOTAL} -> {dest} ({dest.stat().st_size} bytes) ***\n", flush=True)
    return dest


def _checkpoint_due(ctx, lock):
    with lock:
        n = len(_complete_lines())
        for k in MILESTONES:
            if k <= n and not (CKPT_DIR / f"checkpoint_{k:03d}.zip").exists():
                try:
                    write_checkpoint(k, ctx)
                except Exception as e:                    # a checkpoint problem never stops generation
                    print(f"WARNING: checkpoint {k} failed ({type(e).__name__}: {e}); generation continues "
                          "(raw_outputs.jsonl is still fsync'ed per request)", flush=True)


def _partial_zip(n):
    dest = WORKING / f"results_full_768_PARTIAL_{n:03d}of{N_TOTAL}.zip"
    members = [(p.relative_to(WORKING).as_posix(), p.read_bytes())
               for p in sorted(kr.RESULTS.rglob("*")) if p.is_file() and p.name != "run.lock"]
    return _atomic_zip(dest, members)


# ============================================================ CELL 8
def cell8_run(backend, ctx):
    enter(8)
    import torch
    import run_pilot as rp
    t_cell = time.time()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    kr.save(RUN_DIR / "environment.json", kr.environment_record(ctx["gpu"], ctx["versions"]))
    kr.save(RUN_DIR / "provenance.json", ctx["prov"])
    n0 = len(completed_rows())
    print(f"{'RESUMING' if n0 else 'STARTING'} {RUN_ID}: {n0}/{N_TOTAL} already completed "
          "(completed requests are never regenerated)\n", flush=True)
    stop_evt, lock = threading.Event(), threading.Lock()

    def monitor():
        t0, next_mark = time.time(), (n0 // 10 + 1) * 10
        while not stop_evt.wait(3):
            _checkpoint_due(ctx, lock)
            rows = completed_rows()
            n = len(rows)
            if n >= next_mark:
                el = time.time() - t0
                eta = (N_TOTAL - n) * el / max(n - n0, 1)
                print(f"[{n}/{N_TOTAL}] completed | elapsed {el / 60:.1f} min | last latency "
                      f"{rows[-1]['latency_seconds']:.1f} s | ~{eta / 60:.0f} min remaining", flush=True)
                next_mark = (n // 10 + 1) * 10
            if n >= N_TOTAL:
                return

    torch.cuda.reset_peak_memory_stats(0)
    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    args = argparse.Namespace(config=str(ctx["cfg_path"]), run_id=RUN_ID, mode="full", dry_run=False,
                              sequential=False, max_requests=None, output_root=str(kr.RESULTS))
    try:
        rp.execute(args, backend=backend)              # per-request append + fsync; max_attempts 1
    except BaseException as e:                         # CUDA / runtime failure, resume refusal, interrupt
        stop_evt.set()
        mon.join(timeout=30)
        n = len(completed_rows())
        kr.save(RUN_DIR / f"runtime_failure_{int(time.time())}.json",
                {"failed": kr.now(), "error": f"{type(e).__name__}: {e}", "completed_rows_on_disk": n,
                 "policy": "files kept; no setting changed; no token-limit change; model outputs never retried"})
        _checkpoint_due(ctx, lock)
        try:
            print("partial archive:", _partial_zip(n), flush=True)
        except Exception as ze:
            print("partial archive failed:", ze)
        stop(f"{type(e).__name__}: {e}. {n}/{N_TOTAL} completed rows are on disk and in the latest checkpoint. "
             "Nothing was changed. Report this before doing anything else.")
    finally:
        stop_evt.set()
    mon.join(timeout=30)
    _checkpoint_due(ctx, lock)
    n = len(completed_rows())
    print(f"generation finished: {n}/{N_TOTAL} rows on disk | cell {(time.time() - t_cell) / 60:.1f} min",
          flush=True)
    if n != N_TOTAL:
        kr.save(RUN_DIR / f"runtime_failure_{int(time.time())}.json",
                {"failed": kr.now(), "error": f"pass ended with {n}/{N_TOTAL} rows (see transport_failures.jsonl)",
                 "completed_rows_on_disk": n})
        print("partial archive:", _partial_zip(n), flush=True)
        stop(f"only {n}/{N_TOTAL} requests completed; final ZIP not created")
    ctx["cell8_minutes"] = round((time.time() - t_cell) / 60, 1)
    ctx["gpu_peak_memory_gb"] = round(torch.cuda.max_memory_allocated(0) / 1e9, 2)
    done(8)


# ============================================================ CELL 9
def cell9_score():
    enter(9)
    from score_pilot import score_run
    before = {f: kr.sha256_file(RUN_DIR / f) for f in ("parsed_outputs.jsonl", "scored_outputs.jsonl")
              if (RUN_DIR / f).exists()}
    summary, scored = score_run(RUN_DIR)               # frozen parser + scorer (deterministic; same files)
    after = {f: kr.sha256_file(RUN_DIR / f) for f in ("parsed_outputs.jsonl", "scored_outputs.jsonl")}
    parsed_n = len((RUN_DIR / "parsed_outputs.jsonl").read_text(encoding="utf-8").splitlines())
    print("parsed rows:", parsed_n, "| scored rows:", len(scored), "| summary n_completed:",
          summary["n_completed"], "/", summary["n_planned"])
    print("re-scoring reproduced the files written at the end of cell 8:", before == after if before else "n/a")
    print("(per-condition numbers are in summary.json / paired_*.csv; they are NOT displayed or analysed here)")
    if not (parsed_n == len(scored) == summary["n_completed"] == summary["n_planned"] == N_TOTAL):
        stop("parse/score row counts are not 225")
    done(9)
    return scored


# ============================================================ CELL 10
def cell10_diagnostics(ctx, scored):
    enter(10)
    raw = completed_rows()
    by_key = {s["request_key"]: s for s in scored}
    frozen = {r["request_key"]: r["prompt"] for r in ctx["requests"]}
    toks = [r["model_metadata"]["response"].get("new_tokens") or 0 for r in raw]
    lat = [r["latency_seconds"] for r in raw]
    tf = RUN_DIR / "transport_failures.jsonl"
    flags = {"parse_failed": [], "hit_768": [], "lacks_final_answer": [], "visible_thinking": []}
    for r in raw:
        m, s = r["model_metadata"]["response"], by_key[r["request_key"]]
        tag = f"{r['item_id']}/{r['analysis_condition']}"
        if s["numeric_parse_failure"]:
            flags["parse_failed"].append(tag)
        if m.get("finish_reason") == "length" or (m.get("new_tokens") or 0) >= 768:
            flags["hit_768"].append(tag)                 # recorded only; never regenerated
        if re.search(r"final_answer\s*:", r["raw_response"], re.I) is None:
            flags["lacks_final_answer"].append(tag)
        if "<think>" in r["raw_response"] or "</think>" in r["raw_response"] or m.get("think_tag_in_output"):
            flags["visible_thinking"].append(tag)
    ts = sorted(datetime.fromisoformat(r["timestamp"]) for r in raw)
    diag = {
        "LABEL": "COMPLETENESS / INTEGRITY CHECK ONLY - no repair rates, CCA, comparisons or tests",
        "session": "fresh Kaggle runtime (bundle v5, Save & Run All design); frozen protocol unchanged",
        "requested": N_TOTAL, "completed": len(raw),
        "completed_keys_match_plan_in_order": [r["request_key"] for r in raw]
                                              == [x["request_key"] for x in ctx["selected"]],
        "duplicate_keys": len(raw) - len({r["request_key"] for r in raw}),
        "transport_failures": len(tf.read_text(encoding="utf-8").splitlines()) if tf.exists() else 0,
        "runtime_failures_recorded": sorted(p.name for p in RUN_DIR.glob("runtime_failure_*.json")),
        "retries": sum(r["attempt_count"] - 1 for r in raw),
        "numeric_parse_ok": sum(not s["numeric_parse_failure"] for s in scored),
        "numeric_parse_failures": len(flags["parse_failed"]),
        "strict_format_failures": sum(s["strict_format_failure"] for s in scored),
        "hit_max_new_tokens": len(flags["hit_768"]),
        "think_tag_in_any_output": bool(flags["visible_thinking"]),
        "generated_tokens_mean": round(statistics.mean(toks), 1), "generated_tokens_max": max(toks),
        "latency_mean_s": round(statistics.mean(lat), 2), "latency_median_s": round(statistics.median(lat), 2),
        "generation_time_total_min": round(sum(lat) / 60, 1),
        "wall_clock_first_to_last_row_min": round((ts[-1] - ts[0]).total_seconds() / 60, 1),
        "cell8_wall_clock_min": ctx.get("cell8_minutes"), "gpu_peak_memory_gb": ctx.get("gpu_peak_memory_gb"),
        "condition_counts_completed": {c: sum(r["analysis_condition"] == c for r in raw) for c in EXPECTED},
        "prompts_byte_identical_to_frozen": all(r["prompt"] == frozen[r["request_key"]] for r in raw),
        "all_rows_recorded_max_new_tokens_768": all(r["model_metadata"]["max_new_tokens"] == 768 for r in raw),
        "all_rows_recorded_enable_thinking_false": all(r["model_metadata"]["enable_thinking"] is False for r in raw),
        "checkpoints_present": sorted(p.name for p in CKPT_DIR.glob("checkpoint_*.zip")),
        "preflight_checks": ctx["pf"],
        "flagged": flags,
        "provenance": {k: v for k, v in ctx["prov"].items() if k != "bundle_files_sha256"},
    }
    diag["complete_and_consistent"] = (
        diag["completed"] == N_TOTAL and diag["completed_keys_match_plan_in_order"] and diag["duplicate_keys"] == 0
        and diag["transport_failures"] == 0 and diag["condition_counts_completed"] == EXPECTED
        and diag["prompts_byte_identical_to_frozen"] and diag["all_rows_recorded_max_new_tokens_768"]
        and diag["all_rows_recorded_enable_thinking_false"] and all(ctx["pf"].values()))
    kr.save(RUN_DIR / "full_run_diagnostics.json", diag)
    kr.save(RUN_DIR / "environment.json", kr.environment_record(ctx["gpu"], ctx["versions"]))
    missing = [f for f in REQUIRED_RUN_FILES if not (RUN_DIR / f).exists()]
    print("=" * 78)
    print("REQUESTS COMPLETED    ", diag["completed"], "/", N_TOTAL, diag["condition_counts_completed"])
    print("ORDER / DUPLICATES    ", diag["completed_keys_match_plan_in_order"], "/", diag["duplicate_keys"])
    print("FAILURES / RETRIES    ", "transport", diag["transport_failures"], "| runtime",
          diag["runtime_failures_recorded"] or "none", "| retries", diag["retries"])
    print("PARSE (integrity)     ", f"numeric ok {diag['numeric_parse_ok']} / failed {diag['numeric_parse_failures']}")
    print("TRUNCATED AT 768      ", diag["hit_max_new_tokens"], "| visible thinking:", diag["think_tag_in_any_output"])
    print("LATENCY               ", f"mean {diag['latency_mean_s']} s | wall {diag['wall_clock_first_to_last_row_min']} min")
    print("CHECKPOINTS           ", diag["checkpoints_present"])
    print("REQUIRED FILES        ", "all present" if not missing else f"MISSING {missing}")
    print("COMPLETE & CONSISTENT ", "YES" if diag["complete_and_consistent"] else "NO")
    print("=" * 78)
    if missing:
        stop(f"required run files missing: {missing}")
    done(10)
    return diag


# ============================================================ CELL 11
def cell11_final_zip():
    enter(11)
    files = [p for p in sorted(kr.RESULTS.rglob("*")) if p.is_file() and p.name != "run.lock"]
    members = [(p.relative_to(WORKING).as_posix(), p.read_bytes()) for p in files]   # results/full_768/...
    index = {"created": kr.now(), "n_files": len(members), "members_sha256": {a: _sha_bytes(b) for a, b in members}}
    members.append(("results/FINAL_ZIP_INDEX.json", json.dumps(index, indent=2).encode()))
    _atomic_zip(FINAL_ZIP, members)
    res = verify_final_zip(len(members))
    if not res["verified"]:
        stop("final ZIP verification failed; results/ and checkpoints/ are still in the output")
    done(11)
    print("\n" + "=" * 50)
    print("RUN COMPLETE")
    print("DOWNLOAD FROM SAVED VERSION OUTPUT:")
    print("results_full_768_FINAL.zip")
    print("=" * 50)
    return res


def verify_final_zip(expected_members=None):
    checks = {"exists": FINAL_ZIP.exists()}
    if checks["exists"]:
        with zipfile.ZipFile(FINAL_ZIP) as z:
            names = set(z.namelist())
            checks["crc_ok"] = z.testzip() is None
            idx = json.loads(z.read("results/FINAL_ZIP_INDEX.json"))
            checks["member_hashes_match"] = all(_sha_bytes(z.read(a)) == h for a, h in idx["members_sha256"].items())
            raw = z.read("results/full_768/raw_outputs.jsonl").decode("utf-8").splitlines()
            keys = [json.loads(l)["request_key"] for l in raw if l.strip()]
            checks["raw_outputs_225_unique_requests"] = len(keys) == len(set(keys)) == N_TOTAL
            checks["required_run_files_present"] = all(f"results/full_768/{f}" in names for f in REQUIRED_RUN_FILES)
            checks["config_and_provenance_present"] = all(f"results/full_768/{f}" in names
                                                          for f in ("config.json", "provenance.json", "environment.json"))
            if expected_members is not None:
                checks["member_count"] = len(names) == expected_members
        size = FINAL_ZIP.stat().st_size
        checks["non_zero_size"] = size > 0
        digest = kr.sha256_file(FINAL_ZIP)
        (WORKING / "results_full_768_FINAL.zip.sha256.txt").write_text(
            f"{digest}  results_full_768_FINAL.zip\n{size} bytes\n", encoding="utf-8")
    else:
        size, digest = 0, None
    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print("\nFINAL ZIP:\n" + str(FINAL_ZIP))
    print("\nSIZE:\n" + f"{size / 1e6:.3f} MB ({size} bytes)")
    print("\nSHA256:\n" + str(digest))
    return {"path": str(FINAL_ZIP), "bytes": size, "sha256": digest, "checks": checks, "verified": ok}
