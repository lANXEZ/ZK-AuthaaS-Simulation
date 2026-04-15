import redis
import json
import time
import argparse

# Parse command-line arguments for Redis host and port
parser = argparse.ArgumentParser(description="SNARK Verifier Worker")
parser.add_argument('--redis-host', type=str, default='localhost', help='Redis server host (default: localhost)')
parser.add_argument('--redis-port', type=int, default=6380, help='Redis server port (default: 6380)')
parser.add_argument('--index', type=int, required=True, help='Index of this SNARK verifier (0-based)')
parser.add_argument('--proof-queue-host', type=str, default='localhost', help='Proof queue Redis host (for feedback & status)')
parser.add_argument('--proof-queue-port', type=int, default=6379, help='Proof queue Redis port (for feedback & status)')
args = parser.parse_args()

# Connect to the Redis message broker (for fetching SNARK jobs)
# Added decode_responses=True so we don't have to manually decode b'...' byte strings
rSnarkQueue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)

# Connect to the main proof queue Redis (for feedback AND updating the API status)
rProofQueue = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True)

def simulate_verification():
    # Simulate SNARK (~50ms) verification time
    delay = 0.05
    time.sleep(delay)
    return True

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SNARK worker {args.index} started. Waiting for proofs...")

while True:
    try:
        # 1. Pull from queue (blocks until a job is available)
        job = rSnarkQueue.brpop("snark_queue", timeout=0)
        
        if not job:
            continue
        
        # 2. Extract the data package 
        # Using tuple unpacking is usually safer for type checkers, 
        # but we include type: ignore just to be safe from the redis-py stubs!
        queue_name, raw_data = job # type: ignore
        job_data = json.loads(raw_data)
        
        # Extract the Job ID so we can report back to the API
        job_id = job_data.get("job_id")
        payload = job_data.get("payload", {})
        
        # (Optional) Update status to let k6 know this worker picked it up
        if job_id:
            rProofQueue.set(f"status:{job_id}", "processing", ex=3600)

        # 3. Logic: Verify, check nullifier, and "issue token"
        success = simulate_verification() 
        
        # Determine the status regardless of whether we have a job_id
        final_status = "completed" if success else "failed"
        
        # 4. Update the status in Redis ONLY if k6 sent a job_id to track
        if job_id:
            # ex=3600 automatically deletes it after 1 hour to prevent memory leaks
            rProofQueue.set(f"status:{job_id}", final_status, ex=3600)

        # Now this print statement is safe because final_status is always defined
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Processed snark proof. Job ID: {job_id} | Status: {final_status}")

        # 5. Publish feedback to selector
        feedback = {"type": "snark", "index": args.index}
        try:
            rProofQueue.publish("verifier_feedback", json.dumps(feedback))
            # I commented the print out below just to keep your console clean under heavy load, 
            # but you can uncomment it if you want the visual confirmation!
            # print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Published feedback: {feedback}")
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to publish feedback: {e}")
            
    except Exception as e:
        # Added a try/except block around the loop. If someone sends bad JSON, 
        # it will just print the error and grab the next job instead of crashing the whole worker.
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error processing job: {e}")
        time.sleep(1)