"""Package ONE run (Stage 1 or Stage 2: separate archives) into a verified FINAL archive + SHA-256.

    python scripts/12_package_results.py --run-dir <run dir> [--extra <dir or file> ...]
    python scripts/12_package_results.py --verify <archive.zip>
Archive <run_id>_FINAL.zip contains the whole run directory (raw_outputs.jsonl, run_manifest.json, state.json,
checkpoints, scored_outputs.jsonl, run_diagnostics.json, logs), any --extra paths (e.g. the analysis directory,
the frozen failure files), the frozen request/order files of that stage, ENVIRONMENT_MANIFEST.json (python,
platform, package versions, GPU when available, code fingerprint) and ARCHIVE_INDEX.json (sha256 of every member).
Refuses incomplete runs. Writes <archive>.sha256.txt next to it.
"""
import argparse
import json
import platform
import sys
import zipfile
from pathlib import Path

import _bootstrap  # noqa: F401
from src import common as C


def environment():
    env = {"python": sys.version, "platform": platform.platform(), "code_fingerprint": C.code_fingerprint(),
           "created": C.now()}
    try:
        from importlib import metadata
        env["packages"] = {n: _v(metadata, n) for n in ("torch", "transformers", "accelerate", "safetensors",
                                                         "tokenizers", "huggingface_hub", "numpy", "matplotlib")}
    except Exception:            # noqa: BLE001
        pass
    try:
        import torch
        if torch.cuda.is_available():
            env["gpu"] = torch.cuda.get_device_name(0)
            env["cuda"] = torch.version.cuda
    except ImportError:
        env["gpu"] = None
    return env


def _v(metadata, n):
    try:
        return metadata.version(n)
    except metadata.PackageNotFoundError:
        return None


def verify(zpath):
    zpath = Path(zpath)
    with zipfile.ZipFile(zpath) as z:
        if z.testzip() is not None:
            raise SystemExit("STOP: archive corrupt")
        idx = json.loads(z.read("ARCHIVE_INDEX.json"))
        bad = [n for n, h in idx["members_sha256"].items() if C.sha256_bytes(z.read(n)) != h]
    side = zpath.with_name(zpath.name + ".sha256.txt")
    sha = C.sha256_file(zpath)
    ok_side = side.exists() and side.read_text().split()[0] == sha
    return {"archive": str(zpath), "sha256": sha, "members": len(idx["members_sha256"]), "bad_members": bad,
            "sidecar_matches": ok_side if side.exists() else "no sidecar", "passed": not bad and ok_side is not False}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir")
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--verify")
    a = ap.parse_args()
    if a.verify:
        r = verify(a.verify)
        print(json.dumps(r, indent=1))
        return 0 if r["passed"] else 1
    rd = Path(a.run_dir)
    man = C.read_json(rd / "run_manifest.json")
    st = C.read_json(rd / "state.json")
    if st["completed"] != st["planned"] or not (rd / "checkpoints" / "checkpoint_FINAL.zip").exists():
        raise SystemExit(f"STOP: run incomplete ({st['completed']}/{st['planned']}); never package a partial run")
    members = {}
    for f in sorted(rd.rglob("*")):
        if f.is_file() and f.name != "run.lock":
            members[f"{man['run_id']}/{f.relative_to(rd).as_posix()}"] = f.read_bytes()
    for e in a.extra:
        e = Path(e)
        files = [e] if e.is_file() else sorted(p for p in e.rglob("*") if p.is_file())
        for f in files:
            members[f"extra/{e.name}/{f.relative_to(e).as_posix() if e.is_dir() else f.name}"] = f.read_bytes()
    frozen = [C.ITEMS_FILE, C.STAGE1_REQUESTS, C.STAGE1_ORDER, C.FROZEN_HASHES] if man["stage"] == 1 else \
        [C.ITEMS_FILE, C.STAGE1_FAILURES, C.STAGE1_FAILURES_LOCK, C.DATA_MAN / "ANNOTATION_REVIEW_EXPORT.json",
         C.DATA_PROC / "annotation_review" / "annotation_review_packet.md",
         C.DATA_PROC / "annotation_review" / "annotation_review_form_FILLED.csv",
         C.DATA_PROC / "annotation_review_decisions.json", C.DATA_MAN / "ANNOTATION_REVIEW_FROZEN.json",
         C.STAGE2_REQUESTS, C.STAGE2_ORDER, C.RULE_LOCK, C.FROZEN_HASHES,
         # A5 human Pass 1 + A6 Pass-2 waiver provenance
         C.STAGE2_MANIFEST, C.PROTOCOL_AMENDMENTS, C.DATA_MAN / "HUMAN_REVIEW_PASS1_FROZEN.json",
         C.DATA_MAN / "HUMAN_REVIEW_FROZEN.json", C.DATA_PROC / "human_review" / "pass1_final.json",
         C.DATA_PROC / "human_review" / "pass1_mapping.json", C.DATA_PROC / "human_review" / "human_review_judgments.json",
         C.DATA_MAN / "STAGE1_FINAL_ARCHIVE_PROVENANCE.json",
         # A7 smoke-gate clarification provenance (the smoke records themselves are never rescored)
         C.DATA_MAN / "stage2_smoke_selection.json", C.DATA_MAN / "STAGE2_SMOKE_V9_NOGO.json",
         C.DATA_MAN / "DIAG_HEADROOM_V11_RECORD.json", C.DATA_MAN / "STAGE2_SMOKE_GO.json"]
    for f in frozen:
        if f.exists() and not man["test_only"]:
            members[f"frozen_inputs/{C.rel(f)}"] = f.read_bytes()
    members["ENVIRONMENT_MANIFEST.json"] = json.dumps(environment(), indent=2, default=str).encode()
    index = {"run_id": man["run_id"], "stage": man["stage"], "TEST_ONLY": man["test_only"], "created": C.now(),
             "oracle_status": C.ORACLE_STATEMENT,
             "raw_outputs_sha256": C.sha256_file(rd / "raw_outputs.jsonl"),
             "execution_order_sha256": man["execution_order_sha256"], "requests_sha256": man["requests_sha256"],
             "members_sha256": {k: C.sha256_bytes(v) for k, v in sorted(members.items())}}
    members["ARCHIVE_INDEX.json"] = json.dumps(index, indent=2).encode()
    zpath = rd.parent / f"{man['run_id']}_FINAL.zip"
    from src.persistence import atomic_zip
    atomic_zip(zpath, sorted(members.items()))
    sha = C.sha256_file(zpath)
    Path(str(zpath) + ".sha256.txt").write_text(f"{sha}  {zpath.name}\n", encoding="utf-8")
    r = verify(zpath)
    print(json.dumps(r, indent=1))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
