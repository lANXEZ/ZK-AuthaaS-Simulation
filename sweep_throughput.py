#!/usr/bin/env python3
"""
VU Sweep Script for ZK-AuthaaS Throughput Analysis
===================================================

Runs the k6 load test at progressively increasing VU counts and records
aggregate metrics (throughput, latency percentiles, failure counts) to a
CSV file. Use this to find the "knee" where throughput plateaus - the
optimal VU count is right at that knee.

USAGE:
    # Default sweep (localhost, SNARK-only):
    python sweep_throughput.py

    # Against EC2:
    python sweep_throughput.py --target 172.31.45.12

    # Custom VU levels:
    python sweep_throughput.py --vus 50,100,200,400,800

    # STARK-only sweep:
    python sweep_throughput.py --stark-ratio 1.0 --vus 25,50,75,100,150

    # Start fresh (wipe existing CSV):
    python sweep_throughput.py --clean

OUTPUT:
    sweep_results.csv - one row per VU level with metrics
    Use visualize_sweep.py (or your own matplotlib code) to plot.

REQUIREMENTS:
    - k6 must be installed and on PATH (k6 --version works)
    - The ZK-AuthaaS stack must be running and reachable at --target:--port
    - Python 3.7+ (uses only stdlib)


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
DEFAULT_VU_LEVELS = [25, 50, 100, 200, 300, 400, 600, 800]
DEFAULT_ITERATIONS_PER_VU = 10  # keeps each run roughly equal wall-clock time

SWEEP_CSV = "sweep_results.csv"
TEMP_SUMMARY = "_sweep_summary.json"


# ------------------------------------------
# Cost stats helpers
# ------------------------------------------
def fetch_cost_stats(target, port):
    """Query /stats/cost on the request handler. Returns dict or None on failure."""
    try:
        url = f"http://{target}:{port}/stats/cost"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None

def cost_delta(before, after):
    """Return per-run avg cost per job computed from two /stats/cost snapshots."""
    if before is None or after is None:
        return {"snark_avg_cost_per_job": None, "stark_avg_cost_per_job": None}
    snark_jobs = max((after.get("snark_total_jobs", 0) - before.get("snark_total_jobs", 0)), 1)
    snark_cost =       after.get("snark_total_cost", 0) - before.get("snark_total_cost", 0)
    stark_jobs = max((after.get("stark_total_jobs", 0) - before.get("stark_total_jobs", 0)), 1)
    stark_cost =       after.get("stark_total_cost", 0) - before.get("stark_total_cost", 0)
    return {
        "snark_avg_cost_per_job": round(snark_cost / snark_jobs, 4),
        "stark_avg_cost_per_job": round(stark_cost / stark_jobs, 4),
    }

# ------------------------------------------
# k6 orchestration
# ------------------------------------------
def run_k6(vus, iterations, target, port, stark_ratio, load_script):
    """Run k6 once at the given VU level. Returns the parsed summary JSON or None on failure."""
    # Remove stale summary file from any prior run
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

    print(f"\n{'=' * 60}")
    print(f"RUN: VUs={vus}, iterations={iterations}, stark_ratio={stark_ratio}")
    print(f"{'=' * 60}")
    print(" ".join(cmd))
    print()

    # We let k6 stream its normal output to the terminal (so you can watch progress).
    # Non-zero exit codes are expected when thresholds breach - don't abort on them.
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[WARN] k6 exited with code {result.returncode} (often means thresholds failed; continuing)")

    if not Path(TEMP_SUMMARY).exists():
        print(f"[ERROR] {TEMP_SUMMARY} was not produced; skipping this VU level")
        return None

    with open(TEMP_SUMMARY) as f:
        return json.load(f)


# ------------------------------------------
# Metric extraction
# ------------------------------------------
def get(metric, key, default=0):
    """Safe nested get for k6 summary metrics."""
    if not metric:
        return default
    return metric.get(key, default)


def extract_metrics(summary, vus, iterations, stark_ratio):
    """Flatten k6's summary JSON into a single CSV row."""
    metrics = summary.get("metrics", {})

    iter_metric = metrics.get("iterations", {})
    http_dur = metrics.get("http_req_duration", {})
    async_trend = metrics.get("async_verification_time", {})
    snark_trend = metrics.get("snark_verification_time", {})
    stark_trend = metrics.get("stark_verification_time", {})

    failed_verifications = get(metrics.get("failed_verifications"), "count", 0)
    submit_failures = get(metrics.get("submit_failures"), "count", 0)

    completed = get(iter_metric, "count", 0)
    throughput = get(iter_metric, "rate", 0)  # iterations per second

    return {
        "vus": vus,
        "stark_ratio": stark_ratio,
        "target_iterations": iterations,
        "completed_iterations": int(completed),
        "throughput_req_per_sec": round(throughput, 2),
        "async_avg_ms": round(get(async_trend, "avg", 0), 1),
        "async_p50_ms": round(get(async_trend, "med", 0), 1),
        "async_p95_ms": round(get(async_trend, "p(95)", 0), 1),
        "async_p99_ms": round(get(async_trend, "p(99)", 0), 1),
        "async_max_ms": round(get(async_trend, "max", 0), 1),
        "snark_p95_ms": round(get(snark_trend, "p(95)", 0), 1),
        "stark_p95_ms": round(get(stark_trend, "p(95)", 0), 1),
        "http_p95_ms": round(get(http_dur, "p(95)", 0), 1),
        "failed_verifications": int(failed_verifications),
        "submit_failures": int(submit_failures),
    }


# ------------------------------------------
# CSV writer
# ------------------------------------------
def append_row(row, csv_path):
    """Append a row to the sweep CSV; write header if file is new or empty."""
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
        description="Sweep VU counts to find throughput knee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", default="localhost",
                        help="Host/IP of the request-handler (default: localhost)")
    parser.add_argument("--port", default="8000",
                        help="Port of the request-handler (default: 8000)")
    parser.add_argument("--stark-ratio", default="0.0",
                        help="0.0=all SNARK, 1.0=all STARK, 0.5=mixed (default: 0.0)")
    parser.add_argument("--vus",
                        default=",".join(str(v) for v in DEFAULT_VU_LEVELS),
                        help=f"Comma-separated VU levels (default: {DEFAULT_VU_LEVELS})")
    parser.add_argument("--iterations-per-vu", type=int,
                        default=DEFAULT_ITERATIONS_PER_VU,
                        help=f"ITERATIONS = VUs * this value (default: {DEFAULT_ITERATIONS_PER_VU})")
    parser.add_argument("--cooldown", type=int, default=10,
                        help="Seconds between runs to let queues drain (default: 10)")
    parser.add_argument("--output", default=SWEEP_CSV,
                        help=f"Output CSV path (default: {SWEEP_CSV})")
    parser.add_argument("--script", default="load_test.js",
                        help="Path to the k6 load script (default: load_test.js)")
    parser.add_argument("--clean", action="store_true",
                        help="Delete existing sweep CSV before starting")
    args = parser.parse_args()

    # Sanity: is k6 on PATH?
    try:
        subprocess.run(["k6", "--version"], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("[ERROR] k6 not found on PATH. Install from https://k6.io/docs/getting-started/installation/")
        sys.exit(1)

    # Sanity: does the load script exist?
    if not Path(args.script).exists():
        print(f"[ERROR] Load script not found: {args.script}")
        sys.exit(1)

    if args.clean and Path(args.output).exists():
        Path(args.output).unlink()
        print(f"Removed existing {args.output}")

    try:
        vu_levels = [int(v.strip()) for v in args.vus.split(",") if v.strip()]
    except ValueError:
        print(f"[ERROR] --vus must be comma-separated integers; got: {args.vus}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SWEEP PLAN")
    print("=" * 60)
    print(f"  Target:            {args.target}:{args.port}")
    print(f"  STARK_RATIO:       {args.stark_ratio}")
    print(f"  VU levels:         {vu_levels}")
    print(f"  Iterations per VU: {args.iterations_per_vu}  (total iters per run shown below)")
    for v in vu_levels:
        print(f"    VUs={v:<5} -> iterations={v * args.iterations_per_vu}")
    print(f"  Cooldown:          {args.cooldown}s between runs")
    print(f"  Output:            {args.output}")
    print(f"  Load script:       {args.script}")
    print()

    for i, vus in enumerate(vu_levels):
        iterations = vus * args.iterations_per_vu

        cost_before = fetch_cost_stats(args.target, args.port)
        summary = run_k6(vus, iterations, args.target, args.port,
                         args.stark_ratio, args.script)
        cost_after = fetch_cost_stats(args.target, args.port)

        if summary is None:
            print(f"[SKIP] No summary produced for VUs={vus}")
            continue

        row = extract_metrics(summary, vus, iterations, args.stark_ratio)
        row.update(cost_delta(cost_before, cost_after))
        append_row(row, args.output)

        print()
        print(f"[RECORDED] VUs={row['vus']}  "
              f"throughput={row['throughput_req_per_sec']} req/s  "
              f"p95={row['async_p95_ms']}ms  "
              f"p99={row['async_p99_ms']}ms  "
              f"failures={row['failed_verifications']}  "
              f"submit_fails={row['submit_failures']}")

        if i < len(vu_levels) - 1:
            print(f"[COOLDOWN] Waiting {args.cooldown}s for queues to drain...")
            time.sleep(args.cooldown)

    # Tidy up
    if Path(TEMP_SUMMARY).exists():
        Path(TEMP_SUMMARY).unlink()

    print("\n" + "=" * 60)
    print(f"SWEEP COMPLETE. Results saved to {args.output}")
    print("=" * 60)
    print("\nNext steps:")
    print(f"  1. Inspect: column -s, -t {args.output}  (or open in Excel)")
    print(f"  2. Graph it: python visualize_sweep.py")
    print("  3. Look for the knee: the VU level where throughput_req_per_sec stops climbing.")


if __name__ == "__main__":
    main()
