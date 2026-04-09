import redis
import json
import argparse

# Parse command-line arguments for Redis hosts and ports
parser = argparse.ArgumentParser(description="Verifier Selector")
parser.add_argument('--proof-host', type=str, default='localhost', help='Proof queue Redis host (default: localhost)')
parser.add_argument('--proof-port', type=int, default=6379, help='Proof queue Redis port (default: 6379)')
parser.add_argument('--snark-host', type=str, default='localhost', help='SNARK queue Redis host (default: localhost)')
parser.add_argument('--snark-port', type=int, default=6380, help='SNARK queue Redis port (default: 6380)')
parser.add_argument('--stark-host', type=str, default='localhost', help='STARK queue Redis host (default: localhost)')
parser.add_argument('--stark-port', type=int, default=6381, help='STARK queue Redis port (default: 6381)')
args = parser.parse_args()

# Connect to the Redis message brokers
rProofQueue = redis.Redis(host=args.proof_host, port=args.proof_port, db=0)
rSnarkQueue = redis.Redis(host=args.snark_host, port=args.snark_port, db=0)
rStarkQueue = redis.Redis(host=args.stark_host, port=args.stark_port, db=0)

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