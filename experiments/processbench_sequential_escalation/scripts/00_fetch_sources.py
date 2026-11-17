"""Fetch (or verify) the raw Stage-B sources. Network access only; nothing is executed from downloaded files.

    python scripts/00_fetch_sources.py            # verify existing files; download any that are missing
    python scripts/00_fetch_sources.py --verify   # verify only (no network)

  ProcessBench    Qwen/ProcessBench @ pinned revision -> data/raw/processbench_3bdcd537/  (sha256 in config)
  MATH-500        HuggingFaceH4/MATH-500 @ pinned revision -> data/raw/math500_6e4ed1a2/test.jsonl
  Hendrycks MATH  EleutherAI/hendrycks_math test split, 7 subject configs, via the HF datasets-server ROWS API (JSON)
                  -> data/raw/hendrycks_math_21a56338/test_rows_api.jsonl. Parquet files of the pinned revision
                  are also stored (provenance only: parquet cannot be loaded on this machine).
The GSM8K test split is the pilot's frozen copy (information_placebo_pilot/data/raw/test.jsonl, sha-verified).
"""
import argparse
import json
import sys
import time
import urllib.request

import _bootstrap  # noqa: F401
from src import common as C

MATH_CONFIGS = ["algebra", "counting_and_probability", "geometry", "intermediate_algebra", "number_theory",
                "prealgebra", "precalculus"]


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "stage-b-fetch"}),
                                        timeout=120) as r:
                return r.read()
        except Exception as e:                       # noqa: BLE001 - network: retry then stop
            if i == tries - 1:
                raise SystemExit(f"STOP: download failed {url}: {e}")
            time.sleep(3 * (i + 1))


def fetch_processbench(cfg, verify_only):
    s = cfg["sources"]["processbench"]
    d = C.STAGE_ROOT / s["local_dir"]
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, sha in s["files_sha256"].items():
        p = d / name
        if not p.exists():
            if verify_only:
                raise SystemExit(f"STOP: missing {p}")
            p.write_bytes(get(f"https://huggingface.co/datasets/Qwen/ProcessBench/resolve/{s['revision']}/{name}"))
        got = C.sha256_file(p)
        if got != sha:
            raise SystemExit(f"STOP: {p} sha256 {got} != pinned {sha}")
        out[name] = got
    return out


def fetch_math500(cfg, verify_only):
    s = cfg["sources"]["math_reference"]
    p = C.STAGE_ROOT / s["math500_file"]
    if not p.exists():
        if verify_only:
            raise SystemExit(f"STOP: missing {p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(get(f"https://huggingface.co/datasets/{s['math500_repo']}/resolve/{s['math500_revision']}/test.jsonl"))
    got = C.sha256_file(p)
    if got != s["math500_sha256"]:
        raise SystemExit(f"STOP: MATH-500 sha256 {got} != pinned {s['math500_sha256']}")
    return got


def fetch_math_rows(cfg, verify_only):
    s = cfg["sources"]["math_reference"]
    p = C.STAGE_ROOT / s["rows_dump"]
    if p.exists():
        got = C.sha256_file(p)
        if s["rows_dump_sha256"] and got != s["rows_dump_sha256"]:
            raise SystemExit(f"STOP: MATH rows dump sha256 {got} != frozen {s['rows_dump_sha256']}")
        return got, "existing"
    if verify_only:
        raise SystemExit(f"STOP: missing {p}")
    rows = []
    for conf in MATH_CONFIGS:
        off, total = 0, None
        while total is None or off < total:
            url = (f"https://datasets-server.huggingface.co/rows?dataset=EleutherAI/hendrycks_math&config={conf}"
                   f"&split=test&offset={off}&length=100")
            page = json.loads(get(url))
            total = page["num_rows_total"]
            for r in page["rows"]:
                if r.get("truncated_cells"):
                    raise SystemExit(f"STOP: rows API truncated cells in {conf} row {r['row_idx']}")
                rows.append({"subject_config": conf, "row_idx": r["row_idx"], **r["row"]})
            off += 100
        # pinned-revision parquet kept for provenance
        pq = p.parent / conf / "test-00000-of-00001.parquet"
        if not pq.exists():
            pq.parent.mkdir(parents=True, exist_ok=True)
            pq.write_bytes(get(f"https://huggingface.co/datasets/{s['repo']}/resolve/{s['revision_at_fetch']}/"
                               f"{conf}/test-00000-of-00001.parquet"))
    C.write_jsonl(p, rows)
    return C.sha256_file(p), f"fetched {len(rows)} rows"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    from src import frozen_sources as fs
    res = {"processbench": fetch_processbench(cfg, a.verify), "math500": fetch_math500(cfg, a.verify),
           "math_rows": fetch_math_rows(cfg, a.verify),
           "gsm8k_test_pilot_copy": C.sha256_file(fs.pilot_path(cfg["sources"]["gsm8k_reference"]["pilot_file"], cfg))}
    pq = {}
    for conf in MATH_CONFIGS:
        f = C.STAGE_ROOT / cfg["sources"]["math_reference"]["parquet_dir"] / conf / "test-00000-of-00001.parquet"
        pq[conf] = C.sha256_file(f) if f.exists() else None
    res["math_parquet_provenance_sha256"] = pq
    print(json.dumps(res, indent=2))
    if not cfg["sources"]["math_reference"]["rows_dump_sha256"]:
        print("NOTE: record math_rows sha256 in configs/stage_b.json -> sources.math_reference.rows_dump_sha256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
