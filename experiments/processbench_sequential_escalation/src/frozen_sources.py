"""Read-only, hash-verified access to (a) the frozen pilot code/data and (b) two frozen Stage-A files.

Pilot: Stage B reuses the pilot's frozen numeric/strict answer parser (scripts/parse_answers.py, "2.0 (strict +
numeric)") and its Hugging Face backend (scripts/backends.py), plus the frozen solver config. Every file is checked
against configs/stage_b.json BEFORE it is imported or read; bytecode writing is disabled while importing so nothing
is ever written into the pilot folder.
Stage A: only configs/stage_a.json and data/manifests/FROZEN_HASHES.json are READ, to confirm that Stage B uses the
same solver identity. No Stage-A module is imported.

Locations:  local  <repo>/information_placebo_pilot   |  Kaggle  <bundle>/pilot_frozen  (STAGE_B_PILOT_ROOT)
            local  <repo>/experiments/stage_a_...      |  Kaggle  <bundle>/stage_a_frozen (STAGE_B_STAGE_A_ROOT)
"""
import importlib
import json
import os
import sys
from pathlib import Path

from .common import REPO_ROOT, load_config, sha256_file


class FrozenLinkError(RuntimeError):
    pass


def pilot_root(cfg=None):
    env = os.environ.get("STAGE_B_PILOT_ROOT")
    if env:
        return Path(env)
    cfg = cfg or load_config()
    return REPO_ROOT / cfg["pilot_link"]["pilot_root_relative_to_repo"]


def stage_a_root(cfg=None):
    env = os.environ.get("STAGE_B_STAGE_A_ROOT")
    if env:
        return Path(env)
    cfg = cfg or load_config()
    return REPO_ROOT / cfg["stage_a_link"]["stage_a_root_relative_to_repo"]


def _verify(root, relpath, table):
    expected = table.get(relpath)
    if expected is None:
        raise FrozenLinkError(f"{relpath} is not a registered frozen file")
    p = Path(root) / relpath
    if not p.exists():
        raise FrozenLinkError(f"frozen file missing: {p}")
    got = sha256_file(p)
    if got != expected:
        raise FrozenLinkError(f"frozen file changed: {relpath} sha256 {got} != recorded {expected}")
    return p


def pilot_path(relpath, cfg=None):
    cfg = cfg or load_config()
    return _verify(pilot_root(cfg), relpath, cfg["pilot_link"]["files_sha256"])


def stage_a_path(relpath, cfg=None):
    cfg = cfg or load_config()
    return _verify(stage_a_root(cfg), relpath, cfg["stage_a_link"]["files_sha256"])


def pilot_module(name, cfg=None):
    """Import a frozen pilot script (parse_answers | backends | kaggle_runner) after verifying it."""
    cfg = cfg or load_config()
    if name not in ("parse_answers", "backends", "kaggle_runner"):
        raise FrozenLinkError(f"pilot module {name!r} is not used by Stage B")
    pilot_path(f"scripts/{name}.py", cfg)
    scripts = str(pilot_root(cfg) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = old


def stage_a_solver_identity(cfg=None):
    """Model / revision / frozen solver-config sha recorded by FROZEN Stage A (read-only)."""
    cfg = cfg or load_config()
    sa = json.loads(stage_a_path("configs/stage_a.json", cfg).read_text(encoding="utf-8"))
    stage_a_path("data/manifests/FROZEN_HASHES.json", cfg)
    return {"model": sa["solver"]["model"], "model_revision": sa["solver"]["model_revision"],
            "frozen_config_sha256": sa["solver"]["frozen_config_sha256"],
            "solver_tag": sa["requests"]["solver_tag"]}


def check_solver_matches_stage_a(cfg=None):
    cfg = cfg or load_config()
    sa = stage_a_solver_identity(cfg)
    sb = cfg["solver"]
    bad = [k for k in ("model", "model_revision", "frozen_config_sha256", "solver_tag") if sa[k] != sb[k]]
    if bad:
        raise SystemExit(f"STOP: Stage-B solver identity differs from frozen Stage A in {bad}: {sa} vs {sb}")
    return sa
