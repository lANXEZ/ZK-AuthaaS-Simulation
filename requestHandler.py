import argparse
import uuid
import json
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
import redis

# Parse command-line arguments for Redis host and port
parser = argparse.ArgumentParser(description="Request Handler API")
parser.add_argument('--redis-host', type=str, default='localhost', help='Redis server host (default: localhost)')
parser.add_argument('--redis-port', type=int, default=6379, help='Redis server port (default: 6379)')
args, _ = parser.parse_known_args()

app = FastAPI()

# decode_responses=True ensures Redis returns standard strings instead of bytes
rProofQueue = redis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)

# ==========================================
# ENDPOINT 1: Submit the Proof
# ==========================================
@app.post("/verify/submit", status_code=status.HTTP_202_ACCEPTED)
async def submit_request(payload: dict):
    # 1. Generate a unique tracking ID for this specific proof
    job_id = str(uuid.uuid4())
    
    # 2. Prepare the data package for the worker node
    job_data = {
        "job_id": job_id,
        "payload": payload
    }
    
    # 3. Create a status tracker in Redis and set it to 'pending'
    # 'ex=3600' automatically deletes this tracker after 1 hour to prevent memory leaks
    rProofQueue.set(f"status:{job_id}", "pending", ex=3600)
    
    # 4. Push the actual job onto the queue for the worker to process
    rProofQueue.lpush("proof_queue", json.dumps(job_data))
    
    # 5. Return the Job ID immediately to k6
    return {"status": "accepted", "job_id": job_id}


# ==========================================
# ENDPOINT 2: Poll the Status
# ==========================================
@app.get("/verify/status/{job_id}")
async def check_status(job_id: str):
    # 1. Look up the current status of this specific job in Redis
    job_status = rProofQueue.get(f"status:{job_id}")
    
    # 2. Handle the case where the job ID is invalid or expired
    if job_status is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "Job ID not found or expired"}
        )
        
    # 3. Return the status to k6 (e.g., 'pending', 'processing', 'completed')
    return {"job_id": job_id, "status": job_status}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("requestHandler:app", host="0.0.0.0", port=8000, reload=False)