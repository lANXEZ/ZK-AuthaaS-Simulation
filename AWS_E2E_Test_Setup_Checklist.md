# ZK-AuthaaS End-to-End Test Setup Checklist

> **Scope:** Full pipeline including token issuance and app-side validation.  
> **Old checklist (SNARK-only, no token pipeline):** `AWS_Spot_Swarm_Setup_Checklist.md` — kept intact.

---

## EC2 Topology

| Role         | Instance    | vCPU | RAM   | Purpose                                      |
|--------------|-------------|------|-------|----------------------------------------------|
| **Manager**  | c5.xlarge   | 4    | 8 GB  | Swarm manager · Redis brokers · token-issuer |
| **Worker**   | c5.9xlarge  | 36   | 72 GB | 32 SNARK verifiers (1 vCPU each)             |
| **k6**       | c5.large    | 2    | 4 GB  | Load generator                               |
| **App**      | c5.xlarge   | 4    | 8 GB  | TokenValidatorService (5 future apps → 1 now)|

Estimated spot cost: **~$0.59/hr** → **~$3.85/session** (6.5 hr including setup/teardown).

---

## Step 0 — Pre-launch (local machine)

- [ ] Confirm `zk-authaas-key.pem` and `zk-authaas-public.pem` exist in project root
  - Already generated — do **not** regenerate (would invalidate the public key on the App EC2)
- [ ] Note `zk-authaas-key.pem` is in `.gitignore` — never commit it
- [ ] Have your EC2 key pair (`zk-authaas-ec2-key.pem`) ready for SSH

---

## Step 1 — Launch EC2 Instances (AWS Console / CLI)

Launch all four instances as **Spot** instances in the **same VPC and subnet** (so private IPs can reach each other).

```
Manager : c5.xlarge   — Amazon Linux 2023, 30 GB gp3
Worker  : c5.9xlarge  — Amazon Linux 2023, 30 GB gp3
k6      : c5.large    — Amazon Linux 2023, 20 GB gp3
App     : c5.xlarge   — Amazon Linux 2023, 20 GB gp3
```

Security group rules (all within VPC):
- Manager ← Worker, k6 : port 2377 (Swarm join), 6379-6382 (Redis), 8000 (API)
- Manager ← App         : port 6379 (proof-queue Redis write-back from validator)
- App     ← Manager     : port 9000 (token-issuer POST /ingest)
- k6      → Manager     : port 8000
- SSH: port 22 from your IP

Record private IPs — you will need them:
```
MANAGER_IP=<private-ip>
WORKER_IP=<private-ip>
K6_IP=<private-ip>
APP_IP=<private-ip>
```

---

## Step 2 — App EC2 Setup

```bash
# SSH into App EC2
ssh -i zk-authaas-ec2-key.pem ec2-user@<app-public-ip>

# Install Docker
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker

# Transfer files from local machine (run locally):
scp -i zk-authaas-ec2-key.pem \
    zk-authaas-public.pem \
    TokenValidatorService.py \
    Dockerfile.token-validator \
    docker-compose.app.yml \
    ec2-user@<app-public-ip>:~/zk-authaas/

# Back on App EC2:
cd ~/zk-authaas

# Set manager private IP so the validator can write status back to proof-queue
export PROOF_QUEUE_HOST=<MANAGER_IP>

docker compose -f docker-compose.app.yml build
docker compose -f docker-compose.app.yml up -d

# Verify
curl http://localhost:9000/health
# Expected: {"status":"ok","queue_size":0,"workers":4}
```

---

## Step 3 — Manager EC2 Setup

```bash
ssh -i zk-authaas-ec2-key.pem ec2-user@<manager-public-ip>

# Install Docker
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker

# Transfer project files (run locally):
rsync -av -e "ssh -i zk-authaas-ec2-key.pem" \
    --exclude '.venv' --exclude '.git' \
    "E:/Work/VSCode Repo/ZK-AuthaaS Simulation/" \
    ec2-user@<manager-public-ip>:~/zk-authaas/
# zk-authaas-key.pem is included in the rsync (it's excluded from git only)

# Init Swarm
cd ~/zk-authaas
docker swarm init --advertise-addr $MANAGER_IP
# Save the join-token shown in the output
```

---

## Step 4 — Worker EC2 Setup

```bash
ssh -i zk-authaas-ec2-key.pem ec2-user@<worker-public-ip>

# Install Docker
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker

# Increase inotify watches for snarkjs file watching
sudo sysctl -w fs.inotify.max_user_watches=524288
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf

# Join Swarm (use token from Step 3)
docker swarm join --token <SWARM_JOIN_TOKEN> $MANAGER_IP:2377
```

---

## Step 5 — Label Nodes (on Manager)

```bash
# Get worker node ID
docker node ls

# Label nodes
docker node update --label-add pool=snark <WORKER_NODE_ID>
```

---

## Step 6 — Build Images

```bash
# On Manager (builds manager-side images):
cd ~/zk-authaas
docker compose build request-handler verifier-selector token-issuer

# On Worker (SSH in):
cd ~/zk-authaas
docker compose build snark-verifier
```

---

## Step 7 — Deploy Stack

Set the App EC2 private IP before deploying:

```bash
# On Manager:
export APP_EC2_IP=<APP_IP>

docker stack deploy -c docker-compose.yml zk
```

Scale STARK to 0 (SNARK-only test):

```bash
docker service scale zk_stark-verifier=0
```

Verify all services are up:

```bash
docker service ls
# Expected replicas:
#   zk_proof-queue        1/1
#   zk_snark-queue        1/1
#   zk_stark-queue        1/1
#   zk_token-queue        1/1
#   zk_request-handler    8/8
#   zk_verifier-selector  1/1
#   zk_snark-verifier    32/32
#   zk_stark-verifier     0/0
#   zk_token-issuer       4/4
```

---

## Step 8 — Smoke Test (Manual)

```bash
# Submit a proof job
curl -s -X POST http://$MANAGER_IP:8000/verify/submit \
  -H "Content-Type: application/json" \
  -d '{"proof":{},"publicSignals":["12345"]}' | jq .

# Poll until completed (replace <JOB_ID>)
# status is written back by the App validator after async validation
curl -s http://$MANAGER_IP:8000/verify/status/<JOB_ID> | jq .
# Expected: {"job_id":"...","status":"completed"}

# Check app queue is draining (should stay near 0 under steady load)
curl -s http://$APP_IP:9000/health | jq .
# Expected: {"status":"ok","queue_size":0,"workers":4}
```

---

## Step 9 — Load Test (k6)

```bash
# SSH into k6 EC2
ssh -i zk-authaas-ec2-key.pem ec2-user@<k6-public-ip>

# Install k6
sudo dnf install -y https://dl.k6.io/rpm/repo.rpm
sudo dnf install -y k6

# Transfer load_test.js (run locally):
scp -i zk-authaas-ec2-key.pem load_test.js ec2-user@<k6-public-ip>:~/

# Run sweep (adjust VUs / duration)
# KNEE_VU is expected around 400-500 for 32 SNARK workers
k6 run --vus 200 --duration 60s \
  -e TARGET_URL=http://$MANAGER_IP:8000 \
  load_test.js
```

---

## Step 10 — Teardown

```bash
# Remove stack
docker stack rm zk

# Terminate EC2 instances (AWS Console or CLI):
aws ec2 terminate-instances --instance-ids <id1> <id2> <id3> <id4>
```

---

## Expected Performance

| Metric                       | Value                  |
|------------------------------|------------------------|
| SNARK workers                | 32                     |
| Peak verification throughput | ~736 req/s (all 32 busy) |
| Token-issuer replicas        | 4 (manager-pinned)     |
| KNEE_VU                      | ~400–500 VUs           |
| Token TTL                    | 3600 s (1 hr)          |
| App validator instances      | 1 (c5.xlarge)          |

---

## Data Flow Summary

```
Client
  │ POST /verify/submit
  ▼
request-handler ──lpush──► proof_queue (proof-queue Redis:6379)
                              │
                              ▼
                     verifier-selector ──lpush──► snark-job-queue (snark-queue:6379)
                                                    │
                                                    ▼
                                             SNARKVerifierWorker (×32, worker node)
                                               │ success
                                               ├──lpush──► verified_queue (token-queue:6382)
                                               │ failure
                                               └──set status:failed (proof-queue:6379)
                                                    │
                                                    ▼
                                             token-issuer (×4, manager)
                                               │ signs RS256 JWT
                                               │ POST /ingest {job_id, token}
                                               ▼
                                        TokenValidatorService (App EC2:9000)
                                               │ enqueues immediately → 200 fast
                                               │
                                               │ background validation worker (×4)
                                               │   dequeue → verify signature + expiry + domainID
                                               │
                                               ├─ valid:   set status:{job_id}="completed" ─┐
                                               └─ invalid: set status:{job_id}="failed"      │
                                                                                              │ write-back to
                                                                                              │ proof-queue Redis
                                                                                              │ (Manager:6379)
                              │
Client polls GET /verify/status/{job_id}
  └── sees "completed" when app validation succeeds
```
