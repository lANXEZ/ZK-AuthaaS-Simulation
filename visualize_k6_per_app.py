"""
Per-domain throughput & latency visualizer for the 5-domain E2E load test.

Reads test_results_e2e.csv produced by k6's --out csv=... option.
Groups metrics by the `domain=N` tag (N=0..4) and plots:
  1. Throughput per domain over time (completed_by_domain counter)
  2. Latency per domain over time (e2e_latency trend, p95)

Usage:
  python visualize_k6_per_app.py
  python visualize_k6_per_app.py --input test_results_e2e.csv --output per_domain_graph.png
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

DOMAIN_COLORS = {
    "0": "#1f77b4",  # blue
    "1": "#ff7f0e",  # orange
    "2": "#2ca02c",  # green
    "3": "#d62728",  # red
    "4": "#9467bd",  # purple
}

DOMAIN_TAG_RE = re.compile(r"(?:^|&)domain=(\d)")

METRICS_OF_INTEREST = {
    "e2e_latency",
    "completed_by_domain",
    "submits_by_domain",
    "failed_by_domain",
}


def _extract_domain_tag(extra_tags_str: str) -> str | None:
    """k6 CSV stores custom tags in `extra_tags` as `key=value&key=value`."""
    if not extra_tags_str:
        return None
    m = DOMAIN_TAG_RE.search(extra_tags_str)
    return m.group(1) if m else None


def load_csv(path: Path):
    """Returns: dict[metric_name][domain_tag] -> list of (timestamp_sec, value)."""
    series = defaultdict(lambda: defaultdict(list))
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            metric = row.get("metric_name")
            if metric not in METRICS_OF_INTEREST:
                continue
            domain = _extract_domain_tag(row.get("extra_tags", ""))
            if domain is None:
                continue
            try:
                ts = float(row["timestamp"])
                val = float(row["metric_value"])
            except (KeyError, ValueError):
                continue
            series[metric][domain].append((ts, val))
    return series


def _normalize_time(events):
    """Returns (rel_seconds_array, values_array). Sorted by time."""
    events = sorted(events, key=lambda e: e[0])
    if not events:
        return np.array([]), np.array([])
    t0 = events[0][0]
    times = np.array([e[0] - t0 for e in events])
    vals = np.array([e[1] for e in events])
    return times, vals


def plot_throughput(ax, series, bucket_sec=1.0):
    """Overlaid line plot: completed jobs per second, per domain."""
    completed = series.get("completed_by_domain", {})
    if not completed:
        ax.set_title("No completed_by_domain data found")
        return

    all_events = [e for domain_events in completed.values() for e in domain_events]
    if not all_events:
        return
    t0 = min(e[0] for e in all_events)
    t_max = max(e[0] for e in all_events) - t0

    bins = np.arange(0, t_max + bucket_sec, bucket_sec)
    centers = (bins[:-1] + bins[1:]) / 2

    for domain in sorted(completed.keys()):
        times, vals = _normalize_time(completed[domain])
        counts, _ = np.histogram(times, bins=bins, weights=vals)
        rate = counts / bucket_sec  # per second
        ax.plot(centers, rate, label=f"Domain {domain}", color=DOMAIN_COLORS.get(domain, "gray"), linewidth=1.5)

    ax.set_xlabel("Time since test start (s)")
    ax.set_ylabel("Throughput (completed jobs / s)")
    ax.set_title("Per-Domain Throughput Over Time")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)


def plot_latency(ax, series, bucket_sec=2.0):
    """Rolling p95 latency per domain."""
    latency = series.get("e2e_latency", {})
    if not latency:
        ax.set_title("No e2e_latency data found")
        return

    all_events = [e for domain_events in latency.values() for e in domain_events]
    t0 = min(e[0] for e in all_events)
    t_max = max(e[0] for e in all_events) - t0

    bins = np.arange(0, t_max + bucket_sec, bucket_sec)
    centers = (bins[:-1] + bins[1:]) / 2

    for domain in sorted(latency.keys()):
        times, vals = _normalize_time(latency[domain])
        if len(times) == 0:
            continue
        bin_idx = np.digitize(times, bins) - 1
        p95 = np.full(len(centers), np.nan)
        for i in range(len(centers)):
            mask = bin_idx == i
            if mask.any():
                p95[i] = np.percentile(vals[mask], 95)
        ax.plot(centers, p95, label=f"Domain {domain} p95", color=DOMAIN_COLORS.get(domain, "gray"), linewidth=1.5)

    ax.set_xlabel("Time since test start (s)")
    ax.set_ylabel("Latency p95 (ms)")
    ax.set_title("Per-Domain E2E Latency (p95) Over Time")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)


def print_summary(series):
    """Print totals + summary latency stats per domain."""
    completed = series.get("completed_by_domain", {})
    failed = series.get("failed_by_domain", {})
    latency = series.get("e2e_latency", {})

    print("\n=== Per-Domain Summary ===")
    print(f"{'Domain':<8}{'Completed':>12}{'Failed':>10}{'p50 (ms)':>12}{'p95 (ms)':>12}{'p99 (ms)':>12}")
    for domain in sorted(set(completed.keys()) | set(failed.keys()) | set(latency.keys())):
        n_done = int(sum(v for _, v in completed.get(domain, [])))
        n_fail = int(sum(v for _, v in failed.get(domain, [])))
        lat_vals = [v for _, v in latency.get(domain, [])]
        if lat_vals:
            p50 = np.percentile(lat_vals, 50)
            p95 = np.percentile(lat_vals, 95)
            p99 = np.percentile(lat_vals, 99)
            print(f"{domain:<8}{n_done:>12}{n_fail:>10}{p50:>12.1f}{p95:>12.1f}{p99:>12.1f}")
        else:
            print(f"{domain:<8}{n_done:>12}{n_fail:>10}{'—':>12}{'—':>12}{'—':>12}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="test_results_e2e.csv")
    ap.add_argument("--output", default="per_domain_graph.png")
    args = ap.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"[ERROR] {path} not found")
        return

    print(f"Loading {path} ...")
    series = load_csv(path)

    print_summary(series)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    plot_throughput(axes[0], series)
    plot_latency(axes[1], series)
    fig.tight_layout()
    fig.savefig(args.output, dpi=130)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
