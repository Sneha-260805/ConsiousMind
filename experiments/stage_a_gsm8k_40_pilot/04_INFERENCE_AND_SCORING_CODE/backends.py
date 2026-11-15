"""Model backends for the pilot harness.

Every backend implements one stateless call:

    generate(prompt: str) -> GenerationResult(text, metadata)

It receives ONLY the prompt string: no item id, condition, gold answer or history. Each call is a
fresh single-user-message request, so no condition can see another condition's response.

Errors:
  TransportError      timeout, connection failure, rate limit, 5xx: the harness may retry the SAME prompt
  BackendConfigError  bad request / auth / missing model: the run stops (retrying cannot help)
Anything the model actually returns (wrong, empty, refusal, nonsense) is a valid outcome, never an error.

Backends:
  mock               deterministic TEST ONLY backend (no model)
  openai_compatible  POST {base_url}/chat/completions (vLLM, llama.cpp server, LM Studio, OpenAI, ...)
  ollama             POST {base_url}/api/chat
  hf_transformers    local Hugging Face model (imports transformers/torch lazily)
The HTTP adapters use only the standard library.
"""
import hashlib
import json
import os
import platform
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path

MOCK_BANNER = "[TEST ONLY - MOCK BACKEND - NOT A MODEL OUTPUT]"


class TransportError(Exception):
    """Retryable: the request did not produce a model response."""


class BackendConfigError(Exception):
    """Not retryable: the request itself is wrong (auth, unknown model, bad parameters)."""


@dataclass
class GenerationResult:
    text: str
    metadata: dict = field(default_factory=dict)


def package_versions(names):
    out = {}
    for n in names:
        try:
            out[n] = importlib_metadata.version(n)
        except importlib_metadata.PackageNotFoundError:
            out[n] = None
    return out


class Backend:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def generate(self, prompt):
        raise NotImplementedError

    def effective_settings(self):
        """Settings actually applied, plus anything the backend ignores or leaves at a server default."""
        raise NotImplementedError

    def versions(self):
        return {}


# ------------------------------------------------------------------ mock (TEST ONLY)
class SimulatedCrash(Exception):
    """Raised by the mock to imitate the process dying mid-run (used by the resume tests)."""


class MockBackend(Backend):
    """Deterministic, TEST ONLY. Responses come from a script {prompt_sha256: text} supplied by the test,
    or else from a fixed rule keyed on the prompt hash. Fault injection:
      transport_failures {prompt_sha256: n}  raise TransportError on the first n attempts
      crash_after_calls  n                   raise SimulatedCrash on call n+1
    """
    name = "mock"

    def __init__(self, cfg, script=None, transport_failures=None, crash_after_calls=None):
        super().__init__(cfg)
        self.script = script or {}
        self.transport_failures = dict(transport_failures or {})
        self.crash_after_calls = crash_after_calls
        self.calls = []            # every prompt received, in order (lets tests prove statelessness)
        self.successful_calls = 0

    def generate(self, prompt):
        if not isinstance(prompt, str):
            raise TypeError("backends accept only the prompt string")
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.calls.append(prompt)
        if self.crash_after_calls is not None and self.successful_calls >= self.crash_after_calls:
            raise SimulatedCrash(f"simulated crash after {self.successful_calls} calls")
        if self.transport_failures.get(h, 0) > 0:
            self.transport_failures[h] -= 1
            raise TransportError("simulated transport failure (mock)")
        self.successful_calls += 1
        if h in self.script:
            body = self.script[h]
        else:
            body = ["Mock reasoning.\nFINAL_ANSWER: 123", "FINAL_ANSWER: $1,230", "No answer field here."][int(h, 16) % 3]
        return GenerationResult(text=f"{MOCK_BANNER}\n{body}",
                                metadata={"mock": True, "prompt_sha256": h, "finish_reason": "stop"})

    def effective_settings(self):
        return {"backend": "mock", "TEST_ONLY": True, "temperature": None, "top_p": None, "top_k": None,
                "max_new_tokens": None, "seed": None, "ignored_parameters": "all (no model)"}


# ------------------------------------------------------------------ shared HTTP transport
def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        if e.code == 429 or e.code >= 500 or e.code == 408:
            raise TransportError(f"HTTP {e.code}: {detail}") from e
        raise BackendConfigError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise TransportError(f"{type(e).__name__}: {e}") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise TransportError(f"non-JSON response body: {raw[:200]!r}") from e


class OpenAICompatibleBackend(Backend):
    """Chat Completions API. One user message, no system prompt. Sends temperature, max_tokens and
    seed; top_p/top_k are NOT sent (server defaults; irrelevant at temperature 0 on most servers)."""
    name = "openai_compatible"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.url = cfg["base_url"].rstrip("/") + "/chat/completions"
        key_env = cfg.get("api_key_env") or ""
        self.headers = {"Authorization": f"Bearer {os.environ[key_env]}"} if key_env and os.environ.get(key_env) else {}
        self.timeout = cfg.get("timeout_seconds", 120)

    def payload(self, prompt):
        p = {"model": self.cfg["model"], "messages": [{"role": "user", "content": prompt}],
             "temperature": self.cfg["temperature"], "max_tokens": self.cfg["max_new_tokens"], "stream": False}
        if self.cfg.get("seed") is not None:
            p["seed"] = self.cfg["seed"]
        return p

    def generate(self, prompt):
        data = _post_json(self.url, self.payload(prompt), self.headers, self.timeout)
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise TransportError(f"response has no choices: {str(data)[:200]}") from e
        text = (choice.get("message") or {}).get("content") or ""
        return GenerationResult(text=text, metadata={
            "finish_reason": choice.get("finish_reason"), "returned_model": data.get("model"),
            "system_fingerprint": data.get("system_fingerprint"), "usage": data.get("usage")})

    def effective_settings(self):
        return {"backend": self.name, "endpoint": self.url, "model": self.cfg["model"],
                "temperature": self.cfg["temperature"], "max_new_tokens": self.cfg["max_new_tokens"],
                "seed": self.cfg.get("seed"), "top_p": "server default (not sent)",
                "top_k": "server default (not sent)", "system_prompt": None,
                "message_format": "single user message",
                "ignored_parameters": "unknown: an OpenAI-compatible server may ignore 'seed'; check "
                                      "system_fingerprint/returned_model in each raw row"}


class OllamaBackend(Backend):
    """Ollama /api/chat, one user message, stream=false. temperature/num_predict/seed in options."""
    name = "ollama"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.url = cfg.get("base_url", "http://localhost:11434").rstrip("/") + "/api/chat"
        self.timeout = cfg.get("timeout_seconds", 300)

    def payload(self, prompt):
        opts = {"temperature": self.cfg["temperature"], "num_predict": self.cfg["max_new_tokens"]}
        if self.cfg.get("seed") is not None:
            opts["seed"] = self.cfg["seed"]
        return {"model": self.cfg["model"], "messages": [{"role": "user", "content": prompt}],
                "stream": False, "options": opts}

    def generate(self, prompt):
        data = _post_json(self.url, self.payload(prompt), {}, self.timeout)
        if "error" in data:
            raise BackendConfigError(f"ollama error: {data['error']}")
        text = (data.get("message") or {}).get("content") or ""
        return GenerationResult(text=text, metadata={
            "finish_reason": data.get("done_reason"), "returned_model": data.get("model"),
            "eval_count": data.get("eval_count"), "prompt_eval_count": data.get("prompt_eval_count")})

    def effective_settings(self):
        return {"backend": self.name, "endpoint": self.url, "model": self.cfg["model"],
                "temperature": self.cfg["temperature"], "max_new_tokens": self.cfg["max_new_tokens"],
                "seed": self.cfg.get("seed"), "top_p": "model default (not sent)",
                "top_k": "model default (not sent)", "system_prompt": "none sent (a Modelfile SYSTEM may still apply)",
                "message_format": "single user message",
                "quantization": "determined by the pulled model tag; record it in model_revision"}

    def versions(self):
        return {"ollama_server": "query GET /api/version before a real run"}


def free_ram_gb():
    """Currently available physical memory in GB (Windows / POSIX), or None if unknown."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullAvailPhys / 1e9
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except Exception:
        return None


def verify_pinned_revision(repo_id, revision, api=None):
    """Provenance check BEFORE loading: the Hub must resolve the pinned revision to exactly that commit.
    (Transformers' config._commit_hash is not a reliable provenance indicator after loading.)"""
    if api is None:
        from huggingface_hub import HfApi
        api = HfApi()
    sha = api.model_info(repo_id, revision=revision).sha
    if sha != revision:
        raise BackendConfigError(f"Hub resolved {repo_id}@{revision} to {sha}, not the pinned commit")
    return sha


def pinned_snapshot(repo_id, revision, downloader=None):
    """Local snapshot directory of the pinned commit (re-uses the Hub cache; never forces a re-download).
    The Hub cache stores it under .../snapshots/<commit>, which must equal the pinned revision."""
    if downloader is None:
        from huggingface_hub import snapshot_download as downloader
    path = Path(downloader(repo_id=repo_id, revision=revision))
    if path.name != revision or path.parent.name != "snapshots":
        raise BackendConfigError(f"snapshot directory {path} does not correspond to pinned commit {revision}")
    return path


TEMPLATE_PROBES = ["Probe A: 12 + 30 = 42?\nStep 1: x", "Probe B (different) -- $1,920 / Step 7: y"]


def check_chat_template(render, enable_thinking):
    """Verify, from rendered text only, that the chat template wraps the prompt in a CONSTANT wrapper:
      * the prompt appears verbatim exactly once (nothing inside it is changed)
      * the text before/after it is identical for two different prompts (no content-dependent injection)
      * no system message and no '/think' or '/no_think' switch text is added
      * the enable_thinking switch is honoured (rendering differs between True and False)
      * with enable_thinking=False any <think> block in the wrapper is EMPTY (Qwen3's non-thinking
        mechanism pre-fills an empty think block), so the model is not asked to think
    `render(prompt, enable_thinking) -> str`. Raises BackendConfigError on any violation."""
    wrappers = []
    for probe in TEMPLATE_PROBES:
        text = render(probe, enable_thinking)
        if text.count(probe) != 1:
            raise BackendConfigError("chat template does not contain the prompt verbatim exactly once")
        i = text.index(probe)
        wrappers.append((text[:i], text[i + len(probe):]))
    if wrappers[0] != wrappers[1]:
        raise BackendConfigError("chat-template wrapper depends on the prompt content")
    prefix, suffix = wrappers[0]
    low = (prefix + suffix).lower()
    if "system" in low:
        raise BackendConfigError("chat template adds a system message")
    if re.search(r"(?<![<\w])/(?:no_)?think\b", low):          # a soft switch, not the </think> tag
        raise BackendConfigError("chat template adds a /think or /no_think switch to the text")
    if render(TEMPLATE_PROBES[0], not enable_thinking) == render(TEMPLATE_PROBES[0], enable_thinking):
        raise BackendConfigError("enable_thinking is silently ignored by this chat template")
    think_blocks = re.findall(r"<think>(.*?)</think>", suffix, flags=re.S)
    if not enable_thinking:
        if suffix.count("<think>") != len(think_blocks) or any(b.strip() for b in think_blocks):
            raise BackendConfigError("non-thinking render leaves an open or non-empty <think> block")
    return {"prompt_verbatim": True, "wrapper_constant": True, "system_message": False,
            "switch_text_added": False, "enable_thinking_honoured": True,
            "empty_think_block_prefilled": bool(think_blocks) and not enable_thinking,
            "prefix": prefix, "suffix": suffix,
            "wrapper_sha256": hashlib.sha256((prefix + "\x00" + suffix).encode("utf-8")).hexdigest()}


class HFTransformersBackend(Backend):
    """Local Hugging Face causal LM, pinned revision, greedy decoding (do_sample=False, num_beams=1).

    The frozen prompt is passed unchanged as ONE user message through the tokenizer's chat template with
    enable_thinking=cfg['enable_thinking'] (Qwen3 hard switch; nothing is appended to the prompt text).
    The rendered wrapper is verified once at start-up (check_chat_template) and every rendered request
    is re-checked to equal prefix + frozen prompt + suffix. temperature/top_p/top_k are ignored under
    greedy decoding and the model's own sampling defaults (generation_config.json) are overridden; both
    facts are recorded. The rendered template is backend metadata only and never enters the dataset.
    Loading is refused if free RAM is below cfg['min_free_ram_gb'] (avoids swapping mid-run)."""
    name = "hf_transformers"

    def __init__(self, cfg):
        super().__init__(cfg)
        need = cfg.get("min_free_ram_gb")
        have = free_ram_gb()
        if need and have is not None and have < need:
            raise BackendConfigError(f"only {have:.1f} GB RAM free, config requires {need} GB before loading "
                                     f"{cfg['model']}; close other applications and retry")
        import torch  # lazy: only when this backend is selected
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        rev = cfg.get("model_revision") or None
        if cfg.get("torch_num_threads"):
            torch.set_num_threads(int(cfg["torch_num_threads"]))
        torch.use_deterministic_algorithms(True, warn_only=True)
        if cfg.get("seed") is not None:
            torch.manual_seed(cfg["seed"])
        # provenance: verify the pinned commit on the Hub BEFORE loading, then load tokenizer and model from
        # the local snapshot directory of that commit (Hub cache; no forced re-download)
        self.provenance = {"repo_id": cfg["model"], "requested_revision": rev, "hub_resolved_revision": None,
                           "snapshot_path": None, "transformers_commit_hash": None,
                           "revision_verification": "none (no revision pinned)"}
        source, source_rev = cfg["model"], rev
        if rev:
            self.provenance["hub_resolved_revision"] = verify_pinned_revision(cfg["model"], rev)
            self.provenance["revision_verification"] = "HfApi().model_info(repo, revision).sha == requested_revision"
            if cfg.get("load_from_snapshot", True):
                snap = pinned_snapshot(cfg["model"], rev)
                self.provenance["snapshot_path"] = str(snap)
                self.provenance["revision_verification"] += "; loaded from snapshots/<pinned commit>"
                source, source_rev = str(snap), None
        self.tok = AutoTokenizer.from_pretrained(source, revision=source_rev)
        if not getattr(self.tok, "chat_template", None):
            raise BackendConfigError("tokenizer has no chat template; refusing to send raw text to a chat model")
        self.template_check = check_chat_template(self.render, self.enable_thinking)
        dtype = getattr(torch, cfg.get("dtype") or "bfloat16")
        device = cfg.get("device") or "cpu"
        if device.startswith("cuda"):
            if not torch.cuda.is_available():     # never fall back to CPU silently
                raise BackendConfigError("config requests CUDA but torch.cuda.is_available() is False")
            # weights go straight to the GPU (no full CPU copy first)
            self.model = AutoModelForCausalLM.from_pretrained(source, revision=source_rev, dtype=dtype,
                                                              device_map=device)
        else:
            self.model = AutoModelForCausalLM.from_pretrained(source, revision=source_rev, dtype=dtype,
                                                              low_cpu_mem_usage=True)
            self.model.to(device)
        self.model.eval()
        self.model_defaults = {k: getattr(self.model.generation_config, k, None)
                               for k in ("do_sample", "temperature", "top_p", "top_k", "repetition_penalty")}
        # informational only: None is normal (e.g. when loading from a local snapshot path); only a
        # CONTRADICTING commit hash is an error
        tch = getattr(self.model.config, "_commit_hash", None)
        self.provenance["transformers_commit_hash"] = tch
        if rev and tch is not None and tch != rev:
            raise BackendConfigError(f"Transformers reports commit {tch}, contradicting pinned {rev}")
        self.eos_ids = self.model.generation_config.eos_token_id

    def render(self, prompt, enable_thinking):
        return self.tok.apply_chat_template([{"role": "user", "content": prompt}], tokenize=False,
                                            add_generation_prompt=True, enable_thinking=enable_thinking)

    def generate(self, prompt):
        text_in = self.render(prompt, self.enable_thinking)
        tc = self.template_check
        if text_in != tc["prefix"] + prompt + tc["suffix"]:
            raise BackendConfigError("rendered request differs from prefix + frozen prompt + suffix")
        enc = self.tok(text_in, return_tensors="pt", add_special_tokens=False)
        ids = enc.input_ids.to(self.model.device)
        attn = enc.attention_mask.to(self.model.device)
        pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        with self.torch.no_grad():
            out = self.model.generate(input_ids=ids, attention_mask=attn, max_new_tokens=self.cfg["max_new_tokens"],
                                      do_sample=False, num_beams=1, temperature=None, top_p=None, top_k=None,
                                      pad_token_id=pad)
        new = out[0, ids.shape[1]:]
        eos = set(self.eos_ids if isinstance(self.eos_ids, list) else [self.eos_ids])
        ended = len(new) > 0 and int(new[-1]) in eos
        return GenerationResult(text=self.tok.decode(new, skip_special_tokens=True), metadata={
            "finish_reason": "stop" if ended else ("length" if len(new) >= self.cfg["max_new_tokens"] else "other"),
            "prompt_tokens": int(ids.shape[1]), "new_tokens": int(len(new)),
            "think_tag_in_output": "<think>" in self.tok.decode(new, skip_special_tokens=False),
            "enable_thinking": self.enable_thinking})

    def effective_settings(self):
        tc = {k: v for k, v in self.template_check.items() if k not in ("prefix", "suffix")}
        return {"backend": self.name, "model": self.cfg["model"],
                "requested_revision": self.provenance["requested_revision"],
                "hub_resolved_revision": self.provenance["hub_resolved_revision"],
                "transformers_commit_hash": self.provenance["transformers_commit_hash"],
                "snapshot_path": self.provenance["snapshot_path"],
                "revision_verification": self.provenance["revision_verification"],
                "decoding": "greedy (do_sample=False, num_beams=1)", "temperature": "ignored (greedy)",
                "top_p": "ignored (greedy)", "top_k": "ignored (greedy)",
                "model_generation_config_defaults_overridden": self.model_defaults,
                "max_new_tokens": self.cfg["max_new_tokens"], "seed": self.cfg.get("seed"),
                "enable_thinking": self.enable_thinking, "chat_template_check": tc,
                "device": str(self.model.device), "dtype": str(self.model.dtype),
                "quantization": self.cfg.get("quantization") or "none",
                "torch_num_threads": self.torch.get_num_threads(),
                "deterministic_algorithms": "torch.use_deterministic_algorithms(True, warn_only=True)",
                "system_prompt": None, "message_format": "single user message via chat template"}

    def versions(self):
        return package_versions(["transformers", "torch", "accelerate", "safetensors", "tokenizers",
                                 "huggingface_hub", "bitsandbytes"])


BACKENDS = {"mock": MockBackend, "openai_compatible": OpenAICompatibleBackend, "ollama": OllamaBackend,
            "hf_transformers": HFTransformersBackend}


def make_backend(cfg, **mock_kwargs):
    if cfg["backend"] not in BACKENDS:
        raise BackendConfigError(f"unknown backend {cfg['backend']!r}; choose one of {sorted(BACKENDS)}")
    cls = BACKENDS[cfg["backend"]]
    return cls(cfg, **mock_kwargs) if cls is MockBackend else cls(cfg)


def environment_info():
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine()}
