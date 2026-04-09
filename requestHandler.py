from fastapi import FastAPI
import redis, json

app = FastAPI()
rProofQueue = redis.Redis(host='localhost', port=6379, db=0)

@app.post("/verify")
async def handle_request(payload: dict):
    # Phase 4: Admission check & queue insertion
    rProofQueue.lpush("proof_queue", json.dumps(payload))
    return {"status": "enqueued"}
