"""WP4 report figure: M2's gain over B1 (NDCG@10) on the development set, the sealed test split and the five CV folds.

The numbers are the ones printed in the Final Report (artifacts/wp4_d5_dev/report.md, artifacts/wp4_d5_test/report.md and
the five-fold run, scripts/wp4_m2_cv.py). Run from MetaRec-backend/:
    python scripts/wp4_plot_gain_summary.py ../docs/wp4/figures/fig6_m2_gain_summary.png
"""
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = sys.argv[1] if len(sys.argv) > 1 else "fig6_m2_gain_summary.png"

INK, MUTED, GRID, SERIES = "#0b0b0b", "#52514e", "#e3e2de", "#2a78d6"
TARGET = 3.0
dev = (0.81, 0.69, 0.94)                 # point, 95% lower, 95% upper (held-out users, 479,889 cases)
test = (1.87, 1.70, 2.04)                # sealed test split, 443,817 cases
folds = [0.88, 1.00, 0.61, 0.80, 0.69]   # per-fold gains, user-level 5-fold CV on the development users
cv_mean, cv_sd = 0.79, 0.15

plt.rcParams.update({"font.family": "serif", "font.serif": ["Times New Roman", "DejaVu Serif"], "font.size": 9})
fig, ax = plt.subplots(figsize=(7.0, 3.2), dpi=300)
fig.patch.set_facecolor("white")
ax.set_facecolor("white")

ys = {"dev": 3, "test": 2, "cv": 1}
ax.axvline(TARGET, color=MUTED, lw=1, ls=(0, (4, 3)), zorder=1)
ax.text(TARGET - 0.04, 3.62, "3% target\n(not met)", color=MUTED, ha="right", va="top", fontsize=8)

for key, (p, lo, hi) in (("dev", dev), ("test", test)):
    y = ys[key]
    ax.plot([lo, hi], [y, y], color=SERIES, lw=2, solid_capstyle="round", zorder=3)
    ax.plot([p], [y], "o", color=SERIES, ms=7, mec=SERIES, mew=0, zorder=4)
    ax.text(hi + 0.07, y, f"+{p:.2f}%  [+{lo:.2f}, +{hi:.2f}]", color=INK, va="center", fontsize=8.5)

y = ys["cv"]
ax.plot([min(folds), max(folds)], [y, y], color=SERIES, lw=2, alpha=0.35, solid_capstyle="round", zorder=2)
ax.plot(folds, [y] * len(folds), "o", color=SERIES, ms=5.5, mec="white", mew=1.2, alpha=0.9, zorder=3)
ax.plot([cv_mean], [y], "D", color=SERIES, ms=8, mec="white", mew=1.5, zorder=4)
ax.text(max(folds) + 0.07, y, f"mean +{cv_mean:.2f}%  (SD {cv_sd:.2f})", color=INK, va="center", fontsize=8.5)

ax.set_yticks([3, 2, 1])
ax.set_yticklabels(["Development set\nheld-out users, 479,889 cases", "Sealed test split\nopened once, 443,817 cases", "5-fold cross-validation\n5 folds of development users"], color=INK, fontsize=8.5)
ax.set_ylim(0.45, 3.75)
ax.set_xlim(0, 3.3)
ax.set_xlabel("NDCG@10 gain of M2 over B1 (%, relative)", color=INK, fontsize=9)
ax.set_xticks([0, 0.5, 1, 1.5, 2, 2.5, 3])
ax.tick_params(axis="x", colors=MUTED, length=3)
ax.tick_params(axis="y", length=0)
ax.grid(axis="x", color=GRID, lw=0.7, zorder=0)
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(GRID)
fig.text(0.012, 0.012, "Dots and bars: point estimate and 95% user-level bootstrap interval. Cross-validation: one dot per fold, diamond = mean.",
         color=MUTED, fontsize=7, ha="left", va="bottom")
fig.tight_layout(rect=(0, 0.04, 1, 1))
fig.savefig(OUT, facecolor="white")
print("saved", OUT)

