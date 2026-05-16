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

- [ ] Confirm `zk-authaas-ec2-key.pem` and `zk-authaas-public.pem` exist in project root
  - Already generated — do **not** regenerate (would invalidate the public key on the App EC2)
- [ ] Note `zk-authaas-ec2-key.pem` is in `.gitignore` — never commit it
- [ ] Have your EC2 key pair (`zk-authaas-ec2-key.pem`) ready for SSH

---

## Step 1 — Create the shared security group and launch EC2 instances

### Step 1a — Create the shared security group

All 8 instances (manager, worker, k6, and all 5 apps) **must share a single security group**. Docker Swarm overlay networking (VXLAN), Redis cross-node access, and token-issuer → app HTTP traffic all use multiple ports — the easiest and most reliable approach is a self-referencing "All traffic" rule that lets any instance in the group talk freely to any other.

> ⚠️ **Do not create separate security groups per role.** If manager and worker are in different groups, Swarm intra-cluster traffic is blocked and container scheduling will fail silently. If the app EC2s are in a separate group, token-issuer `/ingest` POSTs and validator write-backs to proof-queue Redis will both be dropped.

**In the AWS Console — EC2 → Security Groups → Create security group:**

| Field | Value |
|---|---|
| Name | `zk-authaas-cluster-sg` |
| Description | Shared SG for all ZK-AuthaaS E2E nodes (manager, worker, k6, apps 0–4) |
| VPC | *(your default VPC — the same one all 8 EC2s will go into)* |

**Inbound rules — add all three:**

| # | Type | Protocol | Port | Source | What to enter in the Source field | Purpose |
|---|---|---|---|---|---|---|
| 1 | SSH | TCP | 22 | My IP | Click **"My IP"** in the dropdown — AWS fills in your current public IP automatically | SSH from your laptop to any of the 8 EC2s |
| 2 | Custom TCP | TCP | 8000 | My IP | Same as rule 1 — click **"My IP"** | Direct `curl` to the FastAPI backend from your laptop (sanity checks) |
| 3 | All traffic | All | All | Custom | Type `sg-` in the Source box, then select **this same security group** from the autocomplete dropdown (e.g. `sg-0abc123def456 / zk-authaas-cluster-sg`). This is the self-referencing rule. | All inter-node traffic: Swarm, Redis, token-issuer → /ingest, app write-backs to proof-queue |

> **Where to find each value:**
> - **My IP (rules 1 & 2):** AWS fills this automatically when you select "My IP" — no need to look it up. If your laptop IP changes between sessions, edit rules 1 & 2 and click "My IP" again.
> - **Security group ID (rule 3):** EC2 Console → Security Groups → click `zk-authaas-cluster-sg` → copy the **Security group ID** at the top (`sg-xxxxxxxxxxxxxxxxx`). Then type `sg-` in the Source field and pick it from the dropdown.

> **Rule 3 (self-referencing) must be added after the group is first saved:**
> 1. Save the new SG — note its ID (`sg-xxxxxxxxxxxxxxxxx`)
> 2. Edit inbound rules → Add rule
> 3. Type: **All traffic** · Source: type `sg-` and select **this same security group** from the dropdown
> 4. Save rules

**You do not need to open these ports individually.** Rule 3 already covers all of them — this table is for reference only:

| Port(s) | Protocol | Used by |
|---|---|---|
| 2377 | TCP | Swarm cluster management (`docker swarm join`, leader election) |
| 7946 | TCP + UDP | Container network discovery (gossip between Swarm nodes) |
| 4789 | UDP | VXLAN overlay — tunnel carrying all container-to-container traffic across nodes |
| 6379 | TCP | proof-queue Redis — request-handler writes job metadata; app validators write `status:{job_id}` back |
| 6380 | TCP | snark-queue Redis — selector → SNARK verifiers |
| 6381 | TCP | stark-queue Redis (unused in SNARK-only mode, reserved) |
| 6382 | TCP | token-queue Redis — SNARK verifiers push to `verified_queue`; token-issuer consumes from it |
| 8000 | TCP | FastAPI request-handler — k6 submits proofs and polls job status here |
| 9000 | TCP | TokenValidatorService on each App EC2 — token-issuer POSTs `/ingest` here |

---

### Step 1b — Launch all 8 EC2 instances

All 8 instances go into the **same VPC, same subnet (same Availability Zone), same security group `zk-authaas-cluster-sg`**. Same-AZ placement matters because cross-AZ overlay traffic adds latency and AWS data-transfer fees.

**Manager EC2:**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-manager` · AMI: **Ubuntu 22.04 LTS** · Type: **`c5.xlarge`** (4 vCPU / 8 GiB)
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`**
- [ ] Storage: **30 GiB gp3**
- [ ] Record **Public IPv4** (for SSH) and **Private IPv4** (for inter-node and k6 communication)

**Worker EC2:**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-worker` · AMI: **Ubuntu 22.04 LTS** · Type: **`c5.9xlarge`** (36 vCPU / 72 GiB)
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`** (same as manager)
- [ ] **Same VPC and Subnet as manager** (verify the AZ matches)
- [ ] Storage: **30 GiB gp3**
- [ ] Record **Public IPv4** and **Private IPv4**

**k6 loader EC2:**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-k6` · AMI: **Ubuntu 22.04 LTS** · Type: **`c5.large`** (2 vCPU / 4 GiB)
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`** (the self-referencing rule lets it reach the manager on :8000 automatically)
- [ ] Same VPC and Subnet as the backends
- [ ] Storage: **20 GiB gp3**
- [ ] Record **Public IPv4** (for SSH and `scp`)

**App EC2s — repeat for each domain (5 instances total):**

All 5 are identical in configuration. The `DOMAIN_ID` (0–4) is set at runtime via an environment variable — nothing is baked into the image.

- [ ] EC2 → Launch Instance — **repeat this block 5 times**, naming each `zk-authaas-app-0` through `zk-authaas-app-4`
- [ ] AMI: **Ubuntu 22.04 LTS** · Type: **`c5.xlarge`** (4 vCPU / 8 GiB)
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`** (same group — covers `/ingest` and proof-queue write-backs)
- [ ] Same VPC and Subnet as manager
- [ ] Storage: **20 GiB gp3**
- [ ] Record **Public IPv4** (for SSH/`scp`) and **Private IPv4** (token-issuer routes to this IP)

> **Tip — launch all 5 app instances in one go:** on the Launch Instance page set **Number of instances** to 5, then rename each one from the EC2 console after launch.
>
> **How to rename after launch (Instances list view):**
> 1. Go to **EC2 → Instances** — the 5 new instances will all share the same name (or be unnamed)
> 2. Hover over the **Name cell** of the first instance — a **pencil icon ✏️** appears to the right
> 3. Click the pencil → type `zk-authaas-app-0` → press **Enter** or click the **✓** checkmark
> 4. Repeat for the remaining four, naming them `zk-authaas-app-1` through `zk-authaas-app-4`
>
> You do not need to open the instance detail page — the rename is done entirely from the list view.

---

### Step 1c — Record all private IPs

Fill this in immediately after all 8 instances reach the `running` state. You will need these values in every subsequent step.

```bash
# Fill in your actual private IPs — keep this block handy throughout the session
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

### Step 1d — Connectivity sanity check

Run this before starting any Docker setup — it catches security group misconfiguration early.

```bash
# SSH into manager
ssh -i zk-authaas-ec2-key.pem ubuntu@<manager-public-ip>

# Ping every other node by private IP
ping -c3 $WORKER_IP
# Expected: 0% packet loss

ping -c3 $K6_IP
# Expected: 0% packet loss

for IP in $APP0_IP $APP1_IP $APP2_IP $APP3_IP $APP4_IP; do
  echo "--- $IP ---"
  ping -c2 $IP
done
# Expected: 0% packet loss for all 5 app nodes
```

If any ping fails, re-check that **all 8 EC2s are in `zk-authaas-cluster-sg`** and that rule 3 (self-referencing All traffic) was saved correctly. Do not proceed to Step 2 until all nodes can reach each other.

---

## Step 2 — App EC2 Setup (repeat for all 5 apps)

Repeat the steps below for each app, using `N=0` for app-0, `N=1` for app-1, ..., `N=4` for app-4.
All apps share the same `docker-compose.app.yml` — only `DOMAIN_ID` differs.

```bash
# SSH into App N's EC2
ssh -i zk-authaas-ec2-key.pem ubuntu@<appN-public-ip>

# Install Docker
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-ec2-key.pem ubuntu@<appN-public-ip>
mkdir -p ~/zk-authaas
```

Transfer files from your local machine (one `scp` per app, or scripted):
```powershell
# Run from your local machine — once per app (replace N and the IP)
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" `
    zk-authaas-public.pem `
    TokenValidatorService.py `
    Dockerfile.token-validator `
    docker-compose.app.yml `
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

**On the manager EC2:**
```bash
ssh -i zk-authaas-ec2-key.pem ubuntu@<manager-public-ip>

# Install Docker
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit
```

**On your local machine — transfer the project files:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -r -i "zk-authaas-ec2-key.pem" . ubuntu@<manager-public-ip>:~/zk-authaas/
# Copies everything including zk-authaas-ec2-key.pem (excluded from git but needed on the manager).
# If .venv exists and is large, delete it on the manager afterwards:
#   ssh ubuntu@<manager-public-ip> "rm -rf ~/zk-authaas/.venv"
```

**Re-SSH and initialise the Swarm:**
```bash
ssh -i zk-authaas-ec2-key.pem ubuntu@<manager-public-ip>

# Init Swarm
cd ~/zk-authaas
docker swarm init --advertise-addr $MANAGER_IP
# Save the join-token shown in the output
```

---

## Step 4 — Worker EC2 Setup

```bash
ssh -i zk-authaas-ec2-key.pem ubuntu@<worker-public-ip>

# Install Docker
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-ec2-key.pem ubuntu@<worker-public-ip>

# Increase inotify watches for snarkjs file watching
sudo sysctl -w fs.inotify.max_user_watches=524288
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf

# Join Swarm (use token from Step 3)
docker swarm join --token <SWARM_JOIN_TOKEN> $MANAGER_IP:2377
```

---

## Step 5 — k6 EC2 Setup

**On the k6 EC2:**
```bash
ssh -i zk-authaas-ec2-key.pem ubuntu@<k6-public-ip>

# Install k6
sudo apt update && sudo apt install -y gpg curl
curl -s https://dl.k6.io/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/k6-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update && sudo apt install -y k6
```

**On your local machine — transfer the load test script:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" load_test.js ubuntu@<k6-public-ip>:~/
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
ssh -i zk-authaas-ec2-key.pem ubuntu@<k6-public-ip>

# Raise file descriptor limit — each VU holds one open connection.
# The OS default of 1024 causes k6 to freeze above ~800 VUs.
# Run this in every shell session before any sweep.
ulimit -n 65536
```

Transfer scripts from your **local machine**:

```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" `
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
  load_test.js
```

**Expected:** k6 exits cleanly, `submit_failures{count}<10` threshold passes, no errors in summary.

> If jobs don't complete, check:
> - App EC2 is running: `curl http://<app-ip>:9000/health`
> - App can reach proof-queue Redis: `docker compose -f docker-compose.app.yml logs token-validator | tail -20` on App EC2
> - token-issuer is posting to apps: `docker service logs zk_token-issuer --tail 20`

---

## Customizing the Distribution Pattern

`load_test.js` has a `PHASES` array near the top — each phase has a duration (seconds) and a 5-element weight vector for domains 0..4. Edit it before running k6:

```javascript
// Current default — 20 s focus window rotating through each domain
const PHASES = [
  { duration: 20, weights: [60, 10, 10, 10, 10] },   // 0–20 s   domain 0 hot
  { duration: 20, weights: [10, 60, 10, 10, 10] },   // 20–40 s  domain 1 hot
  { duration: 20, weights: [10, 10, 60, 10, 10] },   // 40–60 s  domain 2 hot
  { duration: 20, weights: [10, 10, 10, 60, 10] },   // 60–80 s  domain 3 hot
  { duration: 20, weights: [10, 10, 10, 10, 60] },   // 80–100 s domain 4 hot
];
```

- The hot domain receives **60 %** of requests; the other four each receive **10 %** (sums to 100 %).
- Weights are **relative** — they do not need to sum to 100; only the ratios matter.
- After all phases elapse, the last phase's weights are kept until the test ends.
- Every metric is tagged with `domain=N` so the CSV output can be sliced per domain.
- The visualizer draws vertical markers at each phase boundary so you can see domain reactions instantly.

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
  --output sweep_e2e_baseline.csv \
  --clean
```

Copy results back and plot:

```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/sweep_e2e_baseline.csv .
python visualize_sweep.py --input sweep_e2e_baseline.csv --output sweep_e2e_baseline_graph.png
```

📝 **Record `KNEE_VU`** — the VU level just before throughput flattens.

---

## Step 13 — Shifting-Focus Distribution Test

This is the main characterisation run. The test rotates the "hot" domain every 20 s (domain 0 → 1 → 2 → 3 → 4), giving each domain 60 % of traffic while it is focused and 10 % when it is not. Running at `KNEE_VU` keeps the system under meaningful load throughout the entire rotation.

### 13a — Transfer the updated test files (local machine)

Make sure the k6 EC2 has the latest `load_test.js` and `visualize_k6_per_app.py` before running.

```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" `
  load_test.js `
  visualize_k6_per_app.py `
  ubuntu@<k6-public-ip>:~/
```

### 13b — Run the shifting-focus test

```bash
# On k6 EC2 (inside tmux, after ulimit)
ulimit -n 65536

k6 run \
  -e TARGET=<manager-private-ip> \
  -e VUS=<KNEE_VU> \
  -e ITERATIONS=<KNEE_VU * 20> \
  -e MAX_DURATION=3m \
  load_test.js \
  --out csv=test_results_e2e.csv
```

> **ITERATIONS guidance:** `KNEE_VU * 20` is a conservative minimum that ensures all 5 phase windows
> (5 × 20 s = 100 s total) complete before the iteration pool exhausts.  
> Example: KNEE_VU = 400 → use `ITERATIONS=8000` and `MAX_DURATION=3m`.

**What to expect during the run:**

| Time (s) | Focused domain | Expected behaviour |
|----------|----------------|--------------------|
| 0–20     | Domain 0       | Domain 0 latency rises; others stay low |
| 20–40    | Domain 1       | Domain 0 recovers; Domain 1 latency rises |
| 40–60    | Domain 2       | Rotation continues |
| 60–80    | Domain 3       | Rotation continues |
| 80–100   | Domain 4       | Domain 4 latency rises last |

k6 prints a live summary every 10 s. Watch `e2e_latency{domain:N}` values shift as each domain takes the load.

### 13c — Copy results back and visualize

```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e.csv .

# Single overlaid latency graph — all 5 domains, focus-switch markers at 20/40/60/80 s
python visualize_k6_per_app.py
# → latency_graph.png

python visualize_k6_per_app.py --bucket 3       # optional — smoother lines
python visualize_k6_per_app.py --no-markers     # optional — no phase lines
```

The script prints a per-domain summary table and saves `latency_graph.png`:

```
=== Per-Domain Latency Summary ===
Domain    Samples    p50 (ms)    p95 (ms)    p99 (ms)    max (ms)
0         ...        ...         ...         ...         ...
1         ...
...
```

**Reading the graph:**
- Five coloured p95-latency lines, one per domain (blue, orange, green, red, purple)
- Vertical dashed lines at t = 20, 40, 60, 80 s mark when the focused domain rotates
- Coloured "Dom N hot" labels above each region identify which domain is carrying 60 % of traffic
- A domain's line should **rise when it becomes hot** and **fall when the focus moves away** — if it doesn't, investigate token-issuer routing or validator throughput

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
