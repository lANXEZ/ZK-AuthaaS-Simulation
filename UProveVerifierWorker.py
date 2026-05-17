"""
U-Prove Verifier Worker
=======================
Drop-in replacement for SNARKVerifierWorker.js in the on-prem stack.

Same BRPOP/verify/status-write loop, but verifies U-Prove presentation proofs
(Paquin & Zaverucha 2013) instead of groth16 SNARK. Used to compare per-domain
latency between SNARK and U-Prove under identical workload conditions.

Each worker pulls jobs from snark_queue:{index} on the shared queue Redis, runs
verify_bundle() from uprove_verifier.py, then writes status=completed|failed
directly to proof-queue (on-prem mode — no token issuance).

Index resolution: --index wins, else TASK_SLOT - 1 (Swarm semantics, also used
by the per-replica docker-compose blocks).
"""
import argparse
import json
import os
import sys
import time

import redis

from uprove_verifier import verify_bundle


def parse_args():
    ap = argparse.ArgumentParser(description="U-Prove Verifier Worker (on-prem)")
    ap.add_argument("--redis-host", default="snark-queue",
                    help="Hostname of the queue Redis (default: snark-queue, "
                         "kept as 'snark-queue' for compose-file parallelism)")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--proof-queue-host", default="proof-queue")
    ap.add_argument("--proof-queue-port", type=int, default=6379)
    ap.add_argument("--index", type=int, default=None,
                    help="0-based worker slot; falls back to TASK_SLOT - 1")
    return ap.parse_args()


def main():
    args = parse_args()

    if args.index is not None:
        my_index = args.index
    else:
        slot = os.environ.get("TASK_SLOT")
        if slot is None:
            print("Must provide --index or set TASK_SLOT env var", file=sys.stderr)
            sys.exit(1)
        my_index = int(slot) - 1

    queue_key = f"snark_queue:{my_index}"

    r_queue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)
    r_proof = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True)

    print(f"[{time.strftime('%H:%M:%S')}] U-Prove worker idx={my_index} listening on '{queue_key}'",
          flush=True)

    while True:
        job_id = None
        try:
            popped = r_queue.brpop(queue_key, timeout=0)
            if not popped:
                continue
            _, raw = popped
            job = json.loads(raw)
            job_id = job.get("job_id")
            payload = job.get("payload") or {}

            if job_id:
                r_proof.set(f"status:{job_id}", "processing", ex=3600)

            # The k6 payload's "proof" field is the full U-Prove bundle
            # ({issuer_params, token, presentation_proof}). verify_bundle()
            # expects exactly this shape.
            bundle = payload.get("proof")
            if not isinstance(bundle, dict):
                if job_id:
                    r_proof.set(f"status:{job_id}", "failed", ex=3600)
                continue

            ok, _reason, _elapsed_ms = verify_bundle(bundle)

            if job_id:
                r_proof.set(f"status:{job_id}", "completed" if ok else "failed", ex=3600)

            # Feedback publish (round-robin selector ignores it, but keep
            # symmetric with SNARKVerifierWorker.js for any future weighted runs)
            try:
                r_proof.publish(
                    "verifier_feedback",
                    json.dumps({"type": "snark", "index": my_index}),
                )
            except Exception:
                pass

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [ERROR] worker {my_index}: {e}", flush=True)
            if job_id:
                try:
                    r_proof.set(f"status:{job_id}", "failed", ex=3600)
                except Exception:
                    pass
            time.sleep(1)


if __name__ == "__main__":
    main()
