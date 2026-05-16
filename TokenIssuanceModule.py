import asyncio
import argparse
import json
import time

import httpx
import jwt
import redis.asyncio as redis
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# Reads verified jobs from token-queue Redis (verified_queue), signs a RS256 JWT,
# and POSTs {job_id, token} to the target app's /ingest endpoint.
#
# Concurrency model:
#   - N coroutine workers all BRPOP from verified_queue concurrently
#   - All share one httpx.AsyncClient with a connection-pool of size N
#     so POSTs reuse keep-alive connections (no TCP setup per request).
#   - Each worker handles its own job end-to-end: BRPOP → look up domain
#     → sign JWT → POST → status write-back on HTTP failure.
#
# This replaces a synchronous requests.post()-per-job loop that was capped
# at ~2 jobs/s/replica due to per-call TCP handshakes.

parser = argparse.ArgumentParser(description="Token Issuance Worker (asyncio)")
parser.add_argument('--token-queue-host', type=str, default='token-queue')
parser.add_argument('--token-queue-port', type=int, default=6379)
parser.add_argument('--proof-queue-host', type=str, default='proof-queue')
parser.add_argument('--proof-queue-port', type=int, default=6379)
parser.add_argument('--private-key-path', type=str, default='/etc/zk-authaas/zk-authaas-key.pem')
parser.add_argument('--app-urls', type=str, required=True,
                    help='Comma-separated list of 5 app /ingest URLs (index 0..4 = domain 0..4)')
parser.add_argument('--token-ttl', type=int, default=3600)
parser.add_argument('--http-timeout', type=float, default=5.0)
parser.add_argument('--concurrency', type=int, default=64,
                    help='Number of in-flight jobs per replica (also the HTTP connection-pool size)')
args = parser.parse_args()

APP_URLS = [u.strip() for u in args.app_urls.split(',')]
if len(APP_URLS) != 5:
    print(f"[ERROR] --app-urls must contain exactly 5 comma-separated URLs (got {len(APP_URLS)})")
    raise SystemExit(1)

try:
    with open(args.private_key_path, 'rb') as f:
        _private_key_pem = f.read()
    # Pre-parse the PEM into a cryptography private key once at startup.
    # pyjwt.encode() re-parses PEM bytes on every call (~100 ms on RSA-2048),
    # which blocks the asyncio event loop and tanks throughput. Passing a
    # cryptography key object skips the re-parse → signing drops to ~2 ms.
    _private_key = load_pem_private_key(_private_key_pem, password=None)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] RSA private key loaded from {args.private_key_path}")
except Exception as e:
    print(f"[ERROR] Failed to load private key: {e}")
    raise SystemExit(1)


def create_jwt(pseudo_id, domain_id, session_nullifier, ttl):
    now = int(time.time())
    payload = {
        "pseudoID": pseudo_id,
        "domainID": int(domain_id),
        "anonymity": "pseudonymous",
        "sessionNullifier": session_nullifier,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, _private_key, algorithm="RS256")


_job_count = 0
_job_count_lock = asyncio.Lock()


async def worker(worker_id, r_token, r_proof, http_client):
    global _job_count
    while True:
        try:
            item = await r_token.brpop("verified_queue", timeout=0)
            if not item:
                continue
            _, raw = item
            entry = json.loads(raw)
            job_id = entry.get("job_id")
            public_inputs = entry.get("public_inputs", [])

            if not job_id or not public_inputs:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] worker {worker_id}: malformed entry {entry}")
                continue

            domain_raw = await r_proof.get(f"domain:{job_id}")
            try:
                domain_id = int(domain_raw) if domain_raw is not None else 0
            except (TypeError, ValueError):
                domain_id = 0
            if domain_id < 0 or domain_id > 4:
                domain_id = 0

            pseudo_id = str(public_inputs[0])
            token = create_jwt(pseudo_id, domain_id, job_id, args.token_ttl)
            app_url = APP_URLS[domain_id]

            try:
                resp = await http_client.post(
                    app_url,
                    json={"job_id": job_id, "token": token},
                )
                if resp.status_code == 200:
                    async with _job_count_lock:
                        _job_count += 1
                        if _job_count % 500 == 0:
                            print(f"[{time.strftime('%H:%M:%S')}] Delivered {_job_count} tokens")
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] [WARN] domain {domain_id} returned {resp.status_code} for job {job_id}")
                    await r_proof.set(f"status:{job_id}", "failed", ex=3600)
            except Exception as http_err:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] HTTP error to domain {domain_id} for job {job_id}: {http_err}")
                await r_proof.set(f"status:{job_id}", "failed", ex=3600)

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [ERROR] worker {worker_id}: {e}")
            await asyncio.sleep(1)


async def main():
    r_token = redis.Redis(
        host=args.token_queue_host, port=args.token_queue_port, db=0, decode_responses=True
    )
    r_proof = redis.Redis(
        host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True
    )

    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
        keepalive_expiry=60.0,
    )
    timeout = httpx.Timeout(args.http_timeout)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as http_client:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Token Issuance Worker started "
              f"(concurrency={args.concurrency}). "
              f"Consuming verified_queue @ {args.token_queue_host}:{args.token_queue_port}. "
              f"Apps: {APP_URLS}")
        workers = [
            asyncio.create_task(worker(i, r_token, r_proof, http_client))
            for i in range(args.concurrency)
        ]
        await asyncio.gather(*workers)


if __name__ == "__main__":
    asyncio.run(main())
