import redis
import json
import argparse
import threading
import time

# ==========================================
# Verifier Selector (Swarm-friendly version)
# ==========================================
# This version connects to ONE Redis per verifier type (snark-queue, stark-queue)
# instead of one Redis per verifier node. Each verifier reads from a dedicated
# key like "snark_queue:{index}" on the shared Redis.

parser = argparse.ArgumentParser(description="Verifier Selector")
parser.add_argument('--proof-host', type=str, default='proof-queue',
                    help='Proof queue Redis host')
parser.add_argument('--proof-port', type=int, default=6379,
                    help='Proof queue Redis port')
parser.add_argument('--snark-host', type=str, default='snark-queue',
                    help='Shared SNARK queue Redis host')
parser.add_argument('--snark-port', type=int, default=6379,
                    help='Shared SNARK queue Redis port')
parser.add_argument('--stark-host', type=str, default='stark-queue',
                    help='Shared STARK queue Redis host')
parser.add_argument('--stark-port', type=int, default=6379,
                    help='Shared STARK queue Redis port')
parser.add_argument('--snark-count', type=int, default=10,
                    help='Number of SNARK verifier replicas (matches deploy.replicas)')
parser.add_argument('--stark-count', type=int, default=10,
                    help='Number of STARK verifier replicas (matches deploy.replicas)')
args = parser.parse_args()

# ------------------------------------------
# Redis connections (one per logical broker)
# ------------------------------------------
rProofQueue = redis.Redis(host=args.proof_host, port=args.proof_port, db=0)
rSnarkQueue = redis.Redis(host=args.snark_host, port=args.snark_port, db=0)
rStarkQueue = redis.Redis(host=args.stark_host, port=args.stark_port, db=0)

snark_count = args.snark_count
stark_count = args.stark_count

# ------------------------------------------
# Cost vectors
# ------------------------------------------
# Base patterns - extend or replace with auto-generated profiles for large N.
snark_costs_base = [1.0, 1.0, 1.0, 5.0, 5.0, 5.0, 8.0, 8.0, 8.0, 10.0]
stark_costs_base = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

def build_cost_vector(base, count):
    """Cycle the base pattern to cover `count` nodes (simple scaling fallback)."""
    return [base[i % len(base)] for i in range(count)]

snark_costs = build_cost_vector(snark_costs_base, snark_count)
stark_costs = build_cost_vector(stark_costs_base, stark_count)

# Adjustable weight for cost influence
SNARK_COST_WEIGHT = 100.0
STARK_COST_WEIGHT = 1.0

# Pseudo queue lengths - updated by feedback from workers
snark_pseudo_queues = [0 for _ in range(snark_count)]
stark_pseudo_queues = [0 for _ in range(stark_count)]

# ------------------------------------------
# Feedback listener (unchanged logic)
# ------------------------------------------
def feedback_listener():
    pubsub = rProofQueue.pubsub()
    pubsub.subscribe('verifier_feedback')
    for message in pubsub.listen():
        if message['type'] != 'message':
            continue
        try:
            payload = json.loads(message['data'])
            vtype = payload.get('type')
            idx = payload.get('index')
            if vtype == 'snark' and 0 <= idx < len(snark_pseudo_queues):
                if snark_pseudo_queues[idx] > 0:
                    snark_pseudo_queues[idx] -= 1
            elif vtype == 'stark' and 0 <= idx < len(stark_pseudo_queues):
                if stark_pseudo_queues[idx] > 0:
                    stark_pseudo_queues[idx] -= 1
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Feedback listener error: {e}")

threading.Thread(target=feedback_listener, daemon=True).start()

# ------------------------------------------
# (Optional) Pseudo queue printer for debugging
# ------------------------------------------
def print_snark_pseudo_queue():
    while True:
        if snark_pseudo_queues:
            queue_str = ' : '.join(str(q) for q in snark_pseudo_queues)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SNARK Queues: {queue_str}")
        time.sleep(1)

# Uncomment if you want live queue depth logs:
# threading.Thread(target=print_snark_pseudo_queue, daemon=True).start()

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Selector started. "
      f"SNARK={snark_count} nodes, STARK={stark_count} nodes. Waiting for proofs...")

# ------------------------------------------
# Main dispatch loop
# ------------------------------------------
while True:
    job = rProofQueue.brpop("proof_queue", timeout=0)
    if not job:
        continue

    queue_name, raw_data = job  # type: ignore

    try:
        data = json.loads(raw_data)
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Malformed JSON discarded: {raw_data} | Error: {e}")
        continue

    # Handle wake-up requests
    if str(data.get('type', '')).lower() in ('wake_up_request', 'wake up request'):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Wake up request discarded: {data}")
        continue

    # Pick a verifier based on scheme + weighted-cost load balancing
    scheme = data.get('payload', {}).get('scheme')

    if scheme == "snark" and snark_count > 0:
        min_idx = min(
            range(snark_count),
            key=lambda i: snark_pseudo_queues[i] + snark_costs[i] * SNARK_COST_WEIGHT
        )
        # Push to this specific node's dedicated key on the shared SNARK Redis
        rSnarkQueue.lpush(f"snark_queue:{min_idx}", json.dumps(data))
        snark_pseudo_queues[min_idx] += 1

    elif scheme == "stark" and stark_count > 0:
        min_idx = min(
            range(stark_count),
            key=lambda i: stark_pseudo_queues[i] + stark_costs[i] * STARK_COST_WEIGHT
        )
        rStarkQueue.lpush(f"stark_queue:{min_idx}", json.dumps(data))
        stark_pseudo_queues[min_idx] += 1

    else:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: Unrecognized scheme or no verifiers available: {scheme}")
