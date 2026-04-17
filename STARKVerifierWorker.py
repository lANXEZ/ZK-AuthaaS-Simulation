import redis
import json
import time
import os
import argparse

# ==========================================
# STARK Verifier Worker (Swarm-friendly version)
# ==========================================
# Reads jobs from a dedicated key "stark_queue:{index}" on a shared STARK Redis.
# Index is derived from the Swarm task slot (1-based) via TASK_SLOT env var,
# or overridden with --index for manual runs.

parser = argparse.ArgumentParser(description="STARK Verifier Worker")
parser.add_argument('--redis-host', type=str, default='stark-queue',
                    help='Shared STARK queue Redis host')
parser.add_argument('--redis-port', type=int, default=6379,
                    help='Shared STARK queue Redis port')
parser.add_argument('--index', type=int, default=None,
                    help='Explicit 0-based index (overrides TASK_SLOT env var)')
parser.add_argument('--proof-queue-host', type=str, default='proof-queue',
                    help='Proof queue Redis host (for feedback & status)')
parser.add_argument('--proof-queue-port', type=int, default=6379,
                    help='Proof queue Redis port (for feedback & status)')
args = parser.parse_args()

# Resolve 0-based index: CLI arg wins, otherwise derive from Swarm's TASK_SLOT (1-based)
if args.index is not None:
    my_index = args.index
else:
    slot_env = os.environ.get('TASK_SLOT')
    if slot_env is None:
        raise SystemExit("Must provide --index or set TASK_SLOT env var (Swarm's {{.Task.Slot}})")
    my_index = int(slot_env) - 1

my_queue_key = f"stark_queue:{my_index}"

# Connect to the shared STARK Redis (for fetching jobs on our dedicated key)
rStarkQueue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)

# Connect to the proof queue Redis (for status updates and feedback publish)
rProofQueue = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True)

def simulate_verification():
    # Simulate STARK verification time (~200ms)
    delay = 0.2
    time.sleep(delay)
    return True

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] STARK worker idx={my_index} listening on '{my_queue_key}'")

while True:
    try:
        # Block until a job lands on our dedicated key
        job = rStarkQueue.brpop(my_queue_key, timeout=0)
        if not job:
            continue

        queue_name, raw_data = job  # type: ignore
        job_data = json.loads(raw_data)

        job_id = job_data.get("job_id")
        payload = job_data.get("payload", {})

        if job_id:
            rProofQueue.set(f"status:{job_id}", "processing", ex=3600)

        success = simulate_verification()
        final_status = "completed" if success else "failed"

        if job_id:
            rProofQueue.set(f"status:{job_id}", final_status, ex=3600)

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] STARK idx={my_index} processed. Job {job_id} -> {final_status}")

        # Publish feedback to selector
        feedback = {"type": "stark", "index": my_index}
        try:
            rProofQueue.publish("verifier_feedback", json.dumps(feedback))
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to publish feedback: {e}")

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error processing STARK job: {e}")
        time.sleep(1)
