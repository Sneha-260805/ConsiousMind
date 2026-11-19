"""Stage-B solver prompts (fixed before any Stage-B model output exists).

STAGE 1 (V, every item):

    Problem:
    {question}

    Original proposed solution:
    Step 1: ...
    Step n: ...

    Feedback:
    The final answer in the proposed solution is wrong.

    Assess the proposed solution carefully and give your best corrected final answer.

    Return the final answer on the last line exactly as:

    FINAL_ANSWER: <answer>

STAGE 2 (new stateless request; only for frozen Stage-1 failures; all four conditions of an item embed the SAME
Stage-1 response, inserted verbatim apart from stripping leading/trailing whitespace):

    Problem:
    {question}

    Original proposed solution:
    Step 1: ...

    Feedback on the original proposed solution:
    The final answer in the proposed solution is wrong.

    Your previous repair attempt:
    ----- BEGIN PREVIOUS REPAIR ATTEMPT -----
    {stage-1 response}
    ----- END PREVIOUS REPAIR ATTEMPT -----

    Feedback:
    Your previous repair attempt is still incorrect. {CONDITION SENTENCE}

    Assess the original proposed solution carefully and give your best corrected final answer.

    Return the final answer on the last line exactly as:

    FINAL_ANSWER: <answer>

Every Stage-2 feedback paragraph is exactly TWO sentences. Sentence 1 is identical for all conditions
("Your previous repair attempt is still incorrect."); sentence 2 alone defines the condition and is the ONLY text
that differs between the Stage-2 prompts of an item:
    VV       Reassess the original proposed solution and your previous repair carefully.
    N_true   Pay special attention to Step {k} of the ORIGINAL proposed solution.
    L_true   The first mistake in the ORIGINAL proposed solution is in Step {k}.
    L_false  The first mistake in the ORIGINAL proposed solution is in Step {f}.
k = annotated first-error step (1-indexed); f = k - 1 (correct pre-error step). L_true and L_false are
byte-identical except for the step number.

OPTIONAL one-shot references (supported, NOT in the request grid unless enabled in the config): the Stage-1 layout
with the feedback paragraph replaced by
    L_true_1shot   The first mistake in the proposed solution is in Step {k}.
    N_true_1shot   Pay special attention to Step {k} of the proposed solution.
    L_false_1shot  The first mistake in the proposed solution is in Step {f}.
    L0_1shot       (no feedback paragraph; closing line "Assess the proposed solution carefully and give your best
                   final answer.")
"""
from .dataset_builder import render_trace

V_FEEDBACK = "The final answer in the proposed solution is wrong."
STAGE1_CLOSING = "Assess the proposed solution carefully and give your best corrected final answer."
STAGE2_CLOSING = "Assess the original proposed solution carefully and give your best corrected final answer."
L0_CLOSING = "Assess the proposed solution carefully and give your best final answer."
FORMAT = "Return the final answer on the last line exactly as:\n\nFINAL_ANSWER: <answer>"
STILL_WRONG = "Your previous repair attempt is still incorrect."
BEGIN = "----- BEGIN PREVIOUS REPAIR ATTEMPT -----"
END = "----- END PREVIOUS REPAIR ATTEMPT -----"

STAGE2_CONDITIONS = ["VV", "N_true", "L_true", "L_false"]
ONE_SHOT_CONDITIONS = ["L_true_1shot", "N_true_1shot", "L_false_1shot", "L0_1shot"]

# scaffold audit (applied to the prompt with question / trace / previous attempt removed)
FORBIDDEN = ["processbench", "gold", "annotat", "oracle", "label", "stage", "generator", "reference answer",
             "sb_", "gsm8k", "hendrycks", "critic", "verifier", "true step", "false step", "l_true", "l_false",
             "n_true", "placebo", "escalat"]
ERROR_WORDS = ["mistake", "error", "wrong", "incorrect", "flaw", "bug"]


def pointed_step(item, condition):
    """Step number a condition points at (None for VV / L0)."""
    if condition in ("N_true", "L_true", "N_true_1shot", "L_true_1shot"):
        return item["true_error_step"]
    if condition in ("L_false", "L_false_1shot"):
        fl = item.get("false_location")
        if not fl:
            raise ValueError(f"{item['item_id']}: no valid false step")
        return fl["false_step"]
    return None


# Stage-2 feedback = FIRST_SENTENCE (identical for every condition) + " " + the condition's SECOND sentence, which
# alone defines the condition.
SECOND_SENTENCE = {
    "VV": "Reassess the original proposed solution and your previous repair carefully.",
    "N_true": "Pay special attention to Step {step} of the ORIGINAL proposed solution.",
    "L_true": "The first mistake in the ORIGINAL proposed solution is in Step {step}.",
    "L_false": "The first mistake in the ORIGINAL proposed solution is in Step {step}.",
}


def condition_sentence(condition, step=None):
    """The SECOND (condition-defining) sentence of the Stage-2 feedback."""
    return SECOND_SENTENCE[condition].format(step=step)


def stage1_prompt(item):
    return "\n\n".join([f"Problem:\n{item['question']}",
                        f"Original proposed solution:\n{render_trace(item['steps'])}",
                        f"Feedback:\n{V_FEEDBACK}", STAGE1_CLOSING, FORMAT])


def stage2_feedback(item, condition):
    second = condition_sentence(condition, pointed_step(item, condition))
    return f"{STILL_WRONG} {second}"


def stage2_prompt(item, condition, stage1_response):
    if condition not in STAGE2_CONDITIONS:
        raise ValueError(condition)
    prev = (stage1_response or "").strip()
    return "\n\n".join([f"Problem:\n{item['question']}",
                        f"Original proposed solution:\n{render_trace(item['steps'])}",
                        f"Feedback on the original proposed solution:\n{V_FEEDBACK}",
                        f"Your previous repair attempt:\n{BEGIN}\n{prev}\n{END}",
                        f"Feedback:\n{stage2_feedback(item, condition)}", STAGE2_CLOSING, FORMAT])


def one_shot_prompt(item, condition):
    step = pointed_step(item, condition)
    fb = {"L_true_1shot": f"The first mistake in the proposed solution is in Step {step}.",
          "N_true_1shot": f"Pay special attention to Step {step} of the proposed solution.",
          "L_false_1shot": f"The first mistake in the proposed solution is in Step {step}.",
          "L0_1shot": None}[condition]
    parts = [f"Problem:\n{item['question']}", f"Original proposed solution:\n{render_trace(item['steps'])}"]
    if fb is None:
        return "\n\n".join(parts + [L0_CLOSING, FORMAT])
    return "\n\n".join(parts + [f"Feedback:\n{fb}", STAGE1_CLOSING, FORMAT])


def scaffold(prompt, item, stage1_response=None):
    """Prompt text with the item content (question, trace, previous attempt) removed - used by the audits."""
    s = prompt.replace(item["question"], "").replace(render_trace(item["steps"]), "")
    if stage1_response is not None:
        s = s.replace((stage1_response or "").strip(), "")
    return s


def find_terms(text, terms):
    low = text.lower()
    return [t for t in terms if t in low]
