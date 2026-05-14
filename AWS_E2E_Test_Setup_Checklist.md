# ZK-AuthaaS End-to-End Test Setup Checklist

> **Scope:** Full pipeline including token issuance and app-side validation.  
> **Old checklist (SNARK-only, no token pipeline):** `AWS_Spot_Swarm_Setup_Checklist.md` — kept intact.

---

## EC2 Topology

| Role            | Instance    | Count | vCPU | RAM   | Purpose                                              |
|-----------------|-------------|-------|------|-------|------------------------------------------------------|
| **Manager**     | c5.xlarge   | 1     | 4    | 8 GB  | Swarm manager · Redis brokers · token-issuer         |
| **Worker**      | c5.9xlarge  | 1     | 36   | 72 GB | 32 SNARK verifiers (1 vCPU each)                     |
| **k6**          | c5.large    | 1     | 2    | 4 GB  | Load generator                                       |
| **App 0..4**    | c5.xlarge   | 5     | 4    | 8 GB  | TokenValidatorService, one per `domainID` (0..4)     |

Total: 8 EC2 instances. Estimated spot cost: **~$0.84/hr** → **~$5.50/session** (6.5 hr including setup/teardown).

Each app validates only tokens whose `domainID` claim matches its assigned ID — cross-app token rejection is enforced.

---

## Step 0 — Pre-launch (local machine)

- [ ] Confirm `zk-authaas-key.pem` and `zk-authaas-public.pem` exist in project root
  - Already generated — do **not** regenerate (would invalidate the public key on the App EC2)
- [ ] Note `zk-authaas-key.pem` is in `.gitignore` — never commit it
- [ ] Have your EC2 key pair (`zk-authaas-key.pem`) ready for SSH

---

## Step 1 — Launch EC2 Instances (AWS Console / CLI)

Launch all 8 instances as **Spot** instances in the **same VPC and subnet** (so private IPs can reach each other).

```
Manager   : c5.xlarge   — Ubuntu 22.04 LTS, 30 GB gp3
Worker    : c5.9xlarge  — Ubuntu 22.04 LTS, 30 GB gp3
k6        : c5.large    — Ubuntu 22.04 LTS, 20 GB gp3
App 0..4  : c5.xlarge   — Ubuntu 22.04 LTS, 20 GB gp3  (5 identical instances, name them app-0 through app-4)
```

Security group rules (all within VPC):
- Manager ← Worker, k6     : port 2377 (Swarm join), 6379-6382 (Redis), 8000 (API)
- Manager ← App 0..4       : port 6379 (proof-queue Redis write-back from each validator)
- App 0..4 ← Manager       : port 9000 (token-issuer POST /ingest)
- k6      → Manager        : port 8000
- SSH: port 22 from your IP

> **Tip:** put all 8 instances in a single security group that allows "All traffic" from itself — same approach as the SNARK-only checklist. Saves rule sprawl.

Record private IPs — you will need them:
```
MANAGER_IP=<private-ip>
WORKER_IP=<private-ip>
K6_IP=<private-ip>
APP0_IP=<private-ip>
APP1_IP=<private-ip>
APP2_IP=<private-ip>
APP3_IP=<private-ip>
APP4_IP=<private-ip>
```

---

## Step 2 — App EC2 Setup (repeat for all 5 apps)

Repeat the steps below for each app, using `N=0` for app-0, `N=1` for app-1, ..., `N=4` for app-4.
All apps share the same `docker-compose.app.yml` — only `DOMAIN_ID` differs.

```bash
# SSH into App N's EC2
ssh -i zk-authaas-key.pem ubuntu@<appN-public-ip>

# Install Docker
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-key.pem ubuntu@<appN-public-ip>
mkdir -p ~/zk-authaas
```

Transfer files from your local machine (one `scp` per app, or scripted):
```bash
# Local machine — run once per app (replace N and IP)
scp -i zk-authaas-key.pem \
    zk-authaas-public.pem \
    TokenValidatorService.py \
    Dockerfile.token-validator \
    docker-compose.app.yml \
    ubuntu@<appN-public-ip>:~/zk-authaas/
```

Then on each App EC2:
```bash
cd ~/zk-authaas

# IMPORTANT: each app gets its own DOMAIN_ID (0..4 matching the EC2 number)
export DOMAIN_ID=<N>                         # 0, 1, 2, 3, or 4
export PROOF_QUEUE_HOST=<MANAGER_IP>

docker compose -f docker-compose.app.yml build
docker compose -f docker-compose.app.yml up -d

# Verify
curl http://localhost:9000/health
# Expected: {"status":"ok","queue_size":0,"workers":4}
```

> **Sanity check:** SSH into app-2, run `docker compose logs token-validator | head -5` and confirm
> the startup line shows it loaded the public key and is bound to `domainID=2`.

---

## Step 3 — Manager EC2 Setup

```bash
ssh -i zk-authaas-key.pem ubuntu@<manager-public-ip>

# Install Docker
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-key.pem ubuntu@<manager-public-ip>

# Transfer project files (run locally):
rsync -av -e "ssh -i zk-authaas-key.pem" \
    --exclude '.venv' --exclude '.git' \
    "E:/Work/VSCode Repo/ZK-AuthaaS Simulation/" \
    ubuntu@<manager-public-ip>:~/zk-authaas/
# zk-authaas-key.pem is included in the rsync (it's excluded from git only)

# Init Swarm
cd ~/zk-authaas
docker swarm init --advertise-addr $MANAGER_IP
# Save the join-token shown in the output
```

---

## Step 4 — Worker EC2 Setup

```bash
ssh -i zk-authaas-key.pem ubuntu@<worker-public-ip>

# Install Docker
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-key.pem ubuntu@<worker-public-ip>

# Increase inotify watches for snarkjs file watching
sudo sysctl -w fs.inotify.max_user_watches=524288
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf

# Join Swarm (use token from Step 3)
docker swarm join --token <SWARM_JOIN_TOKEN> $MANAGER_IP:2377
```

---

## Step 5 — k6 EC2 Setup

```bash
ssh -i zk-authaas-key.pem ubuntu@<k6-public-ip>

# Install k6
sudo apt update && sudo apt install -y gpg curl
curl -s https://dl.k6.io/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/k6-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update && sudo apt install -y k6

# Transfer load_test.js (run locally):
scp -i zk-authaas-key.pem load_test.js ubuntu@<k6-public-ip>:~/
```

---

## Step 6 — Label Nodes (on Manager)

```bash
# Get worker node ID
docker node ls

# Label worker node for SNARK pool placement
docker node update --label-add pool=snark <WORKER_NODE_ID>
```

---

## Step 7 — Build Images

```bash
# On Manager:
cd ~/zk-authaas
docker compose build request-handler verifier-selector token-issuer

# On Worker (SSH in):
cd ~/zk-authaas
docker compose build snark-verifier
```

---

## Step 8 — Deploy Stack

```bash
# On Manager — set ALL 5 app private IPs before deploying:
export APP0_IP=<app0-private-ip>
export APP1_IP=<app1-private-ip>
export APP2_IP=<app2-private-ip>
export APP3_IP=<app3-private-ip>
export APP4_IP=<app4-private-ip>

docker stack deploy -c docker-compose.yml zk

# Scale STARK to 0 (SNARK-only test)
docker service scale zk_stark-verifier=0
```

> Verify the token-issuer received all 5 URLs:
> ```bash
> docker service logs zk_token-issuer --tail 5
> # Should show: Apps: ['http://10.0.0.X:9000/ingest', ..., 'http://10.0.0.Y:9000/ingest']
> ```

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

## Step 9 — Quick Sanity Check (from Manager)

Confirm each of the 5 apps receives its own traffic.

```bash
# On Manager EC2 — submit one job per domain
for N in 0 1 2 3 4; do
  curl -s -X POST http://localhost:8000/verify/submit \
    -H "Content-Type: application/json" \
    -d "{\"scheme\":\"snark\",\"proof\":{},\"public_inputs\":[\"12345\"],\"domain_id\":$N}" | jq .
done

# Wait ~3s, then poll one of the job_ids
curl -s http://localhost:8000/verify/status/<JOB_ID> | jq .
# Expected: {"job_id":"...","status":"completed"}

# Confirm each app saw traffic (their /health queue_size resets to 0 fast)
for IP in $APP0_IP $APP1_IP $APP2_IP $APP3_IP $APP4_IP; do
  echo "--- $IP ---"
  curl -s http://$IP:9000/health | jq .
done
```

> **Cross-app rejection test:** submit `domain_id=1` and inspect the App 2 logs — it should never have received that job. If it did, the routing logic is broken.

---

## Step 10 — Prepare k6 EC2

```bash
# SSH into k6 EC2
ssh -i zk-authaas-key.pem ubuntu@<k6-public-ip>

# Raise file descriptor limit — each VU holds one open connection.
# The OS default of 1024 causes k6 to freeze above ~800 VUs.
# Run this in every shell session before any sweep.
ulimit -n 65536
```

Transfer scripts from your **local machine**:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" \
  load_test.js \
  sweep_throughput.py \
  ubuntu@<k6-public-ip>:~/
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" `
  load_test.js `
  sweep_throughput.py `
  ubuntu@<k6-public-ip>:~/
```

> **Tip — use tmux so SSH disconnects don't kill a running sweep:**
> ```bash
> tmux new -s sweep      # start a named session
> ulimit -n 65536        # set limit inside tmux
> # run your sweep here
> # if SSH disconnects: re-SSH, then:
> tmux attach -t sweep   # re-attach to the running session
> ```

---

## Step 11 — Smoke Test (End-to-End Pipeline)

Before sweeping, confirm the full pipeline works with a small run:

```bash
# On k6 EC2 (inside tmux, after ulimit)
k6 run \
  -e TARGET=<manager-private-ip> \
  -e VUS=10 \
  -e ITERATIONS=50 \
  -e STARK_RATIO=0.0 \
  load_test.js
```

**Expected:** k6 exits cleanly, `failed_verifications=0`, `submit_failures=0`.

> If `failed_verifications` > 0, check:
> - App EC2 is running: `curl http://<app-ip>:9000/health`
> - App can reach proof-queue Redis: check `docker compose logs` on App EC2
> - token-issuer is posting to app: `docker service logs zk_token-issuer --tail 20`

---

## Customizing the Distribution Pattern

`load_test.js` has a `PHASES` array near the top — each phase has a duration (seconds) and a 5-element weight vector for domains 0..4. Edit it before running k6:

```javascript
const PHASES = [
  { duration: 15, weights: [60, 10, 10, 10, 10] },   // domain 0 hot for 15s
  { duration: 15, weights: [10, 60, 10, 10, 10] },   // shift to domain 1
  { duration: 15, weights: [10, 10, 60, 10, 10] },   // shift to domain 2
  { duration: 15, weights: [10, 10, 10, 60, 10] },   // shift to domain 3
  { duration: 15, weights: [10, 10, 10, 10, 60] },   // shift to domain 4
  { duration: 60, weights: [20, 20, 20, 20, 20] },   // even split
];
```

- Weights are **relative** — they do not need to sum to 100.
- After all phases elapse, the last phase's weights are used until the test ends.
- Every metric is tagged with `domain=N` so the CSV output can be sliced per domain.

---

## Step 12 — VU Sweep (Find KNEE_VU)

Run a throughput sweep across VU levels to find the knee point.
Expected KNEE_VU for 32 SNARK workers: **~400–500 VUs**.

```bash
# On k6 EC2 (inside tmux)
ulimit -n 65536
python3 sweep_throughput.py \
  --target <manager-private-ip> \
  --vus 50,100,200,300,400,500,600,800,1000,1500 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_e2e_baseline.csv \
  --clean
```

Copy results back and plot:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_e2e_baseline.csv .
python visualize_sweep.py --input sweep_e2e_baseline.csv --output sweep_e2e_baseline_graph.png
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_e2e_baseline.csv .
python visualize_sweep.py --input sweep_e2e_baseline.csv --output sweep_e2e_baseline_graph.png
```

📝 **Record `KNEE_VU`** — the VU level just before throughput flattens.

---

## Step 13 — Detailed Time-Series Run

Single long run at `KNEE_VU` to capture the full time-series CSV:

```bash
# On k6 EC2 (inside tmux)
ulimit -n 65536
k6 run \
  -e TARGET=<manager-private-ip> \
  -e VUS=<KNEE_VU> \
  -e ITERATIONS=<KNEE_VU * 200> \
  -e STARK_RATIO=0.0 \
  load_test.js \
  --out csv=test_results_e2e.csv
```

> With KNEE_VU ~400–500, `KNEE_VU * 200` gives 80 000–100 000 iterations. Expected runtime ~2–3 minutes.

Copy back and visualize:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e.csv .

# Overall time-series (all apps combined)
python visualize_k6.py

# Per-domain throughput & latency (uses the domain=N tags k6 wrote into the CSV)
python visualize_k6_per_app.py --input test_results_e2e.csv --output per_domain_graph.png
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e.csv .

python visualize_k6.py
python visualize_k6_per_app.py --input test_results_e2e.csv --output per_domain_graph.png
```

The per-domain script prints a summary table (completed / failed / p50 / p95 / p99 per domain) and saves a two-panel PNG: throughput-over-time and p95-latency-over-time, one line per domain. Phase transitions should be visible as the dominant domain changes.

---

## Step 14 — Teardown (every session, no exceptions)

```bash
# On Manager:
docker stack rm zk
docker swarm leave --force

# On each App EC2 (0..4):
docker compose -f docker-compose.app.yml down

# Terminate all 8 EC2 instances (AWS Console or CLI):
aws ec2 terminate-instances --instance-ids <manager-id> <worker-id> <k6-id> <app0-id> <app1-id> <app2-id> <app3-id> <app4-id>
```

---

## Expected Performance

| Metric                       | Value                    |
|------------------------------|--------------------------|
| SNARK workers                | 32                       |
| Peak verification throughput | ~736 req/s (all 32 busy) |
| Token-issuer replicas        | 4 (manager-pinned)       |
| KNEE_VU                      | ~400–500 VUs             |
| Token TTL                    | 3600 s (1 hr)            |
| App validator instances      | 5 (one per domainID 0..4)|
| Per-app peak throughput      | ~147 req/s (even split)  |

---

## Data Flow Summary

```
k6 picks domain_id=N (0..4) per request based on current phase weights
  │ POST /verify/submit {proof, public_inputs, domain_id: N}
  ▼
request-handler  set domain:{job_id} = N         (side-table on proof-queue Redis)
                 lpush proof_queue (proof-queue Redis:6379)
                              │
                              ▼
                     verifier-selector  (unchanged — no domain awareness)
                              │  lpush snark-job-queue (snark-queue:6379)
                              ▼
                     SNARKVerifierWorker (×32, worker node) (unchanged)
                              │ success
                              ├──lpush──► verified_queue (token-queue:6382)
                              │ failure
                              └──set status:failed (proof-queue:6379)
                              │
                              ▼
                     token-issuer (×4, manager)
                              │ GET domain:{job_id} → N
                              │ sign JWT with domainID=N
                              │ POST /ingest → APP_URLS[N]
                              ▼
                     TokenValidatorService N  (App N EC2:9000, --expected-domain-id N)
                              │ enqueues immediately → 200 fast
                              │
                              │ background validation worker (×4)
                              │   dequeue → verify signature + expiry + domainID == N
                              │
                              ├─ valid:   set status:{job_id}="completed" ─┐
                              └─ invalid: set status:{job_id}="failed"      │ proof-queue Redis
                                                                              │ (Manager:6379)
                              │
k6 polls GET /verify/status/{job_id} → sees "completed"
  └── records latency tagged with domain=N → per-domain metrics in CSV
```
