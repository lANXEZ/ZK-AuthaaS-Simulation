#!/usr/bin/env python3
"""
Trace a single job through the ZK-AuthaaS pipeline.

Run this on the MANAGER EC2 while k6 is loading the system at 200 VUs.
It submits ONE extra job, then polls proof-queue Redis directly every 5 ms
for status changes — bypassing k6's 100-300 ms polling jitter.

Output: a timeline showing how many ms each stage took:

    t=0     submit returned
    t=12    request-handler set status=accepted
    t=420   SNARK worker set status=processing
    t=4830  app validator set status=completed   ← latency lives in this gap

USAGE:
    python3 trace_job.py
    python3 trace_job.py --target localhost:8000 --domain 0 --jobs 5

REQUIREMENTS:
    pip install requests redis
"""

import argparse
import json
import time
from typing import Optional

import redis
import requests

# Same valid groth16 proof that load_test.js uses.
SNARK_PROOF = {
    "pi_a": [
        "16893334615242764580836222078829142520432756203770466604081032720388657032757",
        "5095606969395716303621702958471922376961618029789842152295821108717087682311",
        "1",
    ],
    "pi_b": [
        [
            "13772398192624595577472662855811728500397412494267729711099372526485968374649",
            "15249941699599606024139723272508104548269790148997217612719623411267570558493",
        ],
        [
            "19735295879188043871505513529932228526631701925990878770250928234435443795397",
            "11046809327765151786114304454515091703284305019483922364766276175300463695885",
        ],
        ["1", "0"],
    ],
    "pi_c": [
        "18536201733965390491456176988021021022761142364866628667452517360063595662975",
        "15291715715367874403418883228408929985980666544091293542955491873294267230352",
        "1",
    ],
    "protocol": "groth16",
    "curve": "bn128",
}

PUBLIC_INPUTS = [
    "1120771572304984668855649788542860110303223894298952018121329196339919157573",
    "20197087425205130352574209034729275460185533126585197591053247747830393653846",
    "111222333",
    "444555666",
    "1764263975784332459809300572476310454427845461305579380554772042455913567929",
    "10988278040513707334400680073433620711051179041727267619401283491695328957763",
]


def trace_one(target: str, redis_client: redis.Redis, domain: int, max_wait_s: float):
    submit_url = f"http://{target}/verify/submit"
    body = {
        "scheme": "snark",
        "proof": SNARK_PROOF,
        "public_inputs": PUBLIC_INPUTS,
        "domain_id": domain,
    }

    t0 = time.monotonic()
    resp = requests.post(submit_url, json=body, timeout=5.0)
    t_submit_returned = time.monotonic() - t0
    if resp.status_code != 200:
        print(f"  [ERROR] submit returned {resp.status_code}: {resp.text}")
        return
    job_id = resp.json().get("job_id")
    if not job_id:
        print(f"  [ERROR] no job_id in response: {resp.text}")
        return

    status_key = f"status:{job_id}"
    transitions = [(t_submit_returned, "submit returned (HTTP 200)")]
    last_status: Optional[str] = None
    deadline = t0 + max_wait_s

    while time.monotonic() < deadline:
        cur = redis_client.get(status_key)
        if cur != last_status:
            elapsed = time.monotonic() - t0
            transitions.append((elapsed, f"status = {cur!r}"))
            last_status = cur
            if cur in ("completed", "failed"):
                break
        time.sleep(0.005)  # 5 ms polling

    print(f"\n  job {job_id} (domain {domain})")
    prev = 0.0
    for elapsed, label in transitions:
        delta = (elapsed - prev) * 1000
        print(f"    t={elapsed * 1000:>8.1f} ms   (Δ {delta:>7.1f} ms)   {label}")
        prev = elapsed
    if last_status not in ("completed", "failed"):
        print(f"    [WARN] last status before timeout: {last_status!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="localhost:8000",
                    help="request-handler endpoint (default: localhost:8000)")
    ap.add_argument("--redis-host", default="localhost",
                    help="proof-queue Redis host (default: localhost)")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--domain", type=int, default=0, help="domain_id (0..4)")
    ap.add_argument("--jobs", type=int, default=3,
                    help="number of jobs to trace sequentially (default: 3)")
    ap.add_argument("--gap", type=float, default=1.0,
                    help="seconds to wait between jobs (default: 1.0)")
    ap.add_argument("--max-wait", type=float, default=15.0,
                    help="max seconds to wait for completed/failed (default: 15)")
    args = ap.parse_args()

    r = redis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)
    try:
        r.ping()
    except Exception as e:
        print(f"[ERROR] cannot reach proof-queue Redis at {args.redis_host}:{args.redis_port}: {e}")
        return

    print(f"Tracing {args.jobs} job(s) through {args.target}, domain={args.domain}")
    print(f"Polling Redis @ {args.redis_host}:{args.redis_port} every 5 ms.")
    print(f"(Run k6 at 200 VUs in another shell so the system is under load.)\n")

    for i in range(args.jobs):
        if i > 0:
            time.sleep(args.gap)
        trace_one(args.target, r, args.domain, args.max_wait)


if __name__ == "__main__":
    main()
