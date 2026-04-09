
import argparse

from fastapi import FastAPI
import redis, json
import argparse

# Parse command-line arguments for Redis host and port
parser = argparse.ArgumentParser(description="Request Handler API")
parser.add_argument('--redis-host', type=str, default='localhost', help='Redis server host (default: localhost)')
parser.add_argument('--redis-port', type=int, default=6379, help='Redis server port (default: 6379)')
args, _ = parser.parse_known_args()

app = FastAPI()
rProofQueue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("requestHandler:app", host="0.0.0.0", port=8000, reload=False)

@app.post("/verify")
async def handle_request(payload: dict):
    # Phase 4: Admission check & queue insertion
    rProofQueue.lpush("proof_queue", json.dumps(payload))
    return {"status": "enqueued"}
