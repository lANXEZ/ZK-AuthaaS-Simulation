import argparse
import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager

import redis
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from Crypto.Signature import pkcs1_v1_5
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

# App-side token validator service.
#
# Flow:
#   POST /ingest {job_id, token}
#     └─ enqueue immediately → return 200 (fast, no blocking)
#
#   Background validation workers (asyncio tasks, --workers N)
#     └─ dequeue → validate (signature + expiration + domainID)
#     └─ write status:{job_id} = "completed" | "failed"
#        to proof-queue Redis on the main Swarm manager
#
# k6 polls /verify/status/{job_id} on the main system and sees the result.

parser = argparse.ArgumentParser(description="Token Validator Service")
parser.add_argument('--public-key-path', type=str, default='/etc/zk-authaas/zk-authaas-public.pem')
parser.add_argument('--expected-domain-id', type=str, default='1')
parser.add_argument('--proof-queue-host', type=str, required=True,
                    help='Manager proof-queue Redis host (for writing status write-back)')
parser.add_argument('--proof-queue-port', type=int, default=6379)
parser.add_argument('--workers', type=int, default=4,
                    help='Number of parallel validation worker coroutines')
parser.add_argument('--port', type=int, default=9000)
args, _ = parser.parse_known_args()

# Load RSA public key once at startup
try:
    with open(args.public_key_path, 'rb') as f:
        _public_key = RSA.import_key(f.read())
    _verifier = pkcs1_v1_5.new(_public_key)
    print(f"RSA public key loaded ({_public_key.size_in_bits()} bits) from {args.public_key_path}")
except Exception as e:
    print(f"[ERROR] Failed to load public key: {e}")
    raise SystemExit(1)

# Redis connection to main system's proof-queue (for status write-back)
try:
    rProofQueue = redis.Redis(
        host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True
    )
    rProofQueue.ping()
    print(f"Connected to proof-queue Redis @ {args.proof_queue_host}:{args.proof_queue_port}")
except Exception as e:
    print(f"[ERROR] Cannot reach proof-queue Redis: {e}")
    raise SystemExit(1)


# ------------------------------------------------------------------
# Validation logic (sync — called inside asyncio.to_thread)
# ------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    s = s.encode('utf-8') if isinstance(s, str) else s
    pad = 4 - (len(s) % 4)
    if pad != 4:
        s += b'=' * pad
    return base64.urlsafe_b64decode(s)


def _validate_sync(token: str):
    """Returns (ok: bool, reason: str). Runs in a thread pool worker."""
    parts = token.split('.')
    if len(parts) != 3:
        return False, "Malformed JWT"

    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        sig = _b64url_decode(sig_b64)
    except Exception:
        return False, "JWT decode error"

    if header.get("alg") != "RS256":
        return False, f"Unsupported algorithm: {header.get('alg')}"

    message = f"{header_b64}.{payload_b64}".encode('utf-8')
    try:
        valid = _verifier.verify(SHA256.new(message), sig)
        if not valid:
            return False, "Signature invalid"
    except Exception:
        return False, "Signature verification failed"

    exp = payload.get("exp")
    if exp is None:
        return False, "Missing exp claim"
    if int(time.time()) >= exp:
        return False, "Token expired"

    if payload.get("domainID") != args.expected_domain_id:
        return False, f"domainID mismatch (got {payload.get('domainID')!r})"

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
                print(f"[{time.strftime('%H:%M:%S')}] Validation failed job {job_id}: {reason}")
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
    print(f"[{time.strftime('%H:%M:%S')}] {args.workers} validation workers started. "
          f"Listening on port {args.port}.")
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
