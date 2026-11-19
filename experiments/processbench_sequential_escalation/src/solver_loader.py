"""Solver construction. The real solver is the FROZEN pilot protocol (information_placebo_pilot/configs/
qwen3_4b_kaggle.json, sha256 3118f86d...) through the frozen pilot backends.HFTransformersBackend:
Qwen/Qwen3-4B @ 3101254bbe4169895668a0e7653c3fd1f313576e, FP16, cuda:0, greedy (do_sample=False, num_beams=1),
enable_thinking=False, one user message, no system prompt, no history, batch size 1. The backend verifies the Hub
revision BEFORE loading and checks every rendered request equals prefix + frozen prompt + suffix.
Stage-B overrides (configs/stage_b.json -> solver.stage_b_overrides) are applied to a COPY of the frozen config
and recorded in every run manifest; only max_new_tokens may be overridden.
The solver identity is cross-checked against FROZEN Stage A (read-only) before anything is loaded.

LOCAL SAFETY: the real solver is refused unless CUDA is available AND (running on Kaggle OR
STAGE_B_ALLOW_MODEL_LOAD=1). The check runs before anything that could download weights.
"""
import json
import os

from . import common as C
from . import frozen_sources as fs

CANARY_PROMPT = "What is 2 + 2?\nReturn the final answer on the last line exactly as:\nFINAL_ANSWER: <answer>"
ALLOWED_OVERRIDES = {"max_new_tokens"}


def solver_config(cfg):
    p = fs.pilot_path(cfg["solver"]["frozen_config"], cfg)
    sha = C.sha256_file(p)
    if sha != cfg["solver"]["frozen_config_sha256"]:
        raise SystemExit(f"STOP: solver config sha256 {sha} != frozen {cfg['solver']['frozen_config_sha256']}")
    sc = json.loads(p.read_text(encoding="utf-8"))
    if sc["model"] != cfg["solver"]["model"] or sc["model_revision"] != cfg["solver"]["model_revision"]:
        raise SystemExit("STOP: frozen solver config disagrees with configs/stage_b.json (model / revision)")
    fs.check_solver_matches_stage_a(cfg)
    ov = cfg["solver"].get("stage_b_overrides") or {}
    bad = set(ov) - ALLOWED_OVERRIDES
    if bad:
        raise SystemExit(f"STOP: only {sorted(ALLOWED_OVERRIDES)} may be overridden in Stage B, not {sorted(bad)}")
    eff = {**sc, **ov}
    return eff, sha, {k: {"frozen": sc.get(k), "stage_b": v} for k, v in ov.items()}


def guard_real_model():
    if C.env_flag("STAGE_B_TEST_SUITE"):        # harness correction H2: tests never load the real model, on any host
        raise SystemExit("STOP: the real Qwen solver is never loaded from inside the test suite (STAGE_B_TEST_SUITE=1)")
    on_kaggle = bool(os.environ.get("KAGGLE_KERNEL_RUN_TYPE")) or os.path.isdir("/kaggle/working")
    if not (on_kaggle or C.env_flag("STAGE_B_ALLOW_MODEL_LOAD")):
        raise SystemExit("STOP: the real Qwen solver only runs on the GPU machine (Kaggle). This machine is for "
                         "dataset building, validation and mock tests. Use --solver mock locally.")
    try:
        import torch
    except ImportError:
        raise SystemExit("STOP: torch is not installed; the real solver needs the Kaggle GPU environment")
    if not torch.cuda.is_available():
        raise SystemExit("STOP: CUDA is not available; the frozen protocol runs FP16 on cuda:0 (Kaggle T4)")


def make_solver(kind, cfg, requests=None, mock_profile=None):
    """-> (solver, backend_name, test_only, effective_solver_cfg, frozen_solver_cfg_sha, overrides)"""
    sc, sha, ov = solver_config(cfg)
    if kind == "mock":
        from .mock_solver import MockSolver
        return MockSolver(requests or [], sc["max_new_tokens"], mock_profile), "mock", True, sc, sha, ov
    if kind == "qwen":
        guard_real_model()
        be = fs.pilot_module("backends", cfg)
        backend = be.make_backend(sc)
        eff = backend.effective_settings()
        bad = []
        if eff.get("hub_resolved_revision") != sc["model_revision"]:
            bad.append("revision")
        if "float16" not in str(eff.get("dtype")) or eff.get("device") != "cuda:0":
            bad.append("dtype/device")
        if eff.get("enable_thinking") is not False or eff.get("max_new_tokens") != sc["max_new_tokens"]:
            bad.append("thinking/max_new_tokens")
        if eff.get("decoding") != "greedy (do_sample=False, num_beams=1)":
            bad.append("decoding")
        if bad:
            raise SystemExit(f"STOP: loaded solver differs from the frozen protocol: {bad} {eff}")
        return backend, "hf_transformers", False, sc, sha, ov
    raise SystemExit(f"unknown solver {kind!r} (mock | qwen)")


def canary(backend):
    """Non-experimental 2+2 check (stored only in diagnostics)."""
    from .parser import parse
    res = backend.generate(CANARY_PROMPT)
    p = parse(res.text)
    think = "<think>" in res.text or "</think>" in res.text or bool(res.metadata.get("think_tag_in_output"))
    out = {"DIAGNOSTIC_ONLY": True, "timestamp": C.now(), "prompt": CANARY_PROMPT, "response": res.text,
           "metadata": res.metadata, "checks": {"numeric_answer_is_4": p["numeric_parsed_answer"] == "4",
                                                "no_visible_thinking": not think}}
    out["passed"] = all(out["checks"].values())
    return out
