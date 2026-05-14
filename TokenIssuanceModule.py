import redis
import json
import time
import argparse
import base64
import requests
from Crypto.Signature import pkcs1_v1_5
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

# Reads verified jobs from token-queue Redis (verified_queue), signs a RS256 JWT,
# and POSTs {job_id, token} to the app's /ingest endpoint.
#
# The app handles validation and writes status:{job_id} back to proof-queue Redis.
# This module only writes to proof-queue on HTTP failure (app unreachable),
# so k6 sees "failed" rather than the job hanging at "pending" forever.
#
# Queue entry format (from SNARKVerifierWorker.js):
#   {"job_id": "...", "scheme": "snark", "public_inputs": [...], "verified_at": ...}
#
# Claim mapping:
#   pseudoID         <- public_inputs[0]
#   domainID         <- --domain-id arg (default "1")
#   anonymity        <- "pseudonymous" (fixed)
#   sessionNullifier <- job_id  (unique per job, avoids replay collision in load tests)

parser = argparse.ArgumentParser(description="Token Issuance Worker")
parser.add_argument('--token-queue-host', type=str, default='token-queue')
parser.add_argument('--token-queue-port', type=int, default=6379)
parser.add_argument('--proof-queue-host', type=str, default='proof-queue',
                    help='Used only to write status=failed when app is unreachable')
parser.add_argument('--proof-queue-port', type=int, default=6379)
parser.add_argument('--private-key-path', type=str, default='/etc/zk-authaas/zk-authaas-key.pem')
parser.add_argument('--app-url', type=str, required=True,
                    help='App /ingest endpoint, e.g. http://10.0.0.5:9000/ingest')
parser.add_argument('--domain-id', type=str, default='1')
parser.add_argument('--token-ttl', type=int, default=3600)
parser.add_argument('--http-timeout', type=float, default=5.0)
args = parser.parse_args()

rTokenQueue = redis.Redis(host=args.token_queue_host, port=args.token_queue_port, db=0, decode_responses=True)
rProofQueue = redis.Redis(host=args.proof_queue_host, port=args.proof_queue_port, db=0, decode_responses=True)

try:
    with open(args.private_key_path, 'rb') as f:
        private_key = RSA.import_key(f.read())
    signer = pkcs1_v1_5.new(private_key)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] RSA private key loaded ({private_key.size_in_bits()} bits)")
except Exception as e:
    print(f"[ERROR] Failed to load private key: {e}")
    raise SystemExit(1)


def b64url_encode(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')


def create_jwt(pseudo_id, domain_id, session_nullifier, ttl):
    header_enc = b64url_encode(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(',', ':')))
    now = int(time.time())
    exp = now + ttl
    payload = {
        "pseudoID": pseudo_id,
        "domainID": domain_id,
        "anonymity": "pseudonymous",
        "sessionNullifier": session_nullifier,
        "iat": now,
        "exp": exp,
    }
    payload_enc = b64url_encode(json.dumps(payload, separators=(',', ':')))
    message = f"{header_enc}.{payload_enc}".encode('utf-8')
    sig_enc = b64url_encode(signer.sign(SHA256.new(message)))
    return f"{header_enc}.{payload_enc}.{sig_enc}"


print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Token Issuance Worker started. "
      f"Consuming verified_queue @ {args.token_queue_host}:{args.token_queue_port}. "
      f"Delivering to {args.app_url}")

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

        pseudo_id = str(public_inputs[0])
        jwt_token = create_jwt(
            pseudo_id=pseudo_id,
            domain_id=args.domain_id,
            session_nullifier=job_id,
            ttl=args.token_ttl,
        )

        # POST to app /ingest — app enqueues, validates async, writes status back
        try:
            resp = requests.post(
                args.app_url,
                json={"job_id": job_id, "token": jwt_token},
                timeout=args.http_timeout,
            )
            if resp.status_code == 200:
                job_count += 1
                if job_count % 100 == 0:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Delivered {job_count} tokens to app")
            else:
                # App returned an error — write failed ourselves so k6 doesn't hang
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] App returned {resp.status_code} for job {job_id}")
                rProofQueue.set(f"status:{job_id}", "failed", ex=3600)
        except Exception as http_err:
            # App unreachable — write failed so k6 doesn't hang at "pending"
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] HTTP error for job {job_id}: {http_err}")
            rProofQueue.set(f"status:{job_id}", "failed", ex=3600)

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {e}")
        import traceback; traceback.print_exc()
        time.sleep(1)
