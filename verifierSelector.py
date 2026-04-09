import redis
import json

# Connect to the Redis message broker
rProofQueue = redis.Redis(host='localhost', port=6379, db=0)
rSnarkQueue = redis.Redis(host='localhost', port=6380, db=0)
rStarkQueue = redis.Redis(host='localhost', port=6381, db=0)

print("Selector started. Waiting for proofs...")

while True:
    # 1. Pull from queue (blocks until a job is available)
    # brpop returns a tuple: (b'proof_queue', b'{"scheme": "snark", ...}')
    job = rProofQueue.brpop("proof_queue", timeout=0)
    
    if not job:
        continue
    
    # 2. Extract the actual JSON payload (the second item in the tuple)
    raw_data = job[1]  # type: ignore
    data = json.loads(raw_data)
    
    # 3. Logic: Read the "scheme" field to determine which worker should process it
    if data['scheme'] == "snark":
        # Process SNARK proof
        rSnarkQueue.lpush("snark_queue", json.dumps(data))
    elif data['scheme'] == "stark":
        # Process STARK proof
        rStarkQueue.lpush("stark_queue", json.dumps(data))