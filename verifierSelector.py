import redis
import json
import argparse

# Parse command-line arguments for Redis hosts and ports
parser = argparse.ArgumentParser(description="Verifier Selector")
parser.add_argument('--proof-host', type=str, default='localhost', help='Proof queue Redis host (default: localhost)')
parser.add_argument('--proof-port', type=int, default=6379, help='Proof queue Redis port (default: 6379)')


parser.add_argument('--snark-queues', type=str, default='', help='Comma-separated list of SNARK queue host:port (overrides --snark-count)')
parser.add_argument('--stark-queues', type=str, default='', help='Comma-separated list of STARK queue host:port (overrides --stark-count)')
parser.add_argument('--snark-count', type=int, default=0, help='Number of SNARK verifiers (auto-generate as snark_verifier_{i}:6379)')
parser.add_argument('--stark-count', type=int, default=0, help='Number of STARK verifiers (auto-generate as stark_verifier_{i}:6379)')
args = parser.parse_args()


# Connect to the Redis message brokers
rProofQueue = redis.Redis(host=args.proof_host, port=args.proof_port, db=0)

# Parse SNARK and STARK queues

def parse_queues(queue_str):
    queues = []
    for qp in queue_str.split(","):
        qp = qp.strip()
        if not qp:
            continue
        if ":" in qp:
            host, port = qp.split(":")
            queues.append(redis.Redis(host=host, port=int(port), db=0))
    return queues

# Generate queue addresses if count is provided and no explicit queues are given
def generate_queues(prefix, count, default_port=6379):
    # Use dash-separated hostnames to match docker-compose service names
    return [redis.Redis(host=f"{prefix}-queue-{i+1}", port=default_port, db=0) for i in range(count)]


if args.snark_queues:
    snark_verifier_amount = parse_queues(args.snark_queues)
elif args.snark_count > 0:
    snark_verifier_amount = generate_queues("snark", args.snark_count)
else:
    snark_verifier_amount = []

if args.stark_queues:
    stark_verifier_amount = parse_queues(args.stark_queues)
elif args.stark_count > 0:
    stark_verifier_amount = generate_queues("stark", args.stark_count)
else:
    stark_verifier_amount = []




# Pseudo queue lengths for each verifier (feedback-based)
import threading
import time

# Assign cost to each verifier node (customize as needed)
snark_costs = [1.0, 1.0, 1.0, 5.0, 5.0, 5.0, 8.0, 8.0, 8.0, 10.0]
stark_costs = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

# Adjustable weight for cost influence
SNARK_COST_WEIGHT = 100.0  # Change this to tune balancing
STARK_COST_WEIGHT = 1.0

snark_pseudo_queues = [0 for _ in range(len(snark_verifier_amount))]
stark_pseudo_queues = [0 for _ in range(len(stark_verifier_amount))]

# Feedback listener: expects verifiers to publish to 'verifier_feedback' channel with payload {"type": "snark"/"stark", "index": i}
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


# Start feedback listener thread
threading.Thread(target=feedback_listener, daemon=True).start()

# SNARK pseudo queue printer thread
def print_snark_pseudo_queue():
    while True:
        if snark_pseudo_queues:
            queue_str = ' : '.join(str(q) for q in snark_pseudo_queues)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SNARK Queues: {queue_str}")
        time.sleep(1)

threading.Thread(target=print_snark_pseudo_queue, daemon=True).start()


print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Selector started. Waiting for proofs...")

while True:
    # NOTE: This code expects the synchronous redis-py client.
    # If you are using aioredis or another async client, you must use 'await' and run this in an async function.
    job = rProofQueue.brpop("proof_queue", timeout=0)
    if not job:
        continue
    raw_data = job[1]
    data = json.loads(raw_data)

    # 3. Logic: Read the "scheme" field to determine which worker should process it
    if data['scheme'] == "snark" and snark_verifier_amount:
        # Assign to the snark verifier with the minimum (queue_length + cost * weight)
        min_idx = min(
            range(len(snark_pseudo_queues)),
            key=lambda i: snark_pseudo_queues[i] + snark_costs[i] * SNARK_COST_WEIGHT
        )
        target = snark_verifier_amount[min_idx]
        target.lpush("snark_queue", json.dumps(data))
        snark_pseudo_queues[min_idx] += 1
        # Verifier should publish {"type": "snark", "index": min_idx} to 'verifier_feedback' on completion
    elif data['scheme'] == "stark" and stark_verifier_amount:
        # Assign to the stark verifier with the minimum (queue_length + cost * weight)
        min_idx = min(
            range(len(stark_pseudo_queues)),
            key=lambda i: stark_pseudo_queues[i] + stark_costs[i] * STARK_COST_WEIGHT
        )
        target = stark_verifier_amount[min_idx]
        target.lpush("stark_queue", json.dumps(data))
        stark_pseudo_queues[min_idx] += 1
        # Verifier should publish {"type": "stark", "index": min_idx} to 'verifier_feedback' on completion