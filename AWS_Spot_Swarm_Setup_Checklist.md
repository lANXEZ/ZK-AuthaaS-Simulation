# AWS EC2 Setup Checklist — ZK-AuthaaS Simulation

Quick-reference checklist for each experiment session. For the full walkthrough with explanations, see **Section 9 of `README.md`**.

This project runs on a **paid-tier AWS account** (professor-provided). Confirm with the account holder that the account is active and check the current billing balance before starting.

---

## Before your first session (one-time)

- [ ] Receive AWS Console access from the account holder (IAM user, not root)
- [ ] Confirm the IAM user has `AmazonEC2FullAccess`
- [ ] Set a **billing alert at $20** in AWS Billing → Budgets
- [ ] Pick one region and record it — use it for every resource (`us-east-1` recommended)
- [ ] **EC2 → Key Pairs → Create** — name it `zk-authaas-key`, download the `.pem`, run `chmod 400 zk-authaas-key.pem`

---

## Each experiment session

### 1. Launch both EC2 instances

**Backend (one per session):**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-backend` · AMI: Ubuntu 22.04 LTS · Type: `c5.4xlarge`
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group `zk-authaas-backend-sg`: SSH (22) from My IP · TCP 8000 from My IP
- [ ] Storage: 30 GB gp3
- [ ] Record **Public IPv4** and **Private IPv4**

> **Running an 80 + 80 experiment with real Groth16?** See the [Multi-Node Architecture (80+80)](#multi-node-architecture-8080-with-real-groth16) section — this requires two EC2 instances. Single-node 500+500 with real snarkjs is not feasible; see the [known limitations](#known-architectural-limitations) section for why.

**k6 loader (one per session):**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-k6` · AMI: Ubuntu 22.04 LTS · Type: `t3.small`
- [ ] Key pair: `zk-authaas-key`
- [ ] **VPC and Subnet: same as backend** (match the AZ exactly)
- [ ] Security group `zk-authaas-k6-sg`: SSH (22) from My IP only
- [ ] Storage: 8 GB gp3
- [ ] Record **Private IPv4**

### 2. Allow k6 → backend traffic

- [ ] Security Groups → `zk-authaas-backend-sg` → Inbound rules → Add rule:
  - Custom TCP · Port 8000 · Source: `<k6-private-ip>/32`

### 3. Set up the backend EC2

```bash
ssh -i zk-authaas-key.pem ubuntu@<backend-public-ip>
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

ssh -i zk-authaas-key.pem ubuntu@<backend-public-ip>
```

**`docker compose` not found?** The `docker.io` apt package does not include Compose V2. Install it as a CLI plugin before proceeding:
```bash
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version   # must print: Docker Compose version v2.27.0
```
This is a one-time step per EC2 instance. The plugin is installed system-wide so it survives re-login.

```bash
git clone https://github.com/lANXEZ/ZK-AuthaaS-Simulation.git zk-authaas && cd zk-authaas
docker swarm init
docker compose build
docker stack deploy -c docker-compose.yml zk
docker service ls   # wait until all replicas show target/target
```

Sanity check:
```bash
curl -s -X POST http://localhost:8000/verify/submit \
  -H "Content-Type: application/json" \
  -d '{"scheme":"stark","proof":"x","public_inputs":["a"]}' | python3 -m json.tool
# Expected: {"status": "accepted", "job_id": "..."}
```

### 4. Set up the k6 EC2

```bash
ssh -i zk-authaas-key.pem ubuntu@<k6-public-ip>
sudo apt update && sudo apt install -y gpg curl
curl -s https://dl.k6.io/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/k6-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update && sudo apt install -y k6
```

Copy the load test and sweep script from your laptop. Run these commands **on your laptop**, not the EC2. Note the quoted paths — the project folder contains spaces.

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"

scp -i "zk-authaas-key.pem" \
  load_test.js \
  sweep_throughput.py \
  weight_sweep.py \
  ubuntu@<k6-public-ip>:~/
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"

scp -i "zk-authaas-key.pem" `
  load_test.js `
  sweep_throughput.py `
  weight_sweep.py `
  ubuntu@<k6-public-ip>:~/
```

> If your `.pem` file is not inside the project folder, replace `"zk-authaas-key.pem"` with its full path (e.g. `"C:/Users/YourName/Downloads/zk-authaas-key.pem"`).

`sweep_throughput.py` uses only Python's standard library — no `pip install` needed. Python 3 is already available on Ubuntu 22.04.

### 5. Scale verifiers and sync the selector

The stack deploys with 10+10 verifiers by default. Before running a real experiment, scale up and tell the selector the new count.

**Scale the worker pools:**
```bash
docker service scale zk_snark-verifier=80 zk_stark-verifier=80
```

**Update the selector to match** (this restarts only the selector container):
```bash
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80" \
  zk_verifier-selector
```

> Replace both `50` values with whatever replica count you scaled to. The `--args` flag replaces the entire command, so all arguments must be present — only change the numbers at the end.

**Verify the selector started with the right count:**
```bash
docker service logs zk_verifier-selector --tail 1
# Must print: "Selector started. SNARK=50 nodes, STARK=50 nodes. Waiting for proofs..."
```

**Why this matters:** the selector keeps an in-memory scoreboard with one slot per worker. If you scale to 50 workers but the selector still thinks there are 10, workers 10–49 will never receive any jobs. The k6 results will look wrong and there will be no error message to tell you why.

**Sanity check before every test run:**
```bash
docker service ls --format "table {{.Name}}\t{{.Replicas}}"
docker service logs zk_verifier-selector --tail 1
# The replica counts and the selector log must agree.
```

---

### 6. Quick sanity check (before starting the experiment)

On the k6 EC2 — do a small smoke run to confirm the full path (k6 → backend → verifiers → response) is working before committing to a long sweep:
```bash
k6 run \
  -e TARGET=<backend-private-ip> \
  -e VUS=10 \
  -e ITERATIONS=50 \
  -e STARK_RATIO=0.0 \
  load_test.js
```

Expected: k6 exits cleanly, `failed_verifications=0`, `submit_failures=0`. If either counter is non-zero, fix the issue before proceeding to Step 7.

### 7. Run the experiment sequence

Run these four steps in order. Each step feeds a value into the next.

---

**Step A — Find True Capacity (VU Sweep at weight=0)**  
*Runs on: k6 EC2*

First, set the selector to weight=0 so the knee is independent of routing. Call the API from the k6 EC2 — no Docker access needed:
```bash
# On k6 EC2:
curl -X POST "http://<backend-private-ip>:8000/admin/set-weight?snark=0&stark=0"
curl -s "http://<backend-private-ip>:8000/admin/get-weight"
# Expected: {"snark_cost_weight": 0.0, "stark_cost_weight": 0.0}
```

Run the sweep on the k6 EC2:
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 25,50,100,150,200,300,400 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_baseline.csv \
  --clean
```

**80+80 scale (two-node, real Groth16):** based on observed ~204ms avg verification time, KNEE_VU is expected around **60–100**.
```bash
python3 sweep_throughput.py \
  --target 172.31.79.96 \
  --vus 50,100,150,200,300,400,500,600,800,1000,1200,1500 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_baseline.csv \
  --clean
```
Expected KNEE_VU: **60–100**. Beyond that, all 80 workers are saturated and throughput flattens.

**500+500 scale:** use a wider VU range to find the higher knee.
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,200,300,400,500,600,800,1000 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_baseline.csv \
  --clean
```
Expected KNEE_VU: somewhere in the **400–800** range depending on proof complexity.

Copy results back and plot:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_baseline.csv .
python visualize_sweep.py --input sweep_baseline.csv --output sweep_baseline_graph.png
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_baseline.csv .
python visualize_sweep.py --input sweep_baseline.csv --output sweep_baseline_graph.png
```

📝 **Record `KNEE_VU`** — the VU level just before throughput flattens.

---

**Step B — Find Optimal Cost-Weight (Weight Sweep)**  
*Runs on: k6 EC2*

`weight_sweep.py` is already on the k6 EC2 (copied in Step 4). It updates the selector weight via `POST /admin/set-weight` — no Docker access or backend SSH needed.

Run the sweep using `KNEE_VU` from Step A:
```bash
# On k6 EC2:
python3 weight_sweep.py \
  --target <backend-private-ip> \
  --weights 0,1,2,3,5,7,10,15,20,30,50 \
  --vus <KNEE_VU> \
  --iterations 2000
```

**80+80 scale:** with ~204ms avg verification time, 8000 iterations at KNEE_VU gives ~10–15s per weight point — enough for stable statistics.
```bash
python3 weight_sweep.py \
  --target <backend-private-ip> \
  --weights 0,1,2,3,5,7,10,15,20,30,50 \
  --vus <KNEE_VU> \
  --iterations 1000
```

**500+500 scale:** `KNEE_VU` will be larger, so increase `--iterations` to keep each run long enough to be statistically meaningful.
```bash
python3 weight_sweep.py \
  --target <backend-private-ip> \
  --weights 0,1,2,3,5,7,10,15,20,30,50 \
  --vus <KNEE_VU> \
  --iterations 5000
```
Everything else (weights list, `--vus <KNEE_VU>`) stays the same.

Copy results back and plot:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/weight_sweep_results.csv .
python visualize_weight_sweep.py
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/weight_sweep_results.csv .
python visualize_weight_sweep.py
```

📝 **Record `BEST_WEIGHT`** — the weight where the composite score (throughput / cost) peaks.

Set `BEST_WEIGHT` on the selector from the k6 EC2 (takes effect immediately):
```bash
# On k6 EC2:
curl -X POST "http://<backend-private-ip>:8000/admin/set-weight?snark=<BEST_WEIGHT>&stark=1.0"
curl -s "http://<backend-private-ip>:8000/admin/get-weight"
# Expected: {"snark_cost_weight": <BEST_WEIGHT>, "stark_cost_weight": 1.0}
```

> **Note:** the API change is session-level — it resets to the CLI default if the selector container restarts. To make it permanent, update the CLI arg on the backend EC2: `docker service update --args "... --snark-cost-weight <BEST_WEIGHT> ..." zk_verifier-selector`.

---

**Step C — Compare Routing Algorithms**  
*Runs on: k6 EC2 (selector switches on backend EC2)*

**Run 1: weighted at BEST_WEIGHT** (selector already set from Step B):
```bash
# On k6 EC2:
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 25,50,100,150,200,300,400 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_weighted.csv \
  --clean
```

**80+80 scale:** same range as Step A.
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,150,200,300,400,500,600,800,1000,1200,1500 \
  --iterations-per-vu 10 \
  --cooldown 5 \
  --stark-ratio 0.0 \
  --output sweep_weighted.csv \
  --clean
```

**500+500 scale:** use the same wider VU range as Step A.
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,200,300,400,500,600,800,1000,1200,1500 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_weighted.csv \
  --clean
```

Switch to round-robin on the backend EC2:
```bash
# On backend EC2:
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80 --routing roundrobin --snark-cost-weight <BEST_WEIGHT> --stark-cost-weight 1.0" \
  zk_verifier-selector

docker service logs zk_verifier-selector --tail 1
# Must show: Routing=roundrobin
```

**Run 2: round-robin:**
```bash
# On k6 EC2:
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 25,50,100,150,200,300,400 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_roundrobin.csv \
  --clean
```

**80+80 scale:** same range as Run 1.
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,150,200,300,400,500,600,800,1000,1200,1500 \
  --iterations-per-vu 10 \
  --cooldown 5 \
  --stark-ratio 0.0 \
  --output sweep_roundrobin.csv \
  --clean
```

**500+500 scale:** same wider VU range as Run 1.
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,200,300,400,500,600,800,1000,1200,1500 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_roundrobin.csv \
  --clean
```

Copy both CSVs back and generate the comparison graph:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_weighted.csv .
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_roundrobin.csv .
python visualize_comparison.py sweep_weighted.csv sweep_roundrobin.csv \
  --label-a "Weighted (weight=<BEST_WEIGHT>)" --label-b "Round-Robin"
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_weighted.csv .
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_roundrobin.csv .
python visualize_comparison.py sweep_weighted.csv sweep_roundrobin.csv `
  --label-a "Weighted (weight=<BEST_WEIGHT>)" --label-b "Round-Robin"
```

Restore weighted mode on the backend EC2:
```bash
# On backend EC2 (80+80):
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80 --routing weighted --snark-cost-weight <BEST_WEIGHT> --stark-cost-weight 1.0" \
  zk_verifier-selector
```

---

**Step D — Detailed Time-Series Run**  
*Runs on: k6 EC2*

Run a single long test at `KNEE_VU` to produce the time-series CSV:
```bash
# On k6 EC2:
k6 run \
  -e TARGET=<backend-private-ip> \
  -e VUS=<KNEE_VU> \
  -e ITERATIONS=<KNEE_VU * 20> \
  -e STARK_RATIO=0.0 \
  load_test.js \
  --out csv=test_results.csv
```

**80+80 scale:** `KNEE_VU` is expected around 60–100, so `KNEE_VU * 20` gives 1,200–2,000 iterations. Based on observed results (800 iterations completed in 7.8s at 80 VUs), expect the full time-series run to take ~2–3 minutes.
```bash
k6 run \
  -e TARGET=<backend-private-ip> \
  -e VUS=<KNEE_VU> \
  -e ITERATIONS=<KNEE_VU * 20> \
  -e STARK_RATIO=0.0 \
  load_test.js \
  --out csv=test_results_80.csv
```

**500+500 scale:** `KNEE_VU` will be in the 400–800 range, so `KNEE_VU * 20` gives 8,000–16,000 iterations — that's sufficient for a stable time series. No formula change needed; just substitute the larger `KNEE_VU` value.

Copy back and visualize:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/test_results.csv .
python visualize_k6.py
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/test_results.csv .
python visualize_k6.py
```

### 8. TEAR DOWN — do this every session, no exceptions

On the backend EC2:
```bash
docker stack rm zk
exit
```

AWS Console:
- [ ] EC2 → Instances → select `zk-authaas-backend` **and** `zk-authaas-k6`
- [ ] Instance State → **Terminate**
- [ ] Wait for both to show `terminated`
- [ ] Confirm EC2 dashboard shows **0 running instances**

A forgotten `c5.4xlarge` left running overnight costs ~$16. Left for a week: ~$115.

---

---

## 500 + 500 Large-Scale Experiment

Use this checklist **instead of** Step 1 when running the 500+500 VU sweep. All other steps (2–8) are identical.

### Instance selection

- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-backend` · AMI: Ubuntu 22.04 LTS · Type: **`c5.24xlarge`** (96 vCPUs / 192 GB RAM)
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group `zk-authaas-backend-sg`: SSH (22) from My IP · TCP 8000 from My IP
- [ ] Storage: **50 GB gp3** (extra headroom for 1000 container images)
- [ ] Record **Public IPv4** and **Private IPv4**

> ⚠️ `c5.24xlarge` costs ~$4.08/hr. A 2-hour session costs ~$8.20. An overnight accident costs ~$98. **Set a $25 billing alert before launching.**

### Verify available vCPUs

AWS accounts have a default **vCPU limit of 32** for compute-optimised instances. `c5.24xlarge` needs 96.

- [ ] EC2 → Limits → search **"Running On-Demand C instances"** → check the limit
- [ ] If limit < 96: **Service Quotas → EC2 → Running On-Demand C instances → Request increase to 96**
  - Approval usually takes a few minutes for educational accounts; up to 24 hrs otherwise.

### Verify overlay network subnet

The default `docker-compose.yml` configures the overlay network with a `/20` subnet (4094 usable IPs). If you are working from an older copy that uses `/24` (254 IPs), scaling will silently stall at ~246 containers with `DynamicIPsAvailable: 0`.

Check which subnet your running network has:
```bash
docker network inspect zk_zk-net | python3 -m json.tool | grep -A5 "Subnet"
# Must show: "Subnet": "10.1.0.0/20"
# If it shows /24 — tear down the stack, update docker-compose.yml, and redeploy before scaling.
```

### Raise kernel inotify limits (required before scaling)

Ubuntu's default `max_user_instances=128` is exhausted at ~230 containers. Docker stalls silently — no error, just stops scheduling new tasks. Run this **before** scaling:

```bash
# On backend EC2 — apply immediately:
sudo sysctl fs.inotify.max_user_instances=8192
sudo sysctl fs.inotify.max_user_watches=524288

# Make permanent across reboots:
echo "fs.inotify.max_user_instances=8192" | sudo tee -a /etc/sysctl.conf
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
```

### After deploying the stack — scale to 500 + 500

```bash
# On backend EC2:
docker service scale zk_snark-verifier=500 zk_stark-verifier=500

docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 500 --stark-count 500 --routing weighted --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector

docker service ls   # wait until all 500/500 replicas are ready (may take 2–4 min)
docker service logs zk_verifier-selector --tail 1
# Must print: "Selector started. SNARK=500 nodes, STARK=500 nodes. Waiting for proofs..."
```

### Set weight=0 before the VU sweep

```bash
# From k6 EC2:
curl -X POST "http://<backend-private-ip>:8000/admin/set-weight?snark=0&stark=0"
curl -s "http://<backend-private-ip>:8000/admin/get-weight"
# Expected: {"snark_cost_weight": 0.0, "stark_cost_weight": 0.0}
```

### Run the VU sweep

```bash
# On k6 EC2:
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,200,300,400,500,600,800,1000 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_baseline_500.csv \
  --clean
```

Copy results back and plot:

**Git Bash:**
```bash
cd "/e/Work/VSCode Repo/ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_baseline_500.csv .
python visualize_sweep.py --input sweep_baseline_500.csv --output sweep_baseline_500_graph.png
```

**PowerShell:**
```powershell
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-key.pem" ubuntu@<k6-public-ip>:~/sweep_baseline_500.csv .
python visualize_sweep.py --input sweep_baseline_500.csv --output sweep_baseline_500_graph.png
```

📝 **Record `KNEE_VU`** and continue from Step B (weight sweep) using `KNEE_VU` and `--target <backend-private-ip>`.

---

## Scale reference

Always run both commands together — scale the workers, then sync the selector.

**10 + 10 (default, already set at deploy — no action needed)**

**50 + 50:**
```bash
docker service scale zk_snark-verifier=50 zk_stark-verifier=50
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 50 --stark-count 50 --routing weighted --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector
```

**50 + 50, round-robin (for comparison experiment):**
```bash
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 50 --stark-count 50 --routing roundrobin --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector
```

**50 + 50, custom weight (after running weight sweep):**
```bash
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 50 --stark-count 50 --routing weighted --snark-cost-weight <best-weight> --stark-cost-weight 1.0" \
  zk_verifier-selector
```

**80 + 80 (two-node architecture — see multi-node section):**
```bash
# Run on manager after both nodes are labelled and stack is deployed
docker service scale zk_snark-verifier=80 zk_stark-verifier=80
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80 --routing weighted --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector
```

**500 + 500 (NOT viable with real Groth16 — see known limitations):**
```bash
# Only works with mocked verification. See known limitations section before attempting.
docker service scale zk_snark-verifier=500 zk_stark-verifier=500
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 500 --stark-count 500 --routing weighted --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector
```

## Cost reference

**Standard experiment (50 + 50 workers):**

| Instance | Type | On-demand | 2 hr session |
|---|---|---|---|
| Backend | c5.4xlarge | ~$0.68/hr | ~$1.36 |
| k6 | t3.small | ~$0.02/hr | ~$0.04 |
| **Total** | | | **~$1.40** |

**Large-scale experiment (80 + 80 workers, two-node real Groth16):**

| Instance | Role | Type | On-demand | 2 hr session |
|---|---|---|---|---|
| Manager | Redis, selector, STARK pool | c5.24xlarge | ~$4.08/hr | ~$8.16 |
| Worker | SNARK pool only | c5.24xlarge | ~$4.08/hr | ~$8.16 |
| k6 | Load generator | t3.small | ~$0.02/hr | ~$0.04 |
| **Total** | | | | **~$16.36** |

> ⚠️ Two forgotten `c5.24xlarge` instances left overnight costs ~$196. **Terminate both immediately after each session.**

---

## Multi-Node Architecture (80+80 with Real Groth16)

This is the **recommended architecture** for running real `snarkjs.groth16.verify` at scale. It splits the SNARK and STARK pools across two EC2 nodes to stay within Docker overlay network and CPU threading limits.

**Topology:**
- **Manager node** (`ip-172-31-79-96`): Redis ×3, request-handler, verifier-selector, STARK pool (80 workers)
- **Worker node** (`ip-172-31-72-246`): SNARK pool only (80 workers × 1.0 vCPU each)

### Step 1 — Launch two EC2 instances

- [ ] Launch **manager** EC2: `c5.24xlarge`, Ubuntu 22.04, `zk-authaas-key`, same security group, 50 GB gp3
- [ ] Launch **worker** EC2: same specs as manager, same VPC and subnet (same AZ)
- [ ] In the shared security group, add an inbound rule: **All traffic · Source: `<security-group-id>`** (self-referencing — allows all intra-cluster traffic)
- [ ] Verify connectivity: `ping -c3 <worker-private-ip>` from manager should show 0% packet loss

### Step 2 — Install Docker on worker and join swarm

On the **worker EC2**:
```bash
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
newgrp docker
```

On the **manager EC2**:
```bash
docker swarm init --advertise-addr <manager-private-ip>
docker swarm join-token worker   # copy the printed join command
```

On the **worker EC2** (paste the join command from above):
```bash
docker swarm join --token SWMTKN-1-... <manager-private-ip>:2377
# Expected: "This node joined a swarm as a worker."
```

### Step 3 — Label nodes and build images

On the **manager EC2**:
```bash
# Label each node for its pool
docker node update --label-add pool=stark $(docker node ls -q --filter role=manager)
docker node update --label-add pool=snark $(docker node ls -q --filter role=worker)

# Confirm both nodes are Ready
docker node ls
```

On the **worker EC2** — build the SNARK image:
```bash
git clone https://github.com/lANXEZ/ZK-AuthaaS-Simulation zk-authaas
cd zk-authaas
docker build -f Dockerfile.snark -t zk-authaas/snark-verifier:latest .
```

On the **manager EC2** — pull latest compose (includes placement constraints) and deploy:
```bash
cd ~/zk-authaas
git pull
docker stack deploy -c docker-compose.yml zk
watch -n3 'docker service ls --format "table {{.Name}}\t{{.Replicas}}"'
# Wait until all services show target replicas
```

### Step 4 — Scale to 80+80

```bash
# On manager — scale services (SNARK goes to worker, STARK stays on manager)
for TARGET in 20 50 80; do
  echo "=== Scaling to ${TARGET}+${TARGET} ==="
  docker service scale zk_snark-verifier=$TARGET zk_stark-verifier=$TARGET
  sleep 20
  docker service logs zk_verifier-selector --tail 2
done

# Update selector count
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80 --routing weighted --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector

docker service logs zk_verifier-selector --tail 1
# Must print: "Selector started. SNARK=80 nodes, STARK=80 nodes. Waiting for proofs..."
```

### Step 5 — Confirm placement and smoke test

```bash
# Verify pool separation
docker service ps zk_snark-verifier --format "table {{.Name}}\t{{.Node}}" | head -3
# All SNARK tasks must show the worker node

docker service ps zk_stark-verifier --format "table {{.Name}}\t{{.Node}}" | head -3
# All STARK tasks must show the manager node
```

Smoke test — one job should complete in 1–5 seconds:
```bash
JOB=$(curl -s -X POST http://localhost:8000/verify/submit \
  -H "Content-Type: application/json" \
  -d '{"scheme":"snark","proof":{"pi_a":["16893334615242764580836222078829142520432756203770466604081032720388657032757","5095606969395716303621702958471922376961618029789842152295821108717087682311","1"],"pi_b":[["13772398192624595577472662855811728500397412494267729711099372526485968374649","15249941699599606024139723272508104548269790148997217612719623411267570558493"],["19735295879188043871505513529932228526631701925990878770250928234435443795397","11046809327765151786114304454515091703284305019483922364766276175300463695885"],["1","0"]],"pi_c":["18536201733965390491456176988021021022761142364866628667452517360063595662975","15291715715367874403418883228408929985980666544091293542955491873294267230352","1"],"protocol":"groth16","curve":"bn128"},"public_inputs":["1120771572304984668855649788542860110303223894298952018121329196339919157573","20197087425205130352574209034729275460185533126585197591053247747830393653846","111222333","444555666","1764263975784332459809300572476310454427845461305579380554772042455913567929","10988278040513707334400680073433620711051179041727267619401283491695328957763"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

watch -n1 "curl -s http://localhost:8000/verify/status/$JOB"
# Expected: {"status":"completed"} within 10 seconds
```

### Recommended VU ranges for 80+80

| Test | VU sweep | Expected KNEE_VU |
|---|---|---|
| Step A (baseline) | `10,20,40,60,80,100,120,160` | 60–100 |
| Step B (weight sweep) | `--vus <KNEE_VU> --iterations 1000` | — |
| Step C (routing comparison) | same range as Step A | — |
| Step D (time series) | `--vus <KNEE_VU> --iterations <KNEE_VU * 20>` | — |

### Tear down (two nodes)

```bash
# On manager:
docker stack rm zk
docker swarm leave --force

# On worker:
docker swarm leave --force
```

Then terminate **both** EC2 instances in the AWS Console.

---

## Known Architectural Limitations

### Why 500+500 with real Groth16 is not feasible on a single node

`snarkjs.groth16.verify` uses `ffjavascript` for BN128 elliptic curve operations. Internally, ffjavascript determines its worker thread count using `os.cpus().length`. Inside a Docker container, `os.cpus()` reports **all host CPUs** (e.g. 96 on a c5.24xlarge) regardless of the container's CPU limit — Docker uses CFS quota-based throttling, not CPU masking.

Result: every SNARK verifier container spawns **95 worker threads** and uses `Atomics.wait()` to synchronise them. With only `0.15` vCPU allocated per container (the limit needed to fit 500 workers on 96 vCPUs), the CFS scheduler throttles the container to 15ms of CPU per 100ms period — not enough for 95+ threads to make forward progress. The `Atomics.wait()` call blocks indefinitely, the container shows **0% CPU**, and the job stays at `"processing"` forever. This is a deadlock, not starvation.

**The fix applied in this project** — `SNARKVerifierWorker.js` monkey-patches `os.cpus()` at startup to return a single CPU entry before snarkjs/ffjavascript loads. This causes ffjavascript to spawn only 1 worker thread, making `Atomics` synchronisation viable even at constrained CPU budgets:

```javascript
// At top of SNARKVerifierWorker.js — must execute before require("snarkjs")
const os = require("os");
const _realCpus = os.cpus.bind(os);
os.cpus = () => [_realCpus()[0]];
```

**Why 500 workers still doesn't work even with the patch** — with 1 worker thread per container, each verify needs ~200ms on 1.0 vCPU. Running 500 workers at 1.0 vCPU requires 500 vCPUs — more than any single EC2 instance provides. The multi-node architecture (80 workers × 1.0 vCPU = 80 vCPUs on one node) is the practical ceiling for real Groth16 on a single c5.24xlarge.

**Academic justification for 80+80** — in a production SaaS deployment, Groth16 verifiers would be compiled Rust binaries (e.g. `arkworks`, `bellman`) completing in <10ms per proof. The bottleneck demonstrated here is specific to Node.js + WASM. The 80+80 configuration validates the routing, queueing, and cost-weighted scheduling architecture — which is the system under study — at meaningful concurrency with real cryptographic work.

**To scale beyond 80+80 with real Groth16**: add more worker EC2 nodes to the Swarm. Each additional c5.24xlarge node can host another 80 SNARK workers (80 vCPUs at 1.0 each), giving linear horizontal scaling. 6 worker nodes = ~500 SNARK workers. This is the intended production topology for a SaaS ZK verification service.

---

## Common issues

| Symptom | Fix |
|---|---|
| `docker: unknown command: docker compose` | Compose V2 not installed. Run the plugin install block in Step 3. |
| `Cannot connect to Docker daemon` | Forgot to re-login after `usermod -aG docker`. SSH out and back in. |
| SNARK services stuck at `0/10` | Memory overcommit — check `docker service ps zk_snark-verifier --no-trunc`. Reduce replicas or raise the `memory` limit in `docker-compose.yml`. |
| `Error: Invalid proof` in SNARK worker logs | `verification_key.json` mismatch — rebuild the image: `docker compose build --no-cache` |
| k6 `connection refused` on port 8000 | Security group missing the k6 private IP rule (Step 2), or using public IP instead of private IP. |
| Swarm services stuck `pending` | CPU/memory overcommit. Check `docker service ps` and reduce replicas or resource limits. |
| Weight sweep: `docker service update` fails | Stack name mismatch — default service name is `zk_verifier-selector`. Pass `--service <stack>_verifier-selector` if you used a different stack name. |
| Weight sweep: cost stays flat at ≈ 1.5 across all weights | `/stats/cost` endpoint missing — rebuild and redeploy the request-handler image. |
| Weight sweep: throughput barely changes across weights | VU count too low to saturate any node. Increase `--vus` until `monitor_queues.ps1` shows queue depth > 0. |
| `docker service scale` stalls at ~230 containers, no errors | Kernel inotify limit exhausted (`max_user_instances=128`). Run `sudo sysctl fs.inotify.max_user_instances=8192 && sudo sysctl fs.inotify.max_user_watches=524288`. Scaling resumes within 30 seconds. |
| `docker service scale` stalls at ~246 containers, no errors | Overlay network subnet exhausted (`/24` = 254 IPs). Check with `docker network inspect zk_zk-net \| python3 -m json.tool \| grep DynamicIPsAvailable`. Fix: `docker stack rm zk`, set subnet to `10.1.0.0/20` in `docker-compose.yml`, redeploy. |
| Services stuck at `0/N` replicas, task state "New", NODE field empty, no errors | Custom subnet overlaps with Swarm's ingress network (`10.0.0.0/24`). Fix: use a non-overlapping subnet like `10.1.0.0/20` in `docker-compose.yml`. Verify with `docker network inspect ingress \| grep Subnet` — your overlay subnet must NOT overlap. |
| SNARK jobs stuck at `"processing"`, verifier shows **0% CPU**, never completes | snarkjs/ffjavascript `Atomics.wait()` deadlock caused by CPU limit too low for its worker threads. Fix: ensure `SNARKVerifierWorker.js` has the `os.cpus()` monkey-patch at the top (limits ffjavascript to 1 worker thread) AND set `cpus: '1.0'` in `docker-compose.yml`. See [known limitations](#known-architectural-limitations). |
| `verifier-selector` flapping with `Error 113: No route to host` after scaling to 500+500 | Docker overlay VXLAN forwarding table overwhelmed by 1000+ containers on a single node — service VIPs stop resolving. Fix: scale back, or use the two-node architecture. |
| Swarm network `zk_zk-net` won't remove after `docker stack rm`, hangs indefinitely | Phantom task reference in Swarm raft state. Fix: `docker swarm leave --force && docker swarm init`. |
