#!/usr/bin/env python3
"""
Routing Algorithm Comparison Visualizer
========================================
Plots throughput, latency, failures, and cost-per-job from two sweep
CSV files on the same axes — weighted least-queue vs round-robin.

The cost panel is the key result: weighted routing fills cheap nodes
(cost=1.0) first and only spills to expensive nodes (cost=2.0) under
heavy load, so average cost per job stays near 1.0 at low-to-medium VUs.
Round-robin splits jobs evenly regardless of cost, so it always pays the
arithmetic mean (1.5) — a difference that widens under moderate load.

USAGE:
    python visualize_comparison.py sweep_weighted.csv sweep_roundrobin.csv
    python visualize_comparison.py sweep_weighted.csv sweep_roundrobin.csv --out result.png
    python visualize_comparison.py sweep_weighted.csv sweep_roundrobin.csv \\
        --label-a "Weighted Least-Queue" --label-b "Round-Robin"

OUTPUT:
    comparison_graph.png  (or whatever --out specifies)

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


def load(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)
    df = pd.read_csv(p)
    required = {"vus", "throughput_req_per_sec", "async_p95_ms", "async_p99_ms"}
    missing = required - set(df.columns)
    if missing:
        print(f"[ERROR] {path} is missing columns: {missing}")
        sys.exit(1)
    # Keep the last run for each VU level in case the CSV has duplicate rows
    # from multiple sweep runs appended to the same file.
    df = df.sort_values("vus")
    df = df.drop_duplicates(subset="vus", keep="last").reset_index(drop=True)
    return df


def has_cost(df: pd.DataFrame) -> bool:
    return bool("snark_avg_cost_per_job" in df.columns and df["snark_avg_cost_per_job"].notna().any())


def annotate_peak(ax, df, col, color):
    """Mark the peak value with a dotted vertical line and a small label."""
    peak_row = df.loc[df[col].idxmax()]
    ax.axvline(x=peak_row["vus"], color=color, linestyle=":", alpha=0.45, linewidth=1.2)
    ax.annotate(
        f"peak {peak_row[col]:.1f}",
        xy=(peak_row["vus"], peak_row[col]),
        xytext=(6, -14),
        textcoords="offset points",
        fontsize=7,
        color=color,
    )


def add_reference_line(ax, y, label, color="grey"):
    """Draw a horizontal reference line (e.g. theoretical cost floor/ceiling)."""
    ax.axhline(y=y, color=color, linestyle="--", linewidth=0.9, alpha=0.55)
    ax.annotate(
        label,
        xy=(ax.get_xlim()[0], y),
        xytext=(4, 3),
        textcoords="offset points",
        fontsize=7,
        color=color,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Compare two sweep CSVs — weighted vs round-robin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("file_a", help="First CSV  (e.g. sweep_weighted.csv)")
    parser.add_argument("file_b", help="Second CSV (e.g. sweep_roundrobin.csv)")
    parser.add_argument("--label-a", default=None, help="Legend label for file_a")
    parser.add_argument("--label-b", default=None, help="Legend label for file_b")
    parser.add_argument("--out", default="comparison_graph.png",
                        help="Output image filename (default: comparison_graph.png)")
    args = parser.parse_args()

    label_a = args.label_a or Path(args.file_a).stem.replace("_", " ").title()
    label_b = args.label_b or Path(args.file_b).stem.replace("_", " ").title()

    df_a = load(args.file_a)
    df_b = load(args.file_b)

    COLOR_A  = "#2196F3"   # blue  — algorithm A (weighted)
    COLOR_B  = "#F44336"   # red   — algorithm B (round-robin)
    COLOR_REF = "#78909C"  # grey  — reference / theory lines

    show_cost = has_cost(df_a) or has_cost(df_b)
    n_panels  = 3 if show_cost else 2
    fig, axes = plt.subplots(n_panels, 1, figsize=(11, 4 * n_panels), sharex=True)
    fig.suptitle(
        "Routing Algorithm Comparison — Weighted Least-Queue vs Round-Robin\n"
        "50 + 50 Verifiers  |  SNARK Groth16  |  Cost model: 50 % cheap (1.0) / 50 % expensive (2.0)",
        fontsize=11, fontweight="bold", y=0.99,
    )

    # ------------------------------------------------------------------ #
    # Panel 1 — Throughput (req/s)
    # ------------------------------------------------------------------ #
    ax1 = axes[0]
    ax1.plot(df_a["vus"], df_a["throughput_req_per_sec"],          # [OPTIONAL] comment to remove weighted throughput line
             marker="o", color=COLOR_A, label=label_a, linewidth=2)
    ax1.plot(df_b["vus"], df_b["throughput_req_per_sec"],          # [OPTIONAL] comment to remove round-robin throughput line
             marker="s", linestyle="--", color=COLOR_B, label=label_b, linewidth=2)
    annotate_peak(ax1, df_a, "throughput_req_per_sec", COLOR_A)    # [OPTIONAL] comment to remove peak annotation for weighted
    annotate_peak(ax1, df_b, "throughput_req_per_sec", COLOR_B)    # [OPTIONAL] comment to remove peak annotation for round-robin
    ax1.set_ylabel("Throughput (req/s)")
    ax1.legend(loc="lower right")
    ax1.grid(True, alpha=0.2)
    ax1.yaxis.set_minor_locator(ticker.AutoMinorLocator())

    # ------------------------------------------------------------------ #
    # Panel 2 — Latency p95 / p99
    # ------------------------------------------------------------------ #
    ax2 = axes[1]
    ax2.plot(df_a["vus"], df_a["async_p95_ms"],                    # [OPTIONAL] comment to remove weighted p95 line
             marker="o", color=COLOR_A, label=f"{label_a}  p95", linewidth=2)
    ax2.plot(df_b["vus"], df_b["async_p95_ms"],                    # [OPTIONAL] comment to remove round-robin p95 line
             marker="s", linestyle="--", color=COLOR_B, label=f"{label_b}  p95", linewidth=2)
    #ax2.plot(df_a["vus"], df_a["async_p99_ms"],                    # [OPTIONAL] comment to remove weighted p99 line
    #         marker="o", linestyle=":", color=COLOR_A, label=f"{label_a}  p99",
    #         linewidth=1.2, alpha=0.5)
    #ax2.plot(df_b["vus"], df_b["async_p99_ms"],                    # [OPTIONAL] comment to remove round-robin p99 line
    #         marker="s", linestyle=":", color=COLOR_B, label=f"{label_b}  p99",
    #         linewidth=1.2, alpha=0.5)
    ax2.set_ylabel("Latency (ms)")
    ax2.legend(loc="upper left", fontsize=8, ncol=2)
    ax2.grid(True, alpha=0.2)
    ax2.yaxis.set_minor_locator(ticker.AutoMinorLocator())

    if not show_cost:
        ax2.set_xlabel("Virtual Users (VUs)")

    # ------------------------------------------------------------------ #
    # Panel 3 — Average cost per job  (key result)
    # ------------------------------------------------------------------ #
    if show_cost:
        ax3 = axes[2]

        # Theoretical bounds for the [1.0, 2.0] cost model
        COST_FLOOR   = 1.0   # all-cheap routing (theoretical best)
        COST_CEILING = 1.5   # perfectly balanced (round-robin theory)

        if has_cost(df_a):
            ax3.plot(df_a["vus"], df_a["snark_avg_cost_per_job"],  # [OPTIONAL] comment to remove weighted cost line
                     marker="o", color=COLOR_A, label=label_a, linewidth=2)
        if has_cost(df_b):
            ax3.plot(df_b["vus"], df_b["snark_avg_cost_per_job"],  # [OPTIONAL] comment to remove round-robin cost line
                     marker="s", linestyle="--", color=COLOR_B, label=label_b, linewidth=2)

        # Annotate reference lines after plotting so xlim is set
        ax3.set_xlim(ax3.get_xlim())
        add_reference_line(ax3, COST_FLOOR,   "floor = 1.0  (all-cheap)",    COLOR_REF)    # [OPTIONAL] comment to remove floor reference line
        add_reference_line(ax3, COST_CEILING, "ceiling = 1.5 (round-robin theory)", COLOR_REF)  # [OPTIONAL] comment to remove ceiling reference line

        ax3.set_ylabel("Avg cost per job")
        ax3.set_xlabel("Virtual Users (VUs)")
        ax3.set_ylim(0.8, 1.8)
        ax3.legend(loc="upper left")
        ax3.grid(True, alpha=0.2)
        ax3.yaxis.set_minor_locator(ticker.AutoMinorLocator())

        # Shade the "cost saving region" between the two curves
        if has_cost(df_a) and has_cost(df_b):
            merged = pd.merge(
                df_a[["vus", "snark_avg_cost_per_job"]].rename(columns={"snark_avg_cost_per_job": "cost_a"}),
                df_b[["vus", "snark_avg_cost_per_job"]].rename(columns={"snark_avg_cost_per_job": "cost_b"}),
                on="vus",
            ).dropna()
            ax3.fill_between(merged["vus"], merged["cost_a"], merged["cost_b"],  # [OPTIONAL] comment to remove shaded cost-saving region
                             alpha=0.12, color=COLOR_A,
                             label="cost saving (weighted vs RR)")
            ax3.legend(loc="upper left", fontsize=8)

    plt.tight_layout(rect=(0, 0, 1, 0.97))
    out_path = Path(args.out)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved → {out_path.resolve()}")


if __name__ == "__main__":
    main()
