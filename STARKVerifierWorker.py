import redis
import json
import time

# Connect to the Redis message broker
rStarkQueue = redis.Redis(host='localhost', port=6381, db=0)

def simulate_verification():
    # Simulate STARK (~200ms) verification time
    delay = 0.2
    time.sleep(delay)
    return True

print("STARK worker started. Waiting for proofs...")

while True:
    # 1. Pull from queue (blocks until a job is available)
    # brpop returns a tuple: (b'proof_queue', b'{"scheme": "snark", ...}')
    job = rStarkQueue.brpop("stark_queue", timeout=0)
    
    if not job:
        continue
    
    # 2. Extract the actual JSON payload (the second item in the tuple)
    raw_data = job[1]  # type: ignore
    data = json.loads(raw_data)
    
    # 3. Logic: Verify, check nullifier, and "issue token"
    success = simulate_verification()
    
    # 4. Print success message
    print(f"Processed stark proof.")