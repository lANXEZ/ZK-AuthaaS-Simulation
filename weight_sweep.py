#!/usr/bin/env python3
"""
Cost-Weight Sweep for ZK-AuthaaS Verifier Selector
====================================================
Iterates over a range of --snark-cost-weight values, updates the live selector
via POST /admin/set-weight (no Docker restart needed), runs a fixed k6 load
test at each weight, and records throughput + average cost per job.

The selector's cost-weighted routing formula is:
    score(i) = queue_depth(i) + cost(i) * SNARK_COST_WEIGHT

A weight of 0 → pure least-queue (ignores cost).
A very high weight → always route to the cheapest node regardless of queue depth.
The sweet spot maximises throughput while keeping avg cost near 1.0 (all-cheap).

USAGE:
    # Default sweep on local stack (weights 0 → 50):
    python weight_sweep.py

    # Custom weight list:
    python weight_sweep.py --weights 0,1,2,5,10,20,50,100

    # Against EC2 (runs entirely on k6 machine — no Docker access needed):
    python weight_sweep.py --target <backend-private-ip>

    # Fixed VU count (default: 200):
    python weight_sweep.py --vus 400

OUTPUT:
    weight_sweep_results.csv — one row per weight value

REQUIREMENTS:
    - k6 must be on PATH
    - ZK-AuthaaS stack must be running and reachable at --target:--port
    - Python 3.7+
"""

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ------------------------------------------
# Defaults
# ------------------------------------------
DEFAULT_WEIGHTS    = [0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50]
DEFAULT_VUS        = 200
DEFAULT_ITERATIONS = 2000   # fixed iterations (not per-VU) so each run is comparable
DEFAULT_COOLDOWN   = 15     # seconds to let queues drain between runs
WEIGHT_CSV         = "weight_sweep_results.csv"
TEMP_SUMMARY       = "_weight_sweep_summary.json"


# ------------------------------------------
# Helpers
# ------------------------------------------
def fetch_cost_stats(target, port):
    try:
        url = f"http://{target}:{port}/stats/cost"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def cost_delta(before, after):
    if before is None or after is None:
        return {"snark_avg_cost_per_job": None}
    snark_jobs = max(after.get("snark_total_jobs", 0) - before.get("snark_total_jobs", 0), 1)
    snark_cost = after.get("snark_total_cost", 0) - before.get("snark_total_cost", 0)
    return {"snark_avg_cost_per_job": round(snark_cost / snark_jobs, 4)}


def set_weight_via_api(target, port, snark_weight, stark_weight=1.0):
    """
    Change the selector's routing weight live via POST /admin/set-weight.
    No Docker restart — the selector picks up the new value on its next job dispatch.
    Returns the confirmed weights from the response, or None on failure.
    """
    try:
        url = f"http://{target}:{port}/admin/set-weight?snark={snark_weight}&stark={stark_weight}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            result = json.loads(resp.read())
            print(f"  [api] Weight set: snark_cost_weight={result.get('snark_cost_weight')}")
            return result
    except Exception as e:
        print(f"  [WARN] POST /admin/set-weight failed: {e}")
        return None


def run_k6(vus, iterations, target, port, stark_ratio, load_script):
    if Path(TEMP_SUMMARY).exists():
        Path(TEMP_SUMMARY).unlink()

    cmd = [
        "k6", "run",
        "-e", f"TARGET={target}",
        "-e", f"PORT={port}",
        "-e", f"VUS={vus}",
        "-e", f"ITERATIONS={iterations}",
        "-e", f"STARK_RATIO={stark_ratio}",
        "--summary-export", TEMP_SUMMARY,
        load_script,
    ]
    print(f"  [k6] VUs={vus}  iterations={iterations}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  [WARN] k6 exited with code {result.returncode}")

    if not Path(TEMP_SUMMARY).exists():
        print(f"  [ERROR] {TEMP_SUMMARY} not produced; skipping")
        return None

    with open(TEMP_SUMMARY) as f:
        return json.load(f)


def get(metric, key, default=0):
    if not metric:
        return default
    return metric.get(key, default)


def extract_metrics(summary, weight, vus, iterations):
    metrics = summary.get("metrics", {})
    iter_metric  = metrics.get("iterations", {})
    async_trend  = metrics.get("async_verification_time", {})

    failed = get(metrics.get("failed_verifications"), "count", 0)

    return {
        "snark_cost_weight":       weight,
        "vus":                     vus,
        "target_iterations":       iterations,
        "completed_iterations":    int(get(iter_metric, "count", 0)),
        "throughput_req_per_sec":  round(get(iter_metric, "rate", 0.0), 2),
        "async_avg_ms":            round(get(async_trend, "avg", 0), 1),
        "async_p95_ms":            round(get(async_trend, "p(95)", 0), 1),
        "async_p99_ms":            round(get(async_trend, "p(99)", 0), 1),
        "failed_verifications":    int(failed),
    }


def append_row(row, csv_path):
    write_header = not Path(csv_path).exists() or Path(csv_path).stat().st_size == 0
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ------------------------------------------
# Main
# ------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sweep --snark-cost-weight to find optimal routing weight",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--weights",
                        default=",".join(str(w) for w in DEFAULT_WEIGHTS),
                        help=f"Comma-separated weight values to test (default: {DEFAULT_WEIGHTS})")
    parser.add_argument("--vus", type=int, default=DEFAULT_VUS,
                        help=f"Fixed VU count for every run (default: {DEFAULT_VUS})")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                        help=f"Fixed total iterations per run (default: {DEFAULT_ITERATIONS})")
    parser.add_argument("--target", default="localhost",
                        help="Host/IP of the request-handler (default: localhost)")
    parser.add_argument("--port", default="8000",
                        help="Port of the request-handler (default: 8000)")
    parser.add_argument("--stark-ratio", default="0.0",
                        help="0.0=all SNARK, 1.0=all STARK (default: 0.0)")
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN,
                        help=f"Seconds between runs (default: {DEFAULT_COOLDOWN})")
    parser.add_argument("--output", default=WEIGHT_CSV,
                        help=f"Output CSV path (default: {WEIGHT_CSV})")
    parser.add_argument("--script", default="load_test.js",
                        help="k6 load script path (default: load_test.js)")
    parser.add_argument("--clean", action="store_true",
                        help="Delete existing output CSV before starting")
    args = parser.parse_args()

    try:
        weights = [float(w.strip()) for w in args.weights.split(",") if w.strip()]
    except ValueError:
        print(f"[ERROR] --weights must be comma-separated numbers; got: {args.weights}")
        sys.exit(1)

    try:
        subprocess.run(["k6", "--version"], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("[ERROR] k6 not found on PATH.")
        sys.exit(1)

    if not Path(args.script).exists():
        print(f"[ERROR] Load script not found: {args.script}")
        sys.exit(1)

    if args.clean and Path(args.output).exists():
        Path(args.output).unlink()
        print(f"Removed existing {args.output}")

    print("\n" + "=" * 60)
    print("WEIGHT SWEEP PLAN")
    print("=" * 60)
    print(f"  Target:       {args.target}:{args.port}")
    print(f"  Weights:      {weights}")
    print(f"  VUs:          {args.vus}  (fixed)")
    print(f"  Iterations:   {args.iterations}  (fixed per run)")
    print(f"  Cooldown:     {args.cooldown}s")
    print(f"  Weight update: POST /admin/set-weight (live, no restart)")
    print(f"  Output:       {args.output}")
    print()

    for i, weight in enumerate(weights):
        print(f"\n{'=' * 60}")
        print(f"WEIGHT {weight}  ({i+1}/{len(weights)})")
        print(f"{'=' * 60}")

        set_weight_via_api(args.target, args.port, weight)

        cost_before = fetch_cost_stats(args.target, args.port)
        summary = run_k6(args.vus, args.iterations, args.target, args.port,
                         args.stark_ratio, args.script)
        cost_after = fetch_cost_stats(args.target, args.port)

        if summary is None:
            print(f"[SKIP] No summary for weight={weight}")
            continue

        row = extract_metrics(summary, weight, args.vus, args.iterations)
        row.update(cost_delta(cost_before, cost_after))
        append_row(row, args.output)

        print(f"\n[RECORDED] weight={weight}  "
              f"throughput={row['throughput_req_per_sec']} req/s  "
              f"p95={row['async_p95_ms']}ms  "
              f"avg_cost={row.get('snark_avg_cost_per_job', 'N/A')}")

        if i < len(weights) - 1:
            print(f"[COOLDOWN] {args.cooldown}s...")
            time.sleep(args.cooldown)

    if Path(TEMP_SUMMARY).exists():
        Path(TEMP_SUMMARY).unlink()

    print("\n" + "=" * 60)
    print(f"WEIGHT SWEEP COMPLETE. Results: {args.output}")
    print("=" * 60)
    print("\nNext step:  python visualize_weight_sweep.py")


if __name__ == "__main__":
    main()
