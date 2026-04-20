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
parser.add_argument('--routing', type=str, default='weighted',
                    choices=['weighted', 'roundrobin'],
                    help='Routing algorithm: weighted (default) or roundrobin')
parser.add_argument('--snark-cost-weight', type=float, default=10.0,
                    help='Cost weight for SNARK routing (default: 10.0)')
parser.add_argument('--stark-cost-weight', type=float, default=1.0,
                    help='Cost weight for STARK routing (default: 1.0)')
args = parser.parse_args()

# ------------------------------------------
# Redis connections (one per logical broker)
# ------------------------------------------
rProofQueue = redis.Redis(host=args.proof_host, port=args.proof_port, db=0, decode_responses=True)
rSnarkQueue = redis.Redis(host=args.snark_host, port=args.snark_port, db=0)
rStarkQueue = redis.Redis(host=args.stark_host, port=args.stark_port, db=0)

snark_count = args.snark_count
stark_count = args.stark_count
routing    = args.routing

# ------------------------------------------
# Cost vectors
# ------------------------------------------
# Alternating cheap (1.0) / expensive (2.0) nodes.
# With 50 nodes this gives 25 cheap + 25 expensive.
#
# Weighted routing fills cheap nodes first, spilling to expensive
# ones only when cheap nodes are saturated → avg cost stays near 1.0
# at low-to-medium load.
#
# Round-robin ignores cost entirely and always splits 50/50
# → avg cost is always (1.0+2.0)/2 = 1.5 regardless of load.
#
# This difference is the key metric captured by /stats/cost and
# plotted in visualize_comparison.py.
snark_costs_base = [1.0, 2.0]
stark_costs_base = [1.0, 2.0]

def build_cost_vector(base, count):
    """Cycle the base pattern to cover `count` nodes."""
    return [base[i % len(base)] for i in range(count)]

snark_costs = build_cost_vector(snark_costs_base, snark_count)
stark_costs = build_cost_vector(stark_costs_base, stark_count)

# Adjustable weight for cost influence (set via --snark-cost-weight / --stark-cost-weight)
# These are the CLI defaults. The live value is stored in Redis and can be changed
# at runtime via POST /admin/set-weight without restarting this container.
SNARK_COST_WEIGHT = args.snark_cost_weight
STARK_COST_WEIGHT = args.stark_cost_weight

# Pseudo queue lengths - updated by feedback from workers
snark_pseudo_queues = [0 for _ in range(snark_count)]
stark_pseudo_queues = [0 for _ in range(stark_count)]

# Round-robin counters (only used when --routing roundrobin)
snark_rr_counter = 0
stark_rr_counter = 0

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

# Seed Redis with the CLI default weights so the selector always has a valid
# value on the very first job dispatch, and so GET /admin/get-weight works
# immediately after startup even before any POST /admin/set-weight call.
rProofQueue.set("selector:snark_cost_weight", SNARK_COST_WEIGHT)
rProofQueue.set("selector:stark_cost_weight", STARK_COST_WEIGHT)

# Track the last-seen weights so we can log when they change at runtime
_last_snark_w = SNARK_COST_WEIGHT
_last_stark_w = STARK_COST_WEIGHT

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Selector started. "
      f"SNARK={snark_count} nodes, STARK={stark_count} nodes. "
      f"Routing={routing}. "
      f"SNARK_COST_WEIGHT={SNARK_COST_WEIGHT}, STARK_COST_WEIGHT={STARK_COST_WEIGHT}. "
      f"Weights are live-adjustable via POST /admin/set-weight. "
      f"Waiting for proofs...")

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

    # Read weights live from Redis — POST /admin/set-weight updates these
    # keys without restarting the container, so changes take effect here.
    # Falls back to the CLI default if the key has been deleted.
    _snark_w_raw = rProofQueue.get("selector:snark_cost_weight")
    _stark_w_raw = rProofQueue.get("selector:stark_cost_weight")
    snark_w = float(_snark_w_raw) if _snark_w_raw is not None else SNARK_COST_WEIGHT
    stark_w = float(_stark_w_raw) if _stark_w_raw is not None else STARK_COST_WEIGHT

    # Log whenever the weight changes at runtime (not on every job)
    if snark_w != _last_snark_w:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SNARK_COST_WEIGHT changed: {_last_snark_w} → {snark_w}")
        _last_snark_w = snark_w
    if stark_w != _last_stark_w:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] STARK_COST_WEIGHT changed: {_last_stark_w} → {stark_w}")
        _last_stark_w = stark_w

    # Pick a verifier based on the selected routing algorithm
    scheme = data.get('payload', {}).get('scheme')

    if scheme == "snark" and snark_count > 0:
        if routing == 'roundrobin':
            min_idx = snark_rr_counter % snark_count
            snark_rr_counter += 1
        else:  # weighted
            min_idx = min(
                range(snark_count),
                key=lambda i: snark_pseudo_queues[i] + snark_costs[i] * snark_w
            )
        rSnarkQueue.lpush(f"snark_queue:{min_idx}", json.dumps(data))
        snark_pseudo_queues[min_idx] += 1
        # Accumulate cost for this dispatch (atomic Redis ops — readable via /stats/cost)
        rProofQueue.incrbyfloat("selector:snark_total_cost", snark_costs[min_idx])
        rProofQueue.incr("selector:snark_total_jobs")

    elif scheme == "stark" and stark_count > 0:
        if routing == 'roundrobin':
            min_idx = stark_rr_counter % stark_count
            stark_rr_counter += 1
        else:  # weighted
            min_idx = min(
                range(stark_count),
                key=lambda i: stark_pseudo_queues[i] + stark_costs[i] * stark_w
            )
        rStarkQueue.lpush(f"stark_queue:{min_idx}", json.dumps(data))
        stark_pseudo_queues[min_idx] += 1
        # Accumulate cost for this dispatch
        rProofQueue.incrbyfloat("selector:stark_total_cost", stark_costs[min_idx])
        rProofQueue.incr("selector:stark_total_jobs")

    else:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: Unrecognized scheme or no verifiers available: {scheme}")
