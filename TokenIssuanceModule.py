import redis
import json
import time
import argparse
import requests
import jwt

# Reads verified jobs from token-queue Redis (verified_queue), signs a RS256 JWT,
# and POSTs {job_id, token} to the target app's /ingest endpoint.
#
# Multi-app routing:
#   For each job, GET domain:{job_id} from proof-queue Redis to discover which
#   domain (0..4) this request was destined for. Sign the JWT with domainID=N
#   and POST to APP_URLS[N]. Defaults to 0 if missing.
#
# The app handles validation and writes status:{job_id} back to proof-queue Redis.
# This module only writes status=failed on HTTP failure (app unreachable).
#
# Queue entry format (from SNARKVerifierWorker.js):
#   {"job_id": "...", "scheme": "snark", "public_inputs": [...], "verified_at": ...}

parser = argparse.ArgumentParser(description="Token Issuance Worker")
parser.add_argument('--token-queue-host', type=str, default='token-queue')
parser.add_argument('--token-queue-port', type=int, default=6379)
parser.add_argument('--proof-queue-host', type=str, default='proof-queue')
parser.add_argument('--proof-queue-port', type=int, default=6379)
parser.add_argument('--private-key-path', type=str, default='/etc/zk-authaas/zk-authaas-key.pem')
parser.add_argument('--app-urls', type=str, required=True,
                    help='Comma-separated list of 5 app /ingest URLs (index 0..4 = domain 0..4)')
parser.add_argument('--token-ttl', type=int, default=3600)
parser.add_argument('--http-timeout', type=float, default=5.0)
args = parser.parse_args()

APP_URLS = [u.strip() for u in args.app_urls.split(',')]
if len(APP_URLS) != 5:
    print(f"[ERROR] --app-urls must contain exactly 5 comma-separated URLs (got {len(APP_URLS)})")
    raise SystemExit(1)

rTokenQueue = redis.Redis(host=args.token_queue_host, port=args.token_queue_port, db=0, decode_responses=True)
rProofQueue = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True)

# pyjwt accepts raw PEM bytes directly — no manual key parsing needed.
try:
    with open(args.private_key_path, 'rb') as f:
        _private_key_pem = f.read()
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
    return jwt.encode(payload, _private_key_pem, algorithm="RS256")


print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Token Issuance Worker started. "
      f"Consuming verified_queue @ {args.token_queue_host}:{args.token_queue_port}. "
      f"Apps: {APP_URLS}")

job_count = 0

while True:
    try:
        item = rTokenQueue.brpop("verified_queue", timeout=0)
        if not item:
            continue

        _, raw = item
        entry = json.loads(raw)
        job_id = entry.get("job_id")
        public_inputs = entry.get("public_inputs", [])

        if not job_id or not public_inputs:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Malformed entry, skipping: {entry}")
            continue

        # Look up domain_id from side-table (set by request-handler at submit)
        domain_raw = rProofQueue.get(f"domain:{job_id}")
        try:
            domain_id = int(domain_raw) if domain_raw is not None else 0
        except (TypeError, ValueError):
            domain_id = 0
        if domain_id < 0 or domain_id > 4:
            domain_id = 0

        pseudo_id = str(public_inputs[0])
        jwt_token = create_jwt(
            pseudo_id=pseudo_id,
            domain_id=domain_id,
            session_nullifier=job_id,
            ttl=args.token_ttl,
        )

        app_url = APP_URLS[domain_id]

        try:
            resp = requests.post(
                app_url,
                json={"job_id": job_id, "token": jwt_token},
                timeout=args.http_timeout,
            )
            if resp.status_code == 200:
                job_count += 1
                if job_count % 100 == 0:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Delivered {job_count} tokens to apps")
            else:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] domain {domain_id} returned {resp.status_code} for job {job_id}")
                rProofQueue.set(f"status:{job_id}", "failed", ex=3600)
        except Exception as http_err:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] HTTP error to domain {domain_id} for job {job_id}: {http_err}")
            rProofQueue.set(f"status:{job_id}", "failed", ex=3600)

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {e}")
        import traceback; traceback.print_exc()
        time.sleep(1)
