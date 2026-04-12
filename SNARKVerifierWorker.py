import redis
import json
import time
import argparse

# Parse command-line arguments for Redis host and port

parser = argparse.ArgumentParser(description="SNARK Verifier Worker")
parser.add_argument('--redis-host', type=str, default='localhost', help='Redis server host (default: localhost)')
parser.add_argument('--redis-port', type=int, default=6380, help='Redis server port (default: 6380)')
parser.add_argument('--index', type=int, required=True, help='Index of this SNARK verifier (0-based)')
parser.add_argument('--proof-queue-host', type=str, default='proof-queue', help='Proof queue Redis host (for feedback)')
parser.add_argument('--proof-queue-port', type=int, default=6379, help='Proof queue Redis port (for feedback)')
args = parser.parse_args()


# Connect to the Redis message broker (for jobs)
rSnarkQueue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0)
# Connect to the proof queue Redis (for feedback)
rProofQueue = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0)

def simulate_verification():
    # Simulate SNARK (~50ms) verification time
    delay = 0.05
    time.sleep(delay)
    return True

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SNARK worker started. Waiting for proofs...")

while True:
    # 1. Pull from queue (blocks until a job is available)
    # brpop returns a tuple: (b'proof_queue', b'{"scheme": "snark", ...}')
    job = rSnarkQueue.brpop("snark_queue", timeout=0)
    
    if not job:
        continue
    
    # 2. Extract the actual JSON payload (the second item in the tuple)
    raw_data = job[1]  # type: ignore
    data = json.loads(raw_data)
    
    # 3. Logic: Verify, check nullifier, and "issue token"
    success = simulate_verification()
    
    # 4. Print success message
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Processed snark proof.")

    # 5. Publish feedback to selector
    feedback = {"type": "snark", "index": args.index}
    try:
        rProofQueue.publish("verifier_feedback", json.dumps(feedback))
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Published feedback: {feedback}")
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to publish feedback: {e}")