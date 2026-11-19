"""Stage-B figures (matplotlib, optional: skipped with a note if it is not installed). Every number shown is also in
the CSV tables (table view). Condition colors follow one fixed categorical order (validated light-mode palette;
two slots are below 3:1 contrast, so every bar carries a direct value label). TEST-ONLY figures are watermarked.
  fig1_stage1_repair.png           Stage-1 V repair rate (all / per domain), Wilson 95% CI
  fig2_stage2_rescue.png           rescue rate by Stage-2 condition, Wilson 95% CI
  fig3_final_repair.png            final repair rate by sequential policy (+ one-shot V)
  fig4_paired_Ltrue_vs_VV.png      paired outcome counts V->L_true vs V->V
  fig5_failure_decomposition.png   outcome shares, Stage 1 and each Stage-2 condition (stacked, 2px gaps)
  fig6_gain_vs_tokens.png          final-repair gain over one-shot V vs additional generated tokens per item
"""
from pathlib import Path

from . import common as C

COLORS = {"VV": "#2a78d6", "N_true": "#eb6834", "L_true": "#1baf7a", "L_false": "#eda100", "V": "#8a8984"}
OUTCOME_COLORS = {"repair": "#2a78d6", "original_answer_retention": "#eb6834", "stage1_answer_retention": "#1baf7a",
                  "new_wrong_answer": "#eda100", "parse_failure": "#e87ba4"}
TEXT, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"


def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        return None


def _style(ax, title, ylabel=None):
    ax.set_title(title, color=TEXT, fontsize=11, loc="left")
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)


def _bars(ax, labels, vals, colors, lo=None, hi=None):
    x = range(len(vals))
    ax.bar(x, vals, color=colors, width=0.6, edgecolor=SURFACE, linewidth=2)
    if lo is not None:
        err = [[v - l for v, l in zip(vals, lo)], [h - v for v, h in zip(vals, hi)]]
        ax.errorbar(x, vals, yerr=err, fmt="none", ecolor=MUTED, elinewidth=1, capsize=3)
    for i, v in enumerate(vals):
        top = hi[i] if hi is not None and hi[i] is not None else v
        ax.text(i, top + 0.02, f"{v:.2f}", ha="center", va="bottom", color=TEXT, fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, color=TEXT)


def make_all(out_dir, T, summary):
    plt = _plt()
    out = Path(out_dir) / "figures"
    if plt is None:
        return {"skipped": "matplotlib not installed; tables are complete without figures"}
    out.mkdir(parents=True, exist_ok=True)
    test = summary["TEST_ONLY"]
    made = []

    def save(fig, name):
        if test:
            fig.text(0.5, 0.5, C.TEST_ONLY_BANNER, ha="center", va="center", fontsize=16, color="#e34948",
                     alpha=0.35, rotation=20)
        fig.patch.set_facecolor(SURFACE)
        fig.tight_layout()
        fig.savefig(out / name, dpi=150)
        plt.close(fig)
        made.append(name)

    s1 = [r for r in T["stage1_metrics"] if not r["scope"].startswith("math Level")]
    fig, ax = plt.subplots(figsize=(5, 3.4))
    _bars(ax, [r["scope"] for r in s1], [r["stage1_repair_rate"] for r in s1], [COLORS["V"]] * len(s1),
          [r["ci95_low"] for r in s1], [r["ci95_high"] for r in s1])
    ax.set_ylim(0, 1.1)
    _style(ax, "Stage-1 V repair rate (Wilson 95% CI)", "repair rate")
    save(fig, "fig1_stage1_repair.png")

    rs = [r for r in T["stage2_rescue_metrics"] if r["scope"] == "all" and r["condition"] != "-"]
    fig, ax = plt.subplots(figsize=(6, 3.6))
    _bars(ax, [r["policy"] for r in rs], [r["rescue_rate"] for r in rs], [COLORS[r["condition"]] for r in rs],
          [r["rescue_ci95_low"] for r in rs], [r["rescue_ci95_high"] for r in rs])
    ax.set_ylim(0, 1.1)
    _style(ax, f"Stage-2 rescue rate, review-confirmed Stage-1 failures (n={rs[0]['n_received']})", "rescue rate")
    save(fig, "fig2_stage2_rescue.png")

    fr = [r for r in T["stage2_rescue_metrics"] if r["scope"] == "all"]
    fig, ax = plt.subplots(figsize=(6.6, 3.6))
    _bars(ax, [r["policy"] for r in fr], [r["final_repair_rate"] for r in fr],
          [COLORS["V" if r["condition"] == "-" else r["condition"]] for r in fr],
          [r["final_ci95_low"] for r in fr], [r["final_ci95_high"] for r in fr])
    ax.set_ylim(0, 1.1)
    _style(ax, f"Final repair rate by policy (analysis population n={fr[0]['n_population']})", "final repair rate")
    save(fig, "fig3_final_repair.png")

    p = T["stage2_paired_comparisons"][0]                 # PRIMARY
    cats = ["both rescue", "L_true only", "V retry only", "neither"]
    vals = [p["both_rescue"], p["A_only_rescues"], p["B_only_rescues"], p["neither_rescues"]]
    fig, ax = plt.subplots(figsize=(6, 3.4))
    ax.bar(range(4), vals, color=[MUTED, COLORS["L_true"], COLORS["VV"], GRID], width=0.6, edgecolor=SURFACE,
           linewidth=2)
    for i, v in enumerate(vals):
        ax.text(i, v + 0.3, str(v), ha="center", va="bottom", color=TEXT, fontsize=9)
    ax.set_xticks(range(4))
    ax.set_xticklabels(cats, color=TEXT)
    _style(ax, f"Paired: V->L_true vs V->V (Delta {p['delta_rescue']}, 95% CI {p['delta_rescue_ci_low']}"
               f"..{p['delta_rescue_ci_high']})", "items")
    save(fig, "fig4_paired_Ltrue_vs_VV.png")

    fd = T["failure_decomposition"]
    groups = [("Stage 1 V", 1, "V")] + [(f"S2 {c}", 2, c) for c in ("VV", "N_true", "L_true", "L_false")]
    fig, ax = plt.subplots(figsize=(7, 3.8))
    for gi, (lab, st, c) in enumerate(groups):
        bottom = 0
        for o, col in OUTCOME_COLORS.items():
            r = next((x for x in fd if x["stage"] == st and x["condition"] == c and x["outcome"] == o), None)
            v = (r or {}).get("rate") or 0
            if v:
                ax.bar(gi, v, bottom=bottom, color=col, width=0.6, edgecolor=SURFACE, linewidth=2)
                bottom += v
    from matplotlib.patches import Patch
    order = list(OUTCOME_COLORS.items())[::-1]          # legend top-to-bottom = stack top-to-bottom
    ax.legend([Patch(color=c) for _, c in order], [o for o, _ in order], fontsize=7, frameon=False,
              loc="upper left", bbox_to_anchor=(1, 1))
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([g[0] for g in groups], color=TEXT, fontsize=8)
    ax.set_ylim(0, 1.05)
    _style(ax, "Outcome decomposition (share of requests)", "share")
    save(fig, "fig5_failure_decomposition.png")

    ct = T["cost_and_tokens"]
    fig, ax = plt.subplots(figsize=(6.6, 3.8))
    keymap = {"V (one-shot)": "V", **{v: k for k, v in summarise_labels().items()}}
    for r in ct:                                    # 5 series -> legend (identity never color-alone)
        c = keymap.get(r["policy"], "V")
        ax.scatter([r["additional_generated_tokens_vs_V"]], [r["final_repair_gain_vs_V_pp"]], s=64, color=COLORS[c],
                   edgecolors=SURFACE, linewidths=2, zorder=3, label=r["policy"])
    ax.legend(fontsize=8, frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
    ax.margins(x=0.08, y=0.12)
    _style(ax, "Final-repair gain over one-shot V vs extra tokens", "gain (pp)")
    ax.set_xlabel("additional generated tokens per original item", color=MUTED)
    save(fig, "fig6_gain_vs_tokens.png")
    return {"figures": made}


def summarise_labels():
    return C.load_config()["stage2"]["condition_labels"]
