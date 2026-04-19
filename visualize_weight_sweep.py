#!/usr/bin/env python3
"""
Weight Sweep Visualizer
=======================
Plots throughput and avg cost per job vs --snark-cost-weight values produced
by weight_sweep.py.

Two panels:
  - Top:    throughput (req/s) and p95 latency vs weight — left/right axes
  - Bottom: avg cost per job vs weight, with floor (1.0) and ceiling (1.5)
            reference lines and a composite score (throughput / cost) overlaid

The composite score = throughput / avg_cost makes the trade-off explicit:
it peaks at the weight that gives you the best throughput-per-cost-unit.

USAGE:
    python visualize_weight_sweep.py
    python visualize_weight_sweep.py --input weight_sweep_results.csv --output weight_sweep.png
    python visualize_weight_sweep.py --show

REQUIREMENTS:
    pip install pandas matplotlib
"""

import argparse
import sys
from pathlib import Path

try:
    import pandas as pd
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
except ImportError:
    print("[ERROR] Missing dependencies. Run: pip install pandas matplotlib")
    sys.exit(1)


def plot_weight_sweep(csv_path, output_path, show=False):
    df = pd.read_csv(csv_path)
    if df.empty:
        print(f"[ERROR] {csv_path} is empty — run weight_sweep.py first")
        return

    df = df.sort_values("snark_cost_weight").reset_index(drop=True)

    has_cost = "snark_avg_cost_per_job" in df.columns and df["snark_avg_cost_per_job"].notna().any()
    n_panels  = 2 if has_cost else 1

    fig, axes = plt.subplots(n_panels, 1, figsize=(11, 4.5 * n_panels), sharex=True)
    if n_panels == 1:
        axes = [axes]

    fig.suptitle(
        "SNARK Cost-Weight Sweep — Throughput vs Cost Trade-off",
        fontsize=13, fontweight="bold",
    )

    x = df["snark_cost_weight"]

    # ------------------------------------------------------------------ #
    # Panel 1 — Throughput (left) + p95 Latency (right)
    # ------------------------------------------------------------------ #
    ax1 = axes[0]
    COLOR_TP  = "#1f77b4"
    COLOR_LAT = "#d62728"

    ax1.plot(x, df["throughput_req_per_sec"],                          # [OPTIONAL] comment to remove throughput line
             marker="o", color=COLOR_TP, linewidth=2.5, label="Throughput")
    ax1.set_ylabel("Throughput (req/s)", color=COLOR_TP, fontsize=11)
    ax1.tick_params(axis="y", labelcolor=COLOR_TP)
    ax1.grid(True, alpha=0.25, zorder=0)
    ax1.yaxis.set_minor_locator(ticker.AutoMinorLocator())

    ax_lat = ax1.twinx()
    ax_lat.plot(x, df["async_p95_ms"],                                 # [OPTIONAL] comment to remove p95 latency line
                marker="^", linestyle="--", color=COLOR_LAT,
                linewidth=1.5, alpha=0.75, label="p95 latency")
    ax_lat.set_ylabel("p95 Latency (ms)", color=COLOR_LAT, fontsize=11)
    ax_lat.tick_params(axis="y", labelcolor=COLOR_LAT)

    # Mark optimal throughput
    best_tp_idx  = df["throughput_req_per_sec"].idxmax()
    best_tp_w    = df["snark_cost_weight"].iloc[best_tp_idx]
    best_tp_val  = df["throughput_req_per_sec"].iloc[best_tp_idx]
    ax1.axvline(x=best_tp_w, color=COLOR_TP, linestyle=":", alpha=0.5, linewidth=1.2)  # [OPTIONAL] comment to remove peak-throughput vline
    ax1.annotate(                                                       # [OPTIONAL] comment these 7 lines to remove peak annotation
        f"peak {best_tp_val:.1f} req/s\nweight={best_tp_w}",
        xy=(best_tp_w, best_tp_val),
        xytext=(8, -20), textcoords="offset points",
        fontsize=8, color=COLOR_TP,
        arrowprops=dict(arrowstyle="->", color=COLOR_TP, alpha=0.5),
    )

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax_lat.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2,
               loc="upper left", fontsize=9, framealpha=0.9)

    # ------------------------------------------------------------------ #
    # Panel 2 — Avg cost per job + composite score
    # ------------------------------------------------------------------ #
    if has_cost:
        ax2 = axes[1]
        COLOR_COST  = "#ff7f0e"
        COLOR_SCORE = "#2ca02c"
        COST_FLOOR   = 1.0
        COST_CEILING = 1.5

        ax2.plot(x, df["snark_avg_cost_per_job"],                      # [OPTIONAL] comment to remove avg cost line
                 marker="s", color=COLOR_COST, linewidth=2.5, label="Avg cost per job")
        ax2.set_ylabel("Avg cost per job", color=COLOR_COST, fontsize=11)
        ax2.tick_params(axis="y", labelcolor=COLOR_COST)
        ax2.set_ylim(0.8, 1.8)
        ax2.grid(True, alpha=0.25, axis="y")

        # Reference lines
        ax2.axhline(y=COST_FLOOR,   color="grey", linestyle="--",       # [OPTIONAL] comment to remove floor reference line
                    linewidth=0.9, alpha=0.6)
        ax2.annotate("floor = 1.0 (all-cheap)", xy=(x.iloc[0], COST_FLOOR),
                     xytext=(4, 3), textcoords="offset points",
                     fontsize=7, color="grey")
        ax2.axhline(y=COST_CEILING, color="grey", linestyle="--",       # [OPTIONAL] comment to remove ceiling reference line
                    linewidth=0.9, alpha=0.6)
        ax2.annotate("ceiling = 1.5 (round-robin theory)", xy=(x.iloc[0], COST_CEILING),
                     xytext=(4, 3), textcoords="offset points",
                     fontsize=7, color="grey")

        # Composite score on secondary axis: throughput / avg_cost
        df["composite_score"] = df["throughput_req_per_sec"] / df["snark_avg_cost_per_job"].clip(lower=0.01)
        ax_score = ax2.twinx()
        ax_score.plot(x, df["composite_score"],                         # [OPTIONAL] comment to remove composite score line
                      marker="D", linestyle=":", color=COLOR_SCORE,
                      linewidth=1.5, alpha=0.8, label="Composite (tp/cost)")
        ax_score.set_ylabel("Composite score\n(throughput / cost)", color=COLOR_SCORE, fontsize=10)
        ax_score.tick_params(axis="y", labelcolor=COLOR_SCORE)

        # Mark optimal composite
        best_score_idx = df["composite_score"].idxmax()
        best_score_w   = df["snark_cost_weight"].iloc[best_score_idx]
        best_score_val = df["composite_score"].iloc[best_score_idx]
        ax_score.axvline(x=best_score_w, color=COLOR_SCORE,             # [OPTIONAL] comment to remove optimal-weight vline
                         linestyle=":", alpha=0.5, linewidth=1.2)
        ax_score.annotate(                                               # [OPTIONAL] comment these 8 lines to remove optimal-weight annotation
            f"optimal weight={best_score_w}\n"
            f"score={best_score_val:.1f}",
            xy=(best_score_w, best_score_val),
            xytext=(8, -20), textcoords="offset points",
            fontsize=8, color=COLOR_SCORE,
            arrowprops=dict(arrowstyle="->", color=COLOR_SCORE, alpha=0.5),
        )

        lines3, labels3 = ax2.get_legend_handles_labels()
        lines4, labels4 = ax_score.get_legend_handles_labels()
        ax2.legend(lines3 + lines4, labels3 + labels4,
                   loc="upper right", fontsize=9, framealpha=0.9)

        ax2.set_xlabel("snark-cost-weight", fontsize=11)
    else:
        axes[-1].set_xlabel("snark-cost-weight", fontsize=11)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    print(f"[OK] Saved: {output_path}")

    if has_cost:
        best_w = df.loc[df["composite_score"].idxmax(), "snark_cost_weight"]
        best_tp = df.loc[df["composite_score"].idxmax(), "throughput_req_per_sec"]
        best_cost = df.loc[df["composite_score"].idxmax(), "snark_avg_cost_per_job"]
        print(f"     Optimal weight: {best_w}  "
              f"(throughput={best_tp:.1f} req/s, avg_cost={best_cost:.3f})")

    if show:
        plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="Plot weight sweep results from weight_sweep.py"
    )
    parser.add_argument("--input", default="weight_sweep_results.csv",
                        help="Input CSV (default: weight_sweep_results.csv)")
    parser.add_argument("--output", default="weight_sweep_graph.png",
                        help="Output PNG (default: weight_sweep_graph.png)")
    parser.add_argument("--show", action="store_true",
                        help="Also open graph in interactive window")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"[ERROR] {args.input} not found. Run weight_sweep.py first.")
        sys.exit(1)

    plot_weight_sweep(args.input, args.output, show=args.show)


if __name__ == "__main__":
    main()
