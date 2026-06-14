"""
Per-domain THROUGHPUT visualizer for the 5-domain shifting-focus load test
(Track 4 — throughput scalability sweep).

Reads a k6 CSV produced with --out csv=... and plots completed
verifications per second, per domain, over time — all five domains
overlaid in the same style as the latency graph from Tracks 2/3
(visualize_k6_per_app.py). Vertical dashed markers at t = 20/40/60/80 s
show when the hot domain rotates.

Throughput is computed from the `completed_by_domain` counter that both
load_test.js and load_test_onprem.js emit: each completed iteration adds
one sample tagged domain=N, so summing samples per time bucket and
dividing by the bucket width gives completions/s.

Usage:
  python visualize_k6_throughput_per_app.py --input test_results_e2e_thr_r1.csv --output e2e_thr_r1_throughput_graph.png
  python visualize_k6_throughput_per_app.py --bucket 4          # smoothing window (seconds)
  python visualize_k6_throughput_per_app.py --no-markers        # hide focus-switch lines
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# --------------------------------------------------------------------------
# Config (matches visualize_k6_per_app.py)
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
# Matches the PHASES array in load_test.js / load_test_onprem.js (20 s each).
PHASE_LEN = 20
FOCUS_SWITCH_TIMES = [20, 40, 60, 80]


# --------------------------------------------------------------------------
# CSV loading
# --------------------------------------------------------------------------
def _extract_domain(extra_tags: str) -> str | None:
    if not extra_tags:
        return None
    m = DOMAIN_TAG_RE.search(extra_tags)
    return m.group(1) if m else None


def load_series(path: Path):
    """
    Returns (completed, t0):
      completed: dict[domain] -> list of (timestamp_sec, count) from
                 completed_by_domain counter samples.
      t0: test-start reference — the earliest submits_by_domain or
          completed_by_domain sample, so bucket times align with the
          phase windows even though the first completion lags the
          first submit by one verification latency.
    """
    completed: dict[str, list[tuple[float, float]]] = defaultdict(list)
    t0 = None
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            metric = row.get("metric_name")
            if metric not in ("completed_by_domain", "submits_by_domain"):
                continue
            try:
                ts = float(row["timestamp"])
            except (KeyError, ValueError):
                continue
            if t0 is None or ts < t0:
                t0 = ts
            if metric != "completed_by_domain":
                continue
            domain = _extract_domain(row.get("extra_tags", ""))
            if domain is None:
                continue
            try:
                val = float(row["metric_value"])
            except (KeyError, ValueError):
                continue
            completed[domain].append((ts, val))
    return completed, t0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _bin_rate(times, vals, bins, bucket_sec):
    """Completions/s per time bin (0 where no data)."""
    bin_idx = np.digitize(times, bins) - 1
    n_bins = len(bins) - 1
    rate = np.zeros(n_bins)
    for i in range(n_bins):
        mask = bin_idx == i
        if mask.any():
            rate[i] = vals[mask].sum() / bucket_sec
    return rate


# --------------------------------------------------------------------------
# Summary table
# --------------------------------------------------------------------------
def print_summary(completed, t0) -> None:
    all_events = [e for dom in completed.values() for e in dom]
    if not all_events:
        return
    t_max = max(e[0] for e in all_events) - t0
    span = max(t_max, 1e-9)

    print("\n=== Per-Domain Throughput Summary ===")
    print(f"{'Domain':<8}{'Completed':>12}{'Avg (c/s)':>12}{'Hot win (c/s)':>16}{'Cold avg (c/s)':>16}")
    grand_total = 0.0
    for domain in sorted(completed):
        events = completed[domain]
        total = sum(v for _, v in events)
        grand_total += total
        # Hot window for domain N is [20*N, 20*N + 20) relative to test start
        n = int(domain)
        hot_lo, hot_hi = n * PHASE_LEN, (n + 1) * PHASE_LEN
        hot = sum(v for ts, v in events if hot_lo <= (ts - t0) < hot_hi)
        hot_rate = hot / PHASE_LEN
        cold_span = span - PHASE_LEN
        cold_rate = (total - hot) / cold_span if cold_span > 1e-9 else float("nan")
        print(f"{domain:<8}{total:>12.0f}{total / span:>12.1f}{hot_rate:>16.1f}{cold_rate:>16.1f}")
    print(f"{'TOTAL':<8}{grand_total:>12.0f}{grand_total / span:>12.1f}")
    print(f"\nObserved span: {span:.1f} s  |  total system throughput: {grand_total / span:.1f} completions/s\n")


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------
def plot_throughput(completed, t0, output: Path, bucket_sec: float = 2.0, show_markers: bool = True) -> None:
    all_events = [e for dom in completed.values() for e in dom]
    if not all_events:
        print("[WARN] No completed_by_domain data found — nothing to plot.")
        return

    t_max = max(e[0] for e in all_events) - t0
    bins = np.arange(0, t_max + bucket_sec, bucket_sec)
    centers = (bins[:-1] + bins[1:]) / 2

    fig, ax = plt.subplots(figsize=(13, 6))

    for domain in sorted(completed):
        events = sorted(completed[domain], key=lambda e: e[0])
        times = np.array([e[0] - t0 for e in events])
        vals = np.array([e[1] for e in events])
        rate = _bin_rate(times, vals, bins, bucket_sec)
        ax.plot(
            centers, rate,
            label=f"Domain {domain}",
            color=DOMAIN_COLORS.get(domain, "gray"),
            linewidth=2.0,
        )

    # Focus-switch vertical markers
    if show_markers:
        for t_switch in FOCUS_SWITCH_TIMES:
            if t_switch <= t_max:
                ax.axvline(t_switch, color="black", linestyle="--", linewidth=0.8, alpha=0.5)

        # Annotate focus regions below the x-axis (under the tick labels)
        region_starts = [0] + FOCUS_SWITCH_TIMES
        for idx, t_start in enumerate(region_starts):
            t_end = FOCUS_SWITCH_TIMES[idx] if idx < len(FOCUS_SWITCH_TIMES) else t_max
            if t_start > t_max:
                break
            t_end = min(t_end, t_max)
            mid = (t_start + t_end) / 2
            ax.text(
                mid, -0.12, f"Dom {idx}\nhot",
                transform=ax.get_xaxis_transform(),
                ha="center", va="top",
                fontsize=8, color=DOMAIN_COLORS.get(str(idx), "gray"),
                fontweight="bold",
            )

    ax.set_xlabel("Time since test start (s)", fontsize=11)
    ax.set_ylabel("Throughput (completed verifications/s)", fontsize=11)
    ax.set_title(
        "Per-Domain Throughput — Shifting Focus (20 s per domain)",
        fontsize=13, fontweight="bold",
    )
    ax.legend(loc="upper right", fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    fig.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Saved {output}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Visualize per-domain throughput from k6 CSV output.")
    ap.add_argument("--input",      default="test_results_e2e.csv",  help="k6 CSV file")
    ap.add_argument("--output",     default="throughput_graph.png",  help="Output PNG path")
    ap.add_argument("--bucket",     type=float, default=2.0,         help="Bin width in seconds (smoothing)")
    ap.add_argument("--no-markers", action="store_true",             help="Hide focus-switch vertical lines")
    args = ap.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"[ERROR] {path} not found — run k6 with --out csv={args.input} first.")
        return

    print(f"Loading {path} ...")
    completed, t0 = load_series(path)
    if t0 is None:
        print("[ERROR] No submits_by_domain/completed_by_domain rows found in the CSV.")
        return

    print_summary(completed, t0)
    plot_throughput(completed, t0, Path(args.output), bucket_sec=args.bucket, show_markers=not args.no_markers)


if __name__ == "__main__":
    main()
