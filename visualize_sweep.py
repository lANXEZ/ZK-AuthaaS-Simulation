#!/usr/bin/env python3
"""
Plot the throughput sweep results produced by sweep_throughput.py.

Reads sweep_results.csv and generates a figure with two stacked panels:
  - Top panel: throughput (req/s) vs VUs, with p50/p95/p99 latency overlaid
    on a secondary y-axis. The VU level with peak throughput is annotated -
    that's your "knee" / optimal operating point.
  - Bottom panel: failure counts (verification + submit) per VU level.

USAGE:
    python visualize_sweep.py
    python visualize_sweep.py --input sweep_results.csv --output my_graph.png
    python visualize_sweep.py --show          # also open the plot interactively

REQUIREMENTS:
    pip install pandas matplotlib
"""

import argparse
import sys
from pathlib import Path

try:
    import pandas as pd
    import matplotlib.pyplot as plt
except ImportError:
    print("ERROR: Missing dependencies. Install with: pip install pandas matplotlib")
    sys.exit(1)


def plot_sweep(csv_path, output_path, show=False):
    df = pd.read_csv(csv_path)
    if df.empty:
        print(f"[ERROR] {csv_path} is empty - run sweep_throughput.py first")
        return

    # Sort by VUs so lines are drawn left-to-right
    df = df.sort_values('vus').reset_index(drop=True)

    # Locate the knee - the VU level with peak throughput.
    knee_pos = int(df['throughput_req_per_sec'].to_numpy().argmax())
    knee_vus = int(df['vus'].iloc[knee_pos])
    knee_throughput = float(df['throughput_req_per_sec'].iloc[knee_pos])
    knee_p95 = float(df['async_p95_ms'].iloc[knee_pos])

    # Figure layout: top panel 3x the height of the bottom
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1,
        figsize=(11, 7),
        gridspec_kw={'height_ratios': [3, 1]},
        sharex=True,
    )

    # ----------------------------------------
    # Top panel: throughput (left) + latency (right)
    # ----------------------------------------
    color_tp = '#1f77b4'
    ax_top.set_ylabel('Throughput (req/s)', color=color_tp, fontsize=11)
    ax_top.plot(                                                   # [OPTIONAL] comment these 5 lines to remove throughput line
        df['vus'], df['throughput_req_per_sec'],
        marker='o', color=color_tp, linewidth=2.5,
        label='Throughput', zorder=3,
    )
    ax_top.tick_params(axis='y', labelcolor=color_tp)
    ax_top.grid(True, alpha=0.3, zorder=0)

    # Secondary axis for latency
    ax_lat = ax_top.twinx()
    ax_lat.set_ylabel('Latency (ms)', color='#d62728', fontsize=11)
    ax_lat.plot(df['vus'], df['async_p50_ms'],                     # [OPTIONAL] comment to remove p50 latency line
                marker='s', linestyle='--', color='#2ca02c',
                alpha=0.75, label='p50 latency')
    ax_lat.plot(df['vus'], df['async_p95_ms'],                     # [OPTIONAL] comment to remove p95 latency line
                marker='^', linestyle='--', color='#ff7f0e',
                alpha=0.75, label='p95 latency')
    ax_lat.plot(df['vus'], df['async_p99_ms'],                     # [OPTIONAL] comment to remove p99 latency line
                marker='v', linestyle='--', color='#d62728',
                alpha=0.75, label='p99 latency')
    ax_lat.tick_params(axis='y', labelcolor='#d62728')

    ax_top.annotate(                                               # [OPTIONAL] comment these 10 lines to remove peak/knee annotation
        f'Peak throughput\n{knee_throughput:.1f} req/s @ VUs={knee_vus}\n'
        f'(p95 = {knee_p95:.0f} ms here)',
        xy=(knee_vus, knee_throughput),
        xytext=(0.55, 0.25),
        textcoords='axes fraction',
        arrowprops=dict(arrowstyle='->', color='black', alpha=0.6),
        fontsize=9,
        bbox=dict(boxstyle='round,pad=0.4',
                  facecolor='lightyellow', edgecolor='gray', alpha=0.9),
    )

    # Combine legends from both axes
    lines1, labels1 = ax_top.get_legend_handles_labels()
    lines2, labels2 = ax_lat.get_legend_handles_labels()
    ax_top.legend(lines1 + lines2, labels1 + labels2,
                  loc='upper left', fontsize=9, framealpha=0.9)

    # Title reflects the test configuration
    stark_ratio = df['stark_ratio'].iloc[0] if 'stark_ratio' in df.columns else None
    title = "ZK-AuthaaS Throughput Sweep"
    if stark_ratio is not None:
        if stark_ratio == 0.0:
            title += "  —  all SNARK"
        elif stark_ratio == 1.0:
            title += "  —  all STARK"
        else:
            title += f"  —  STARK ratio: {stark_ratio}"
    ax_top.set_title(title, fontsize=13, fontweight='bold')

    # ----------------------------------------
    # Bottom panel: failures
    # ----------------------------------------
    x = [int(v) for v in df['vus'].tolist()]
    failed = [int(v) for v in df['failed_verifications'].tolist()]
    submits = [int(v) for v in df['submit_failures'].tolist()]
    x_max = max(x)
    x_min = min(x) if x else 1

    base_width = 0.4
    bar_width = base_width * (x_max * 0.01 + 1) if len(x) > 1 else 1.0

    x_left = [v - base_width / 2 for v in x]
    x_right = [v + base_width / 2 for v in x]

    ax_bot.bar(x_left, failed, width=bar_width,                    # [OPTIONAL] comment to remove verification-failure bars
               color='#d62728', alpha=0.7, label='Verification failures')
    ax_bot.bar(x_right, submits, width=bar_width,                  # [OPTIONAL] comment to remove submit-failure bars
               color='#9467bd', alpha=0.7, label='Submit failures')
    ax_bot.set_xlabel('Virtual Users (VUs)', fontsize=11)
    ax_bot.set_ylabel('Failures (count)', fontsize=11)
    ax_bot.grid(True, alpha=0.3, axis='y')
    ax_bot.legend(loc='upper left', fontsize=9)

    # Use log scale on X-axis if the range is wide
    if x_max / max(x_min, 1) > 20:                                # [OPTIONAL] comment these 3 lines to force linear X-axis always
        ax_top.set_xscale('log')
        ax_bot.set_xscale('log')

    # Make sure VU ticks show actual values
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels([str(v) for v in x], rotation=0)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"[OK] Saved: {output_path}")
    print(f"     Peak: {knee_throughput:.1f} req/s at VUs={knee_vus} "
          f"(p95 latency there = {knee_p95:.0f} ms)")

    if show:
        plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="Plot throughput sweep graph from sweep_throughput.py output"
    )
    parser.add_argument('--input', default='sweep_results.csv',
                        help='Input sweep CSV (default: sweep_results.csv)')
    parser.add_argument('--output', default='sweep_throughput_graph.png',
                        help='Output PNG path (default: sweep_throughput_graph.png)')
    parser.add_argument('--show', action='store_true',
                        help='Also open the graph in an interactive window')
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"[ERROR] {args.input} not found. Run sweep_throughput.py first.")
        sys.exit(1)

    plot_sweep(args.input, args.output, show=args.show)


if __name__ == '__main__':
    main()
