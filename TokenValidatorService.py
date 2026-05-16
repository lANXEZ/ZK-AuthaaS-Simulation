import argparse
import asyncio
import time
from contextlib import asynccontextmanager

import jwt
import redis
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# App-side token validator service.
#
# Flow:
#   POST /ingest {job_id, token}
#     └─ enqueue immediately → return 200 (fast, no blocking)
#
#   Background validation workers (asyncio tasks, --workers N)
#     └─ dequeue → validate (RS256 signature + expiration + domainID)
#     └─ write status:{job_id} = "completed" | "failed"
#        to proof-queue Redis on the main Swarm manager
#
# k6 polls /verify/status/{job_id} on the main system and sees the result.

parser = argparse.ArgumentParser(description="Token Validator Service")
parser.add_argument('--public-key-path', type=str, default='/etc/zk-authaas/zk-authaas-public.pem')
parser.add_argument('--expected-domain-id', type=int, default=0,
                    help='Domain ID this app owns (0..4). Tokens with a different domainID are rejected.')
parser.add_argument('--proof-queue-host', type=str, required=True,
                    help='Manager proof-queue Redis host (for status write-back)')
parser.add_argument('--proof-queue-port', type=int, default=6379)
parser.add_argument('--workers', type=int, default=4,
                    help='Number of parallel validation worker coroutines')
parser.add_argument('--port', type=int, default=9000)
args, _ = parser.parse_known_args()

# Load RSA public key PEM bytes once at startup.
# pyjwt accepts raw PEM bytes directly — no manual key parsing needed.
try:
    with open(args.public_key_path, 'rb') as f:
        _public_key_pem = f.read()
    print(f"RSA public key loaded from {args.public_key_path}")
except Exception as e:
    print(f"[ERROR] Failed to load public key: {e}")
    raise SystemExit(1)

# Redis connection to main system's proof-queue (for status write-back).
# The Redis client is lazy — it does not actually connect until the first command.
# A ping at startup is attempted only as a connectivity check; failure is non-fatal
# so the service can start before the manager's stack is deployed.
rProofQueue = redis.Redis(
    host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True
)
try:
    rProofQueue.ping()
    print(f"Connected to proof-queue Redis @ {args.proof_queue_host}:{args.proof_queue_port}")
except Exception as e:
    print(f"[WARN] proof-queue Redis not reachable yet ({e})")
    print("[WARN] Service starting anyway — workers will connect once Redis is up.")


# ------------------------------------------------------------------
# Validation logic (sync — called inside asyncio.to_thread)
# ------------------------------------------------------------------

def _validate_sync(token: str):
    """Returns (ok: bool, reason: str). Runs in a thread pool worker."""
    # jwt.decode validates signature AND expiration in one call.
    # Raises specific exceptions on each failure type.
    try:
        payload = jwt.decode(token, _public_key_pem, algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        return False, "Token expired"
    except jwt.InvalidTokenError as e:
        return False, f"Invalid token: {e}"

    # domainID is encoded as int in the JWT payload — compare as int.
    if "domainID" not in payload:
        return False, "Missing domainID claim"
    if int(payload["domainID"]) != args.expected_domain_id:
        return False, (
            f"domainID mismatch (got {payload['domainID']!r}, "
            f"expected {args.expected_domain_id})"
        )

    return True, "Valid"


def _write_status(job_id: str, status: str):
    """Write job status back to main system's proof-queue Redis. Runs in thread pool."""
    rProofQueue.set(f"status:{job_id}", status, ex=3600)


# ------------------------------------------------------------------
# Asyncio queue + background workers
# ------------------------------------------------------------------

_ingest_queue: asyncio.Queue


async def _validation_worker():
    while True:
        job_id, token = await _ingest_queue.get()
        try:
            ok, reason = await asyncio.to_thread(_validate_sync, token)
            status = "completed" if ok else "failed"
            if not ok:
                print(f"[{time.strftime('%H:%M:%S')}] Validation FAILED job {job_id}: {reason}")
            await asyncio.to_thread(_write_status, job_id, status)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [ERROR] Worker exception for job {job_id}: {e}")
            try:
                await asyncio.to_thread(_write_status, job_id, "failed")
            except Exception:
                pass
        finally:
            _ingest_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ingest_queue
    _ingest_queue = asyncio.Queue()
    workers = [asyncio.create_task(_validation_worker()) for _ in range(args.workers)]
    print(
        f"[{time.strftime('%H:%M:%S')}] {args.workers} validation workers started. "
        f"domainID={args.expected_domain_id}. Listening on port {args.port}."
    )
    yield
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


# ------------------------------------------------------------------
# FastAPI endpoints
# ------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)


@app.post("/ingest", status_code=200)
async def ingest(body: dict):
    """
    Receive a signed JWT from the token-issuer.
    Enqueues immediately and returns 200 — validation happens asynchronously.
    """
    job_id = body.get("job_id")
    token = body.get("token")

    if not job_id or not token:
        return JSONResponse(status_code=400, content={"error": "Missing job_id or token"})

    await _ingest_queue.put((job_id, token))
    return {"status": "queued", "queue_size": _ingest_queue.qsize()}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queue_size": _ingest_queue.qsize() if _ingest_queue else 0,
        "workers": args.workers,
    }


if __name__ == "__main__":
    uvicorn.run("TokenValidatorService:app", host="0.0.0.0", port=args.port, reload=False)
