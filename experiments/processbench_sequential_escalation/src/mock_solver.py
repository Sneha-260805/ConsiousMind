"""Deterministic TEST-ONLY solver (no model). Every response carries the mock banner; every run that uses it is
marked test_only and written under outputs/test_runs/. Outcomes are a hash of the prompt, so they are reproducible
and exercise every scoring path (repair, original retention, Stage-1 retention, new wrong answer, parse failure,
truncation). Nothing produced here is a research result.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from . import common as C

MOCK_BANNER = "[TEST ONLY - MOCK SOLVER - NOT A MODEL OUTPUT]"
DEFAULT_PROFILE = {"repair": 0.55, "original": 0.15, "stage1": 0.08, "new": 0.12, "parse": 0.05, "truncate": 0.05}


class MockTransportError(Exception):
    pass


@dataclass
class MockResult:
    text: str
    metadata: dict = field(default_factory=dict)


class MockSolver:
    name = "mock"

    def __init__(self, requests, max_new_tokens, profile=None, fail_transport_keys=()):
        self.by_sha = {r["prompt_sha256"]: r for r in requests}
        self.max_new_tokens = max_new_tokens
        self.profile = profile or DEFAULT_PROFILE
        self.fail_transport = dict.fromkeys(fail_transport_keys, 1)   # key -> remaining transport failures
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        sha = C.sha256_text(prompt)
        req = self.by_sha.get(sha)
        if req and self.fail_transport.get(req["request_key"], 0) > 0:
            self.fail_transport[req["request_key"]] -= 1
            from . import frozen_sources as fs
            raise fs.pilot_module("backends").TransportError("mock transport failure (TEST ONLY)")
        u = int(sha[:8], 16) / 0xFFFFFFFF
        cut, kind = 0.0, "new"
        for k in ("repair", "original", "stage1", "new", "parse", "truncate"):
            cut += self.profile.get(k, 0)
            if u < cut:
                kind = k
                break
        gold = req["gold_answer"] if req else "0"
        orig = req["original_answer"] if req else "1"
        s1 = (req or {}).get("stage1_numeric_answer") or str(Decimal(orig) + 7)
        ans = {"repair": gold, "original": orig, "stage1": s1, "new": str(Decimal(gold) + 13)}.get(kind)
        body = f"{MOCK_BANNER}\nmock reasoning for {sha[:10]}"
        if kind == "parse":
            return MockResult(body + "\nI am not sure.", {"finish_reason": "stop", "new_tokens": 12,
                                                         "prompt_tokens": len(prompt) // 4})
        if kind == "truncate":
            return MockResult(body + " ... (cut)", {"finish_reason": "length", "new_tokens": self.max_new_tokens,
                                                   "prompt_tokens": len(prompt) // 4})
        return MockResult(f"{body}\nFINAL_ANSWER: {ans}", {"finish_reason": "stop", "new_tokens": 40 + int(u * 200),
                                                           "prompt_tokens": len(prompt) // 4})

    def effective_settings(self):
        return {"backend": "mock", "TEST_ONLY": True, "max_new_tokens": self.max_new_tokens}
