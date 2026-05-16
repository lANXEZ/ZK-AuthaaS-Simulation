"""
Per-domain latency visualizer for the 5-domain shifting-focus load test.

Reads test_results_e2e.csv produced by k6's --out csv=... option.
Produces a single graph: e2e latency p95 per domain over time, all five
domains overlaid so you can clearly see each domain's reaction when it
becomes (or stops being) the hot domain.

Vertical dashed markers are drawn at t = 20, 40, 60, 80 s to show when
the focus switches from one domain to the next.

Usage:
  python visualize_k6_per_app.py
  python visualize_k6_per_app.py --input test_results_e2e.csv --output latency_graph.png
  python visualize_k6_per_app.py --bucket 3          # smoothing window (seconds)
  python visualize_k6_per_app.py --no-markers        # hide focus-switch lines
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import numpy as np

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DOMAIN_COLORS = {
    "0": "#1f77b4",   # blue
    "1": "#ff7f0e",   # orange
    "2": "#2ca02c",   # green
    "3": "#d62728",   # red
    "4": "#9467bd",   # purple
}

DOMAIN_TAG_RE = re.compile(r"(?:^|&)domain=(\d)")

# Times (seconds from test start) at which the focused domain rotates.
# Matches the PHASES array in load_test.js (20 s each domain).
FOCUS_SWITCH_TIMES = [20, 40, 60, 80]   # domain switches at these points
FOCUS_LABELS = {
    0:  "dom 0\nhot",
    20: "dom 1\nhot",
    40: "dom 2\nhot",
    60: "dom 3\nhot",
    80: "dom 4\nhot",
}


# --------------------------------------------------------------------------
# CSV loading
# --------------------------------------------------------------------------
def _extract_domain(extra_tags: str) -> str | None:
    if not extra_tags:
        return None
    m = DOMAIN_TAG_RE.search(extra_tags)
    return m.group(1) if m else None


def load_latency(path: Path) -> dict[str, list[tuple[float, float]]]:
    """
    Returns dict[domain_tag] -> list of (timestamp_sec, latency_ms).
    Only reads e2e_latency rows.
    """
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("metric_name") != "e2e_latency":
                continue
            domain = _extract_domain(row.get("extra_tags", ""))
            if domain is None:
                continue
            try:
                ts  = float(row["timestamp"])
                val = float(row["metric_value"])
            except (KeyError, ValueError):
                continue
            series[domain].append((ts, val))
    return series


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _sort_and_relativize(events: list[tuple[float, float]], t0: float):
    events = sorted(events, key=lambda e: e[0])
    times = np.array([e[0] - t0 for e in events])
    vals  = np.array([e[1]       for e in events])
    return times, vals


def _bin_p95(times, vals, bins):
    """Return p95 latency per time bin (NaN where no data)."""
    bin_idx = np.digitize(times, bins) - 1
    n_bins  = len(bins) - 1
    p95     = np.full(n_bins, np.nan)
    for i in range(n_bins):
        mask = bin_idx == i
        if mask.any():
            p95[i] = np.percentile(vals[mask], 95)
    return p95


# --------------------------------------------------------------------------
# Summary table
# --------------------------------------------------------------------------
def print_summary(series: dict[str, list[tuple[float, float]]]) -> None:
    print("\n=== Per-Domain Latency Summary ===")
    print(f"{'Domain':<8}{'Samples':>10}{'p50 (ms)':>12}{'p95 (ms)':>12}{'p99 (ms)':>12}{'max (ms)':>12}")
    for domain in sorted(series):
        vals = [v for _, v in series[domain]]
        if not vals:
            print(f"{domain:<8}{'—':>10}")
            continue
        arr = np.array(vals)
        print(
            f"{domain:<8}"
            f"{len(arr):>10}"
            f"{np.percentile(arr, 50):>12.1f}"
            f"{np.percentile(arr, 95):>12.1f}"
            f"{np.percentile(arr, 99):>12.1f}"
            f"{arr.max():>12.1f}"
        )
    print()


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------
def plot_latency(
    series: dict[str, list[tuple[float, float]]],
    output: Path,
    bucket_sec: float = 2.0,
    show_markers: bool = True,
) -> None:
    all_events = [e for dom_events in series.values() for e in dom_events]
    if not all_events:
        print("[WARN] No e2e_latency data found — nothing to plot.")
        return

    t0    = min(e[0] for e in all_events)
    t_max = max(e[0] for e in all_events) - t0

    bins    = np.arange(0, t_max + bucket_sec, bucket_sec)
    centers = (bins[:-1] + bins[1:]) / 2

    fig, ax = plt.subplots(figsize=(13, 6))

    for domain in sorted(series):
        times, vals = _sort_and_relativize(series[domain], t0)
        p95 = _bin_p95(times, vals, bins)
        ax.plot(
            centers, p95,
            label=f"Domain {domain}",
            color=DOMAIN_COLORS.get(domain, "gray"),
            linewidth=2.0,
        )

    # Focus-switch vertical markers
    if show_markers:
        y_top = ax.get_ylim()[1]
        for t_switch in FOCUS_SWITCH_TIMES:
            if t_switch <= t_max:
                ax.axvline(t_switch, color="black", linestyle="--", linewidth=0.8, alpha=0.5)

        # Annotate focus regions at the top of the plot
        region_starts = [0] + FOCUS_SWITCH_TIMES
        for idx, t_start in enumerate(region_starts):
            t_end = FOCUS_SWITCH_TIMES[idx] if idx < len(FOCUS_SWITCH_TIMES) else t_max
            if t_start > t_max:
                break
            t_end = min(t_end, t_max)
            mid   = (t_start + t_end) / 2
            label = f"Dom {idx}\nhot"
            ax.text(
                mid, 1.01, label,
                transform=ax.get_xaxis_transform(),
                ha="center", va="bottom",
                fontsize=8, color=DOMAIN_COLORS.get(str(idx), "gray"),
                fontweight="bold",
            )

    ax.set_xlabel("Time since test start (s)", fontsize=11)
    ax.set_ylabel("Latency p95 (ms)", fontsize=11)
    ax.set_title(
        "Per-Domain E2E Latency p95 — Shifting Focus (20 s per domain)",
        fontsize=13, fontweight="bold",
    )
    ax.legend(loc="upper right", fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(left=0)

    fig.tight_layout()
    fig.savefig(output, dpi=150)
    print(f"Saved {output}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Visualize per-domain latency from k6 CSV output.")
    ap.add_argument("--input",      default="test_results_e2e.csv", help="k6 CSV file")
    ap.add_argument("--output",     default="latency_graph.png",    help="Output PNG path")
    ap.add_argument("--bucket",     type=float, default=2.0,        help="Bin width in seconds (smoothing)")
    ap.add_argument("--no-markers", action="store_true",            help="Hide focus-switch vertical lines")
    args = ap.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"[ERROR] {path} not found — run k6 with --out csv={args.input} first.")
        return

    print(f"Loading {path} ...")
    series = load_latency(path)

    print_summary(series)
    plot_latency(series, Path(args.output), bucket_sec=args.bucket, show_markers=not args.no_markers)


if __name__ == "__main__":
    main()
