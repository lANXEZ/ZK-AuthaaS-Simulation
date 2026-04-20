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


# ==========================================
# ENDPOINT 3: Cost Statistics
# ==========================================
# Read running cost totals written by the selector into proof-queue Redis.
# Used by sweep_throughput.py to record avg cost per job in the sweep CSV.
# Query before and after a k6 run; the delta gives per-run cost.
@app.get("/stats/cost")
async def get_cost_stats():
    _snark_cost = rProofQueue.get("selector:snark_total_cost")
    _snark_jobs = rProofQueue.get("selector:snark_total_jobs")
    _stark_cost = rProofQueue.get("selector:stark_total_cost")
    _stark_jobs = rProofQueue.get("selector:stark_total_jobs")

    snark_cost = float(_snark_cost) if _snark_cost is not None else 0.0  # type: ignore[arg-type]
    snark_jobs = int(_snark_jobs)   if _snark_jobs is not None else 0    # type: ignore[arg-type]
    stark_cost = float(_stark_cost) if _stark_cost is not None else 0.0  # type: ignore[arg-type]
    stark_jobs = int(_stark_jobs)   if _stark_jobs is not None else 0    # type: ignore[arg-type]

    return {
        "snark_total_cost": round(snark_cost, 4),
        "snark_total_jobs": snark_jobs,
        "snark_avg_cost_per_job": round(snark_cost / snark_jobs, 4) if snark_jobs > 0 else 0,
        "stark_total_cost": round(stark_cost, 4),
        "stark_total_jobs": stark_jobs,
        "stark_avg_cost_per_job": round(stark_cost / stark_jobs, 4) if stark_jobs > 0 else 0,
    }


# ==========================================
# ENDPOINT 4: Set routing weights dynamically
# ==========================================
# Writes the new weights into the proof-queue Redis so the selector picks
# them up on the very next job dispatch — no container restart required.
# Used by weight_sweep.py to change the weight between k6 runs remotely.
@app.post("/admin/set-weight")
async def set_weight(snark: float = 10.0, stark: float = 1.0):
    rProofQueue.set("selector:snark_cost_weight", snark)
    rProofQueue.set("selector:stark_cost_weight", stark)
    return {"snark_cost_weight": snark, "stark_cost_weight": stark, "status": "ok"}


# ==========================================
# ENDPOINT 5: Read current routing weights
# ==========================================
# Returns whatever is stored in Redis — reflects the live value the selector
# is using, including any changes made via POST /admin/set-weight.
@app.get("/admin/get-weight")
async def get_weight():
    snark = rProofQueue.get("selector:snark_cost_weight")
    stark = rProofQueue.get("selector:stark_cost_weight")
    return {
        "snark_cost_weight": float(snark) if snark is not None else None,  # type: ignore[arg-type]
        "stark_cost_weight": float(stark) if stark is not None else None,  # type: ignore[arg-type]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("requestHandler:app", host="0.0.0.0", port=8000, reload=False)