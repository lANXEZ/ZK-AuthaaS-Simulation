import redis
import json
import time
import argparse

# Parse command-line arguments for Redis host and port
parser = argparse.ArgumentParser(description="STARK Verifier Worker")
parser.add_argument('--redis-host', type=str, default='localhost', help='Redis server host (default: localhost)')
parser.add_argument('--redis-port', type=int, default=6381, help='Redis server port (default: 6381)')
parser.add_argument('--index', type=int, required=True, help='Index of this STARK verifier (0-based)')
parser.add_argument('--proof-queue-host', type=str, default='localhost', help='Proof queue Redis host (for feedback & status)')
parser.add_argument('--proof-queue-port', type=int, default=6379, help='Proof queue Redis port (for feedback & status)')
args = parser.parse_args()

# Connect to the Redis message broker (for fetching STARK jobs)
# Added decode_responses=True for automatic string parsing
rStarkQueue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)

# Connect to the main proof queue Redis (for feedback AND updating the API status)
rProofQueue = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True)

def simulate_verification():
    # Simulate STARK (~200ms) verification time
    delay = 0.2
    time.sleep(delay)
    return True

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] STARK worker {args.index} started. Waiting for proofs...")

while True:
    try:
        # 1. Pull from queue (blocks until a job is available)
        job = rStarkQueue.brpop("stark_queue", timeout=0)
        
        if not job:
            continue
        
        # 2. Extract the data package safely
        queue_name, raw_data = job # type: ignore
        job_data = json.loads(raw_data)
        
        # Extract the Job ID for the API tracking
        job_id = job_data.get("job_id")
        payload = job_data.get("payload", {})
        
        # (Optional) Update status to let k6 know this worker picked it up
        if job_id:
            rProofQueue.set(f"status:{job_id}", "processing", ex=3600)

        # 3. Logic: Verify, check nullifier, and "issue token"
        success = simulate_verification()
        
        # Determine the final status
        final_status = "completed" if success else "failed"
        
        # 4. Update the status so the k6 polling loop can escape!
        if job_id:
            rProofQueue.set(f"status:{job_id}", final_status, ex=3600)

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Processed stark proof. Job ID: {job_id} | Status: {final_status}")

        # 5. Publish feedback to selector
        feedback = {"type": "stark", "index": args.index}
        try:
            rProofQueue.publish("verifier_feedback", json.dumps(feedback))
            # print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Published feedback: {feedback}")
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to publish feedback: {e}")
            
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error processing STARK job: {e}")
        time.sleep(1)