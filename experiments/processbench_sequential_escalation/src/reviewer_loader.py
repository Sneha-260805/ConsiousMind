"""Reviewer construction for the A3 LLM-assisted annotation review (configs/stage_b.json -> llm_review.reviewer).

Real reviewer (amendment A4; A3's Qwen2.5-Math-7B-Instruct was replaced after its smoke NO-GO):
Qwen/Qwen2.5-7B-Instruct at a pinned Hub commit, loaded 4-bit NF4 with bitsandbytes (compute dtype float16: the
Kaggle T4 has no bfloat16), cuda:0, batch size 1, pure greedy decoding (do_sample=False, num_beams=1,
repetition_penalty 1.0 passed explicitly so the checkpoint's generation_config sampling defaults never apply), fixed
system message + one user message, no tools, no web access. The Hub revision is verified BEFORE
loading (frozen pilot helpers), the quantization actually applied is read back from the loaded model and compared
with the config, and every rendered request must equal llm_review.render_chat(prompt) exactly. A request whose
prompt + max_new_tokens would exceed the model context STOPS the run (never truncated silently).
Local safety: same guard as the solver (Kaggle or STAGE_B_ALLOW_MODEL_LOAD=1, and CUDA).
"""
from . import common as C
from . import frozen_sources as fs
from . import llm_review as lr
from .solver_loader import guard_real_model


class QwenReviewer:
    name = "hf_transformers_bnb4"

    def __init__(self, rc):
        guard_real_model()
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        be = fs.pilot_module("backends")
        self.torch, self.rc, self.be = torch, rc, be
        torch.use_deterministic_algorithms(True, warn_only=True)
        torch.manual_seed(rc["seed"])
        q = rc["quantization"]
        self.hub_revision = be.verify_pinned_revision(rc["model"], rc["model_revision"])
        snap = be.pinned_snapshot(rc["model"], rc["model_revision"])
        self.snapshot = str(snap)
        self.tok = AutoTokenizer.from_pretrained(self.snapshot)
        bnb = BitsAndBytesConfig(load_in_4bit=q["load_in_4bit"], bnb_4bit_quant_type=q["bnb_4bit_quant_type"],
                                 bnb_4bit_compute_dtype=getattr(torch, q["bnb_4bit_compute_dtype"]),
                                 bnb_4bit_use_double_quant=q["bnb_4bit_use_double_quant"])
        self.model = AutoModelForCausalLM.from_pretrained(self.snapshot, quantization_config=bnb,
                                                          dtype=getattr(torch, q["bnb_4bit_compute_dtype"]),
                                                          device_map=rc["device"])
        self.model.eval()
        self.eos_ids = self.model.generation_config.eos_token_id
        self.ctx = int(self.model.config.max_position_embeddings)
        import bitsandbytes
        qc = self.model.config.quantization_config
        get = (lambda k: qc.get(k)) if isinstance(qc, dict) else (lambda k: getattr(qc, k, None))
        method = get("quant_method")
        self.applied_quantization = {
            "quant_method": str(getattr(method, "value", method)), "load_in_4bit": bool(get("load_in_4bit")),
            "bnb_4bit_quant_type": str(get("bnb_4bit_quant_type")),
            "bnb_4bit_compute_dtype": str(get("bnb_4bit_compute_dtype")).replace("torch.", ""),
            "bnb_4bit_use_double_quant": bool(get("bnb_4bit_use_double_quant")),
            "n_linear4bit_modules": sum(isinstance(m, bitsandbytes.nn.Linear4bit) for m in self.model.modules()),
            "bitsandbytes_version": bitsandbytes.__version__}
        want = {"quant_method": "bitsandbytes", "load_in_4bit": q["load_in_4bit"],
                "bnb_4bit_quant_type": q["bnb_4bit_quant_type"], "bnb_4bit_compute_dtype": q["bnb_4bit_compute_dtype"],
                "bnb_4bit_use_double_quant": q["bnb_4bit_use_double_quant"]}
        bad = []
        if self.hub_revision != rc["model_revision"]:
            bad.append("revision")
        if any(self.applied_quantization[k] != v for k, v in want.items()):
            bad.append(f"quantization {self.applied_quantization}")
        if self.applied_quantization["n_linear4bit_modules"] == 0:
            bad.append("no bitsandbytes Linear4bit layers in the loaded model")
        if bitsandbytes.__version__ != rc["bitsandbytes_version"]:
            bad.append(f"bitsandbytes {bitsandbytes.__version__}")
        if str(self.model.device) != rc["device"]:
            bad.append(f"device {self.model.device}")
        if self.ctx != rc["max_context_tokens"]:
            bad.append(f"context {self.ctx}")
        probe = "probe user message"
        if self.render(probe) != lr.render_chat(probe):
            bad.append("chat rendering differs from llm_review.render_chat")
        if bad:
            raise SystemExit(f"STOP: loaded reviewer differs from the registered configuration: {bad}")

    def render(self, prompt):
        return self.tok.apply_chat_template([{"role": "system", "content": lr.SYSTEM_PROMPT},
                                             {"role": "user", "content": prompt}], tokenize=False,
                                            add_generation_prompt=True)

    def generate(self, prompt):
        from .mock_solver import MockResult
        text_in = self.render(prompt)
        if text_in != lr.render_chat(prompt):
            raise SystemExit("STOP: rendered reviewer request differs from the fixed system + user rendering")
        enc = self.tok(text_in, return_tensors="pt", add_special_tokens=False)
        ids, attn = enc.input_ids.to(self.model.device), enc.attention_mask.to(self.model.device)
        mx = self.rc["max_new_tokens"]
        if ids.shape[1] + mx > self.ctx:
            raise SystemExit(f"STOP: prompt ({ids.shape[1]} tokens) + max_new_tokens ({mx}) exceeds the reviewer "
                             f"context ({self.ctx}); nothing is truncated silently")
        pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        with self.torch.no_grad():
            out = self.model.generate(input_ids=ids, attention_mask=attn, pad_token_id=pad, **self.generation_kwargs())
        new = out[0, ids.shape[1]:]
        eos = set(self.eos_ids if isinstance(self.eos_ids, list) else [self.eos_ids])
        ended = len(new) > 0 and int(new[-1]) in eos
        return MockResult(self.tok.decode(new, skip_special_tokens=True), {
            "finish_reason": "stop" if ended else ("length" if len(new) >= mx else "other"),
            "prompt_tokens": int(ids.shape[1]), "new_tokens": int(len(new))})

    def generation_kwargs(self):
        return {"max_new_tokens": self.rc["max_new_tokens"], "do_sample": False, "num_beams": 1, "temperature": None,
                "top_p": None, "top_k": None, "repetition_penalty": self.rc["repetition_penalty"]}

    def effective_settings(self):
        return {"backend": self.name, "model": self.rc["model"], "requested_revision": self.rc["model_revision"],
                "hub_resolved_revision": self.hub_revision, "snapshot_path": self.snapshot,
                "applied_quantization": self.applied_quantization,
                "is_loaded_in_4bit": self.applied_quantization["n_linear4bit_modules"] > 0,
                "device": str(self.model.device), "max_context_tokens": self.ctx,
                "decoding": "greedy (do_sample=False, num_beams=1)", "batch_size": 1,
                "max_new_tokens": self.rc["max_new_tokens"], "seed": self.rc["seed"],
                "generation_kwargs": self.generation_kwargs(),
                "system_prompt_sha256": C.sha256_text(lr.SYSTEM_PROMPT), "tools": None, "web_access": None,
                "deterministic_algorithms": "torch.use_deterministic_algorithms(True, warn_only=True)",
                "package_versions": self.be.package_versions(["torch", "transformers", "accelerate", "bitsandbytes",
                                                              "tokenizers", "safetensors", "huggingface_hub"])}


def make_reviewer(kind, cfg, profile="default"):
    """-> (reviewer, backend_name, test_only)"""
    rc = cfg["llm_review"]["reviewer"]
    if kind == "mock":
        return lr.MockReviewer(rc["max_new_tokens"], profile), "mock", True
    if kind == "qwen_instruct":
        r = QwenReviewer(rc)
        return r, r.name, False
    if kind == "qwen_math":
        raise SystemExit("STOP: the A3 reviewer Qwen2.5-Math-7B-Instruct was replaced by amendment A4 (qwen_instruct)")
    raise SystemExit(f"unknown reviewer {kind!r} (mock | qwen_instruct)")
