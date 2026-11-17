"""VENDORED from experiments/stage_a_reliability_gating/src/persistence.py (sha256 8b8fe359a715e05a..., recorded in
configs/stage_b.json -> stage_a_link.vendored_from_stage_a). Logic unchanged; Stage A is not imported at run time.
Crash-safe persistence for model runs.

Source of truth: <run_dir>/raw_outputs.jsonl. Each completed request is appended as ONE line, flushed and fsync'ed
before the runner moves on, so a crash loses at most the request in flight. Everything else is derived from it:
  state.json                completion state (count, last key); rewritten atomically (tmp + fsync + os.replace)
  transport_failures.jsonl  technical failures that produced no model response (these may be retried)
  checkpoints/checkpoint_NNNN.zip   every `checkpoint_every` completed requests: exactly the first N raw rows plus
                            the run manifest and state; written to *.tmp, fsync'ed, then atomically renamed
  run.lock                  prevents two runners writing the same run directory
A torn final line (crash mid-write) is cut off and reported on the next read; a malformed line anywhere else stops.
"""
import json
import os
import time
import zipfile
from pathlib import Path

from .common import now, sha256_bytes


class DuplicateRequestError(RuntimeError):
    pass


def fsync_dir(d):
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass          # not supported on this OS / filesystem (e.g. Windows directories)


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def replace_with_retry(tmp, dest, attempts=8, wait=0.25):
    """os.replace, retried: on Windows a sync client (OneDrive) or virus scanner can hold the target briefly."""
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait * (i + 1))


def atomic_write_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp, path)
    fsync_dir(path.parent)


def atomic_zip(dest, members):
    """members: [(arcname, bytes)]"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, data in members:
                z.writestr(arc, data)
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp, dest)
    fsync_dir(dest.parent)
    return dest


def read_jsonl_tolerant(path, repair=True):
    """Rows of a JSONL file written by append_jsonl. A torn FINAL line is removed (and reported)."""
    path = Path(path)
    if not path.exists():
        return [], None
    data = path.read_bytes()
    lines = data.split(b"\n")
    rows, good = [], 0
    for i, line in enumerate(lines):
        if not line:
            good += 1 if i < len(lines) - 1 else 0
            continue
        try:
            rows.append(json.loads(line.decode("utf-8")))
            good += len(line) + 1
        except (json.JSONDecodeError, UnicodeDecodeError):
            if i == len(lines) - 1 or all(not x for x in lines[i + 1:]):
                if repair:
                    with open(path, "r+b") as fh:
                        fh.truncate(good)
                return rows, f"torn final line ({len(line)} bytes) removed from {path.name}"
            raise SystemExit(f"STOP: {path} has a malformed line {i + 1} that is not the final line")
    return rows, None


class RunStore:
    def __init__(self, run_dir, checkpoint_every=10):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.raw = self.dir / "raw_outputs.jsonl"
        self.state = self.dir / "state.json"
        self.failures = self.dir / "transport_failures.jsonl"
        self.ckpt_dir = self.dir / "checkpoints"
        self.lock = self.dir / "run.lock"
        self.checkpoint_every = checkpoint_every
        self.note = None

    # ---------------- lock
    def acquire(self, force=False):
        if self.lock.exists() and not force:
            raise SystemExit(f"STOP: {self.lock} exists: another runner may be writing this run (or a previous one "
                             "was killed). If you are sure no runner is active, rerun with --force-unlock.")
        self.lock.write_text(json.dumps({"pid": os.getpid(), "since": now()}), encoding="utf-8")

    def release(self):
        if self.lock.exists():
            self.lock.unlink()

    # ---------------- completion state
    def completed_rows(self):
        rows, note = read_jsonl_tolerant(self.raw)
        if note:
            self.note = note
        keys = [r["request_key"] for r in rows]
        if len(keys) != len(set(keys)):
            dup = sorted({k for k in keys if keys.count(k) > 1})
            raise DuplicateRequestError(f"duplicate request keys in raw_outputs.jsonl: {dup[:5]}")
        return rows

    def completed_keys(self):
        return {r["request_key"] for r in self.completed_rows()}

    def write_state(self, n_completed, n_planned, last_key):
        atomic_write_text(self.state, json.dumps({"updated": now(), "completed": n_completed,
                                                  "planned": n_planned, "last_request_key": last_key,
                                                  "source_of_truth": "raw_outputs.jsonl"}, indent=2) + "\n")

    def append(self, row, done_keys):
        if row["request_key"] in done_keys:
            raise DuplicateRequestError(f"refusing to write {row['request_key']} twice")
        append_jsonl(self.raw, row)
        done_keys.add(row["request_key"])

    def transport_failure(self, rec):
        append_jsonl(self.failures, rec)

    # ---------------- checkpoints
    def _complete_lines(self):
        if not self.raw.exists():
            return []
        data = self.raw.read_bytes()
        return [l + b"\n" for l in data[: data.rfind(b"\n") + 1].split(b"\n")[:-1] if l]

    def checkpoint(self, k, extra_files=(), name=None):
        lines = self._complete_lines()
        if len(lines) < k:
            raise ValueError(f"only {len(lines)} rows on disk, cannot checkpoint {k}")
        members = [("raw_outputs.jsonl", b"".join(lines[:k]))]
        for p in [self.dir / "run_manifest.json", self.state, self.failures, *extra_files]:
            p = Path(p)
            if p.exists():
                members.append((p.name, p.read_bytes()))
        index = {"created": now(), "rows": k, "members_sha256": {a: sha256_bytes(b) for a, b in members}}
        members.append(("CHECKPOINT_INDEX.json", json.dumps(index, indent=2).encode()))
        dest = atomic_zip(self.ckpt_dir / (name or f"checkpoint_{k:04d}.zip"), members)
        with open(self.ckpt_dir / "checkpoint_log.txt", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{now()}  {dest.name}  rows={k}  sha256={sha256_bytes(dest.read_bytes())}\n")
            fh.flush()
            os.fsync(fh.fileno())
        return dest

    def checkpoints_due(self, n_done):
        out = []
        for k in range(self.checkpoint_every, n_done + 1, self.checkpoint_every):
            if not (self.ckpt_dir / f"checkpoint_{k:04d}.zip").exists():
                out.append(k)
        return out
