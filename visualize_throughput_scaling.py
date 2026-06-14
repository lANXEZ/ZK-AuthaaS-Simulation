"""
Throughput-vs-vCPU scaling curve for Track 4 — the headline graph of the
throughput-scalability sweep.

Takes the 4 E2E (service) CSVs and the 4 on-prem CSVs — one per vCPU
budget round — and plots two panels:

  Left:  TOTAL system throughput vs total vCPU budget
  Right: HOT-DOMAIN throughput vs total vCPU budget — the average
         completions/s a domain sustains during its own 20 s hot window
         (when it receives 60% of traffic). This is where pooled vs
         partitioned verifiers diverge hardest.

Each panel has two lines: Service (E2E) and On-prem.

Usage:
  python visualize_throughput_scaling.py \
    --budgets 10,20,40,60 \
    --e2e    test_results_e2e_thr_r1.csv,test_results_e2e_thr_r2.csv,test_results_e2e_thr_r3.csv,test_results_e2e_thr_r4.csv \
    --onprem test_results_onprem_thr_r1.csv,test_results_onprem_thr_r2.csv,test_results_onprem_thr_r3.csv,test_results_onprem_thr_r4.csv \
    --output throughput_scaling_graph.png
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

DOMAIN_TAG_RE = re.compile(r"(?:^|&)domain=(\d)")
PHASE_LEN = 20          # seconds per hot-domain window (matches PHASES in the k6 scripts)
N_DOMAINS = 5


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------
def _extract_domain(extra_tags: str) -> str | None:
    if not extra_tags:
        return None
    m = DOMAIN_TAG_RE.search(extra_tags)
    return m.group(1) if m else None


def analyze_csv(path: Path) -> dict:
    """
    Returns {"total_rate": completions/s over the whole run,
             "hot_rate":   avg completions/s of the hot domain during its
                           own 20 s window (averaged across all 5 windows)}.
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

    all_events = [e for dom in completed.values() for e in dom]
    if not all_events or t0 is None:
        raise ValueError(f"{path}: no completed_by_domain samples found")

    span = max(max(e[0] for e in all_events) - t0, 1e-9)
    grand_total = sum(v for _, v in all_events)

    # Hot-domain completions: domain N's samples inside [20N, 20N+20)
    hot_total = 0.0
    for domain, events in completed.items():
        n = int(domain)
        lo, hi = n * PHASE_LEN, (n + 1) * PHASE_LEN
        hot_total += sum(v for ts, v in events if lo <= (ts - t0) < hi)

    return {
        "total_rate": grand_total / span,
        "hot_rate": hot_total / (N_DOMAINS * PHASE_LEN),
    }


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------
def plot_scaling(budgets, e2e_stats, onprem_stats, output: Path) -> None:
    fig, (ax_total, ax_hot) = plt.subplots(1, 2, figsize=(14, 6))

    styles = [
        ("Service (E2E)", e2e_stats, "#1f77b4", "o"),
        ("On-prem",       onprem_stats, "#d62728", "s"),
    ]

    for ax, key, title, ylabel in [
        (ax_total, "total_rate", "Total System Throughput",      "Throughput (completions/s)"),
        (ax_hot,   "hot_rate",   "Hot-Domain Throughput\n(avg during its 20 s / 60 % window)", "Hot-domain throughput (completions/s)"),
    ]:
        for label, stats, color, marker in styles:
            ys = [s[key] for s in stats]
            ax.plot(budgets, ys, label=label, color=color, marker=marker,
                    linewidth=2.0, markersize=7)
            for x, y in zip(budgets, ys):
                ax.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                            xytext=(0, 8), ha="center", fontsize=8, color=color)
        ax.set_xlabel("Total vCPU budget", fontsize=11)
        ax.set_ylabel(ylabel, fontsize=11)
        ax.set_title(title, fontsize=12, fontweight="bold")
        ax.set_xticks(budgets)
        ax.grid(True, alpha=0.3)
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left", fontsize=10)

    fig.suptitle("Service vs On-Prem Throughput Scaling — same budget, same verifier cores",
                 fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Saved {output}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Plot throughput vs vCPU budget for service vs on-prem.")
    ap.add_argument("--budgets", default="10,20,40,60",
                    help="Comma-separated vCPU budgets, one per round")
    ap.add_argument("--e2e", required=True,
                    help="Comma-separated E2E CSVs, in the same order as --budgets")
    ap.add_argument("--onprem", required=True,
                    help="Comma-separated on-prem CSVs, in the same order as --budgets")
    ap.add_argument("--output", default="throughput_scaling_graph.png", help="Output PNG path")
    args = ap.parse_args()

    budgets = [float(b) for b in args.budgets.split(",")]
    e2e_paths = [Path(p.strip()) for p in args.e2e.split(",")]
    onprem_paths = [Path(p.strip()) for p in args.onprem.split(",")]

    if not (len(budgets) == len(e2e_paths) == len(onprem_paths)):
        raise SystemExit("[ERROR] --budgets, --e2e and --onprem must all have the same number of entries.")
    for p in e2e_paths + onprem_paths:
        if not p.exists():
            raise SystemExit(f"[ERROR] {p} not found.")

    e2e_stats = [analyze_csv(p) for p in e2e_paths]
    onprem_stats = [analyze_csv(p) for p in onprem_paths]

    print("\n=== Throughput Scaling Summary ===")
    header = (f"{'Budget':>8}{'E2E total':>12}{'OnPrem total':>14}{'ratio':>8}"
              f"{'E2E hot':>12}{'OnPrem hot':>13}{'ratio':>8}")
    print(header)
    for b, e, o in zip(budgets, e2e_stats, onprem_stats):
        rt = e["total_rate"] / o["total_rate"] if o["total_rate"] > 0 else float("nan")
        rh = e["hot_rate"] / o["hot_rate"] if o["hot_rate"] > 0 else float("nan")
        print(f"{b:>8.0f}{e['total_rate']:>12.1f}{o['total_rate']:>14.1f}{rt:>8.2f}"
              f"{e['hot_rate']:>12.1f}{o['hot_rate']:>13.1f}{rh:>8.2f}")
    print()

    plot_scaling(budgets, e2e_stats, onprem_stats, Path(args.output))


if __name__ == "__main__":
    main()
