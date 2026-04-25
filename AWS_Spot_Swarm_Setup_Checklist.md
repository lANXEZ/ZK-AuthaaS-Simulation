# AWS EC2 Setup Checklist — ZK-AuthaaS Simulation

Quick-reference checklist for each experiment session. For the full walkthrough with explanations, see **Section 9 of `README.md`**.

This project runs on a **paid-tier AWS account** (professor-provided). Confirm with the account holder that the account is active and check the current billing balance before starting.

---

## Topology you will end up with

This checklist sets up the **two-node Swarm + dedicated k6 loader** topology — three EC2 instances total. This is the canonical configuration for running real `snarkjs.groth16.verify` at 80+80 scale and is what every step below assumes.

```
                      ┌──────────────────────┐
   your laptop ──┐    │  Manager EC2         │
                 │    │  (c5.24xlarge)       │
                 │    │  • Redis × 3         │
                 │    │  • request-handler×8 │
                 │    │  • verifier-selector │
                 ├───►│  • STARK pool × 80   │
                 │    └─────────┬────────────┘
                 │              │ Swarm overlay (VXLAN)
                 │              ▼
                 │    ┌──────────────────────┐
                 │    │  Worker EC2          │
                 │    │  (c5.24xlarge)       │
                 │    │  • SNARK pool × 80   │
                 │    └──────────────────────┘
                 │
                 │    ┌──────────────────────┐
                 └───►│  k6 EC2 (c5.2xlarge) │
                      │  • Load generator    │
                      └──────────┬───────────┘
                                 │ HTTP :8000
                                 ▼
                          (manager EC2)
```

| Role | Instance type | Purpose |
|---|---|---|
| Manager | `c5.24xlarge` (96 vCPU / 192 GB) | Redis ×3, request-handler **×8**, verifier-selector, STARK pool (80 workers × 0.15 vCPU) |
| Worker | `c5.24xlarge` (96 vCPU / 192 GB) | SNARK pool only (80 workers × 1.0 vCPU) |
| k6 | `c5.2xlarge` (8 vCPU / 16 GiB) | Load generator — runs k6 + Python sweep scripts |

> **Why two backend nodes?** A single c5.24xlarge cannot host 80 SNARK workers at 1.0 vCPU plus the rest of the stack — it runs out of vCPU. Splitting SNARK onto its own node also avoids overlay-network VIP exhaustion that occurs with 1000+ containers on one node. See [Known Architectural Limitations](#known-architectural-limitations) for the deeper reasoning.

---

## Before your first session (one-time)

- [ ] Receive AWS Console access from the account holder (IAM user, not root)
- [ ] Confirm the IAM user has `AmazonEC2FullAccess`
- [ ] Set a **billing alert at $25** in AWS Billing → Budgets (two `c5.24xlarge` instances cost ~$8/hr — easy to overrun)
- [ ] Pick one region and record it — use it for every resource (`us-east-1` recommended)
- [ ] **EC2 → Key Pairs → Create** — name it `zk-authaas-key`, download the `.pem`, run `chmod 400 zk-authaas-key.pem`
- [ ] Check the vCPU quota for your account — you need at least 192 vCPUs of "Running On-Demand C instances" (two `c5.24xlarge` × 96 vCPUs each). Default is 32. Request an increase via **Service Quotas → EC2 → Running On-Demand C instances → Request quota increase to 256** if needed. Educational accounts are usually approved within minutes.

---

## Each experiment session

### Step 1 — Create the shared security group

Both backend nodes (and optionally the k6 loader) **must share a single security group**. Docker Swarm overlay networking (VXLAN) and management traffic use multiple TCP and UDP ports — the easiest and most reliable approach is a self-referencing "All traffic" rule that lets any instance in the group talk freely to any other.

> ⚠️ **Do not create separate security groups for each node.** If manager and worker are in different security groups, intra-cluster traffic is blocked and `docker swarm join` will appear to succeed but container scheduling will fail silently.

**In the AWS Console — EC2 → Security Groups → Create security group:**

| Field | Value |
|---|---|
| Name | `zk-authaas-cluster-sg` |
| Description | Shared SG for all ZK-AuthaaS Swarm nodes and k6 loader |
| VPC | *(your default VPC — same one you'll launch all three EC2s into)* |

**Inbound rules — add all three:**

| # | Type | Protocol | Port | Source | What to enter in the Source field | Purpose |
|---|---|---|---|---|---|---|
| 1 | SSH | TCP | 22 | My IP | Click **"My IP"** in the dropdown — AWS fills in your current public IP automatically (e.g. `203.0.113.45/32`) | SSH from your laptop to any of the three EC2s |
| 2 | Custom TCP | TCP | 8000 | My IP | Same as rule 1 — click **"My IP"** | Direct `curl` to the FastAPI backend from your laptop (used in `/admin/set-weight` and sanity checks) |
| 3 | All traffic | All | All | Custom | Start typing `sg-` in the Source box, then select **this same security group** from the autocomplete dropdown (e.g. `sg-0abc123def456 / zk-authaas-cluster-sg`). This is the self-referencing rule. | All inter-node Swarm traffic, plus k6 → backend on :8000 |

> **Where to find each IP:**
> - **My IP (rules 1 & 2):** AWS fills this automatically when you select "My IP" — no need to look it up. If your laptop IP changes between sessions, edit rules 1 & 2 and click "My IP" again.
> - **Security group ID (rule 3):** EC2 Console → Security Groups → click `zk-authaas-cluster-sg` → copy the **Security group ID** at the top (`sg-xxxxxxxxxxxxxxxxx`). Then paste it into the Source field of rule 3, or just type `sg-` and pick it from the dropdown.

> **Rule 3 (self-referencing) is the critical one.** To add it:
> 1. After creating the SG, note its ID (`sg-xxxxxxxxxxxxxxxxx`)
> 2. Edit inbound rules → Add rule
> 3. Type: **All traffic** · Source: start typing `sg-` and select **this same security group** from the dropdown
> 4. Save rules

**You do not need to add these ports manually.** Rule 3 already covers everything below — this table is for reference only:

| Port | Protocol | Used by |
|---|---|---|
| 2377 | TCP | Swarm cluster management (`docker swarm join`, leader election) |
| 7946 | TCP + UDP | Container network discovery (gossip between nodes) |
| 4789 | UDP | VXLAN overlay — the tunnel that carries all container-to-container traffic across nodes |
| 6379–6381 | TCP | Redis brokers (proof-queue, snark-queue, stark-queue) — accessed by containers on both nodes |
| 8000 | TCP | FastAPI request-handler — k6 EC2 hits this on the manager's private IP |

### Step 2 — Launch the three EC2 instances

All three go into the **same VPC, same subnet (same Availability Zone), same security group `zk-authaas-cluster-sg`**. Same-AZ matters because cross-AZ overlay traffic adds latency and AWS data transfer fees.

**Manager EC2:**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-manager` · AMI: Ubuntu 22.04 LTS · Type: **`c5.24xlarge`**
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`** (created in Step 1)
- [ ] Storage: 50 GB gp3
- [ ] Record **Public IPv4** (for SSH) and **Private IPv4** (for inter-node and k6 use)

**Worker EC2:**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-worker` · AMI: Ubuntu 22.04 LTS · Type: **`c5.24xlarge`**
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`** (same as manager)
- [ ] **Same VPC and Subnet as manager** (verify the AZ matches)
- [ ] Storage: 50 GB gp3
- [ ] Record **Public IPv4** and **Private IPv4**

**k6 loader EC2:**
- [ ] EC2 → Launch Instance
- [ ] Name: `zk-authaas-k6` · AMI: Ubuntu 22.04 LTS · Type: **`c5.2xlarge`** (8 vCPU / 16 GiB)
- [ ] Key pair: `zk-authaas-key`
- [ ] Security group: **`zk-authaas-cluster-sg`** (same group — the self-referencing rule lets it reach the backend on :8000 automatically)
- [ ] Same VPC and Subnet as the backends
- [ ] Storage: 20 GiB gp3
- [ ] Record **Public IPv4** (for SSH) and **Private IPv4**

**Connectivity sanity check** (run this before going further — catches SG misconfiguration early):
```bash
# SSH into manager
ssh -i zk-authaas-key.pem ubuntu@<manager-public-ip>

# From manager, ping the worker by private IP
ping -c3 <worker-private-ip>
# Expected: 0% packet loss

# Also ping the k6 instance
ping -c3 <k6-private-ip>
# Expected: 0% packet loss
```

If either ping fails, re-check that all three EC2s are in `zk-authaas-cluster-sg` and that rule 3 (self-referencing All traffic) was saved correctly.

### Step 3 — Raise kernel inotify limits on both backend nodes

Ubuntu's default `max_user_instances=128` is exhausted at ~230 containers. With 80+ tasks per node this is borderline; safer to raise it preemptively. Run on **both manager and worker**:

```bash
sudo sysctl fs.inotify.max_user_instances=8192
sudo sysctl fs.inotify.max_user_watches=524288

# Make permanent across reboots:
echo "fs.inotify.max_user_instances=8192" | sudo tee -a /etc/sysctl.conf
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
```

### Step 4 — Install Docker + clone repo on both backend nodes

Run this **identically on the manager and the worker** (two separate SSH sessions):

```bash
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-key.pem ubuntu@<that-node-public-ip>
```

**`docker compose` not found?** The `docker.io` apt package does not include Compose V2. Install it as a CLI plugin (one-time, per node):
```bash
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version   # must print: Docker Compose version v2.27.0
```

Clone the repo on both nodes:
```bash
git clone https://github.com/lANXEZ/ZK-AuthaaS-Simulation.git zk-authaas
cd zk-authaas
```

### Step 5 — Initialise the Swarm and join the worker

On the **manager**:
```bash
docker swarm init --advertise-addr <manager-private-ip>
docker swarm join-token worker
# Copy the entire printed command — it looks like:
# docker swarm join --token SWMTKN-1-... <manager-private-ip>:2377
```

On the **worker** (paste the command you just copied):
```bash
docker swarm join --token SWMTKN-1-... <manager-private-ip>:2377
# Expected: "This node joined a swarm as a worker."
```

Verify on the **manager**:
```bash
docker node ls
# Both nodes should show Status=Ready, the manager has MANAGER STATUS=Leader
```

### Step 6 — Label the nodes for pool placement

The compose file uses `node.labels.pool == snark` and `node.labels.pool == stark` to pin each verifier pool to its dedicated node. Apply the labels on the **manager**:

```bash
docker node update --label-add pool=stark $(docker node ls -q --filter role=manager)
docker node update --label-add pool=snark $(docker node ls -q --filter role=worker)

# Confirm the labels stuck:
docker node inspect $(docker node ls -q) --format '{{.Description.Hostname}} → pool={{index .Spec.Labels "pool"}}'
# Expected:
#   ip-172-31-XX-XX → pool=stark
#   ip-172-31-YY-YY → pool=snark
```

### Step 7 — Build images on both nodes

Swarm does **not** ship images between nodes — each node must already have the images it will run. The manager runs the request-handler, selector, and STARK pool images; the worker runs only the SNARK pool image.

On the **manager**:
```bash
cd ~/zk-authaas
docker compose build
# Builds all images: request-handler, verifier-selector, snark-verifier, stark-verifier
```

On the **worker**:
```bash
cd ~/zk-authaas
docker build -f Dockerfile.snark -t zk-authaas/snark-verifier:latest .
# The worker only needs the SNARK image, but building all is fine too:
# docker compose build
```

### Step 8 — Deploy the stack

On the **manager**:
```bash
cd ~/zk-authaas
docker stack deploy -c docker-compose.yml zk

# Watch services come up:
watch -n3 'docker service ls --format "table {{.Name}}\t{{.Mode}}\t{{.Replicas}}"'
# All services must reach target replicas. The default in docker-compose.yml is 80+80.
```

**Verify pool placement** (catches mis-labelled nodes):
```bash
docker service ps zk_snark-verifier --format "table {{.Name}}\t{{.Node}}" | head -3
# All SNARK tasks must be on the WORKER node

docker service ps zk_stark-verifier --format "table {{.Name}}\t{{.Node}}" | head -3
# All STARK tasks must be on the MANAGER node
```

If a service is stuck at `0/80`, check `docker service ps zk_<service> --no-trunc` for the error in the last column.

**Scale the request-handler to 8 replicas** — a single FastAPI process caps at ~100 real verifies/s due to the single-threaded event loop. 8 replicas raises the upstream throughput ceiling to ~800 verifies/s, which is high enough to saturate the cheap worker pool and show the cost-vs-throughput tradeoff at high VUs. Swarm's ingress mesh load-balances port 8000 across all replicas automatically — no changes to k6 or the client needed:
```bash
docker service scale zk_request-handler=8
docker service ls | grep request-handler
# Must show 8/8
```

### Step 9 — Set up the k6 loader EC2

```bash
ssh -i zk-authaas-key.pem ubuntu@<k6-public-ip>
sudo apt update && sudo apt install -y gpg curl
curl -s https://dl.k6.io/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/k6-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update && sudo apt install -y k6
```

Copy the load test and sweep scripts from your **laptop** (not from EC2 — these live in the project folder):

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

`sweep_throughput.py` and `weight_sweep.py` use only Python's standard library — no `pip install` needed. Python 3 is preinstalled on Ubuntu 22.04.

**Raise the file descriptor limit before running any sweep** — each VU holds one open connection, and the OS default of 1024 will cause k6 to freeze or crash above ~800 VUs:
```bash
ulimit -n 65536
```

Run this in the same shell session before every sweep. It resets on logout, so set it again each time you SSH in.

> **Tip — use tmux so SSH disconnects don't kill a running sweep:**
> ```bash
> tmux new -s sweep       # start a named session
> ulimit -n 65536         # set limit inside the session
> python3 sweep_throughput.py ...  # run your sweep
> # If SSH disconnects: re-SSH, then:
> tmux attach -t sweep    # re-attach to the running session
> ```

### Step 10 — Smoke test (full path: k6 → manager → workers → response)

From the **k6 EC2**, do a small run to confirm the entire pipeline is working before committing to a long sweep:

```bash
k6 run \
  -e TARGET=<manager-private-ip> \
  -e VUS=10 \
  -e ITERATIONS=50 \
  -e STARK_RATIO=0.0 \
  load_test.js
```

**Expected:** k6 exits cleanly, `failed_verifications=0`, `submit_failures=0`, response times in the 100–300ms range.

If anything fails, fix it before proceeding to Step 11. Common causes:
- `connection refused` on :8000 → SG misconfigured, or used public IP instead of private IP
- `failed_verifications` > 0 → SNARK images mismatched between nodes (rebuild on the worker)
- All requests stuck "processing" → SNARK worker `Atomics` deadlock (see [Known Limitations](#known-architectural-limitations))

### Step 11 — Run the experiment sequence

Run these four steps in order. Each step feeds a value into the next.

---

**Step A — Find True Capacity (VU sweep at weight=0)**
*Runs on: k6 EC2*

Set the selector to weight=0 so the knee is independent of routing. Call the API from the k6 EC2:
```bash
curl -X POST "http://<manager-private-ip>:8000/admin/set-weight?snark=0&stark=0"
curl -s "http://<manager-private-ip>:8000/admin/get-weight"
# Expected: {"snark_cost_weight": 0.0, "stark_cost_weight": 0.0}
```

Run the sweep (range tuned for 80+80 with real Groth16 — KNEE_VU is expected around **60–100**):
```bash
ulimit -n 65536   # required — prevents k6 from freezing above ~800 VUs
python3 sweep_throughput.py \
  --target <manager-private-ip> \
  --vus 50,100,200,400,600,800,1000,1200,1500,2000,3000,4000,5000 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_baseline.csv \
  --clean
```

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

**Step B — Find Optimal Cost-Weight (Weight sweep)**
*Runs on: k6 EC2*

`weight_sweep.py` is already on the k6 EC2. It updates the selector weight via `POST /admin/set-weight` — no SSH to the manager needed.

```bash
ulimit -n 65536   # raise fd limit if not already set in this shell
python3 weight_sweep.py \
  --target <manager-private-ip> \
  --weights 0,1,2,3,5,7,10,15,20,30,50 \
  --vus <KNEE_VU> \
  --iterations 15000
```

> **Why 15000 iterations?** Each weight point needs at least 30 seconds of steady-state measurement to produce a stable average cost and throughput. At KNEE_VU with ~500 completions/s, 15000 iterations ≈ 30s per weight point. Using fewer (e.g. 1000) captures only the transient phase and produces a spiky, unreliable graph. The full 11-weight sweep takes ~6–7 minutes.
>
> **Delete the output CSV before re-running** — `weight_sweep.py` appends to `~/weight_sweep_results.csv` rather than overwriting it. Running twice without deleting produces multiple data points per weight on the graph:
> ```bash
> rm ~/weight_sweep_results.csv
> ```

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

Set `BEST_WEIGHT` on the selector (effective immediately, no restart needed):
```bash
curl -X POST "http://<manager-private-ip>:8000/admin/set-weight?snark=<BEST_WEIGHT>&stark=1.0"
curl -s "http://<manager-private-ip>:8000/admin/get-weight"
```

> The API change is session-level — it resets to the CLI default if the selector container restarts. To make it permanent, update the CLI arg on the manager: `docker service update --args "... --snark-cost-weight <BEST_WEIGHT> ..." zk_verifier-selector`.

---

**Step C — Compare routing algorithms**
*Runs on: k6 EC2 (selector switches on manager)*

**Run 1: weighted at BEST_WEIGHT** (selector already set from Step B):
```bash
ulimit -n 65536   # required — prevents k6 from freezing above ~800 VUs
python3 sweep_throughput.py \
  --target <manager-private-ip> \
  --vus 50,100,200,400,600,800,1000,1200,1500,2000,3000,4000,5000 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_weighted.csv \
  --clean
```

> **VU ceiling on c5.2xlarge (16 GiB):** each VU uses ~1–2 MB of RAM, so 5000 VUs needs up to 10 GiB. The c5.2xlarge handles this comfortably. If you push beyond 5000 VUs and see k6 slowing down, check `free -h` on the k6 EC2 — you may need to upgrade to `c5.4xlarge` (32 GiB).

Switch the selector to round-robin on the **manager**, then clear Redis state:
```bash
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80 --routing roundrobin --snark-cost-weight <BEST_WEIGHT> --stark-cost-weight 1.0" \
  zk_verifier-selector

docker service logs zk_verifier-selector --tail 1
# Must show: Routing=roundrobin

docker exec $(docker ps -q -f name=zk_proof-queue) redis-cli DEL \
  selector:snark_total_cost selector:snark_total_jobs \
  selector:stark_total_cost selector:stark_total_jobs
docker exec $(docker ps -q -f name=zk_snark-queue) redis-cli FLUSHDB
docker exec $(docker ps -q -f name=zk_stark-queue) redis-cli FLUSHDB
docker exec $(docker ps -q -f name=zk_proof-queue) redis-cli DEL proof_queue
docker service update --force zk_verifier-selector
```

**Run 2: round-robin** (back on the k6 EC2):
```bash
ulimit -n 65536   # required — set again if you opened a new shell
python3 sweep_throughput.py \
  --target <manager-private-ip> \
  --vus 50,100,200,400,600,800,1000,1200,1500,2000,3000,4000,5000 \
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

Restore weighted mode on the **manager**:
```bash
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 80 --stark-count 80 --routing weighted --snark-cost-weight 20 --stark-cost-weight 1.0" \
  zk_verifier-selector
```

---

**Step D — Detailed time-series run**
*Runs on: k6 EC2*

Single long test at `KNEE_VU` to produce the time-series CSV:
```bash
ulimit -n 65536   # required — prevents k6 from freezing at high VU counts
k6 run \
  -e TARGET=<manager-private-ip> \
  -e VUS=<KNEE_VU> \
  -e ITERATIONS=<KNEE_VU * 20> \
  -e STARK_RATIO=0.0 \
  load_test.js \
  --out csv=test_results.csv
```

> With KNEE_VU 60–100, `KNEE_VU * 20` gives 1,200–2,000 iterations. Expected runtime ~2–3 minutes.

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
scp -i "zk-authaas-key.pem" ubuntu@3.209.12.24:~/test_results.csv .
python visualize_k6.py
```

### Step 12 — TEAR DOWN (every session, no exceptions)

On the **manager**:
```bash
docker stack rm zk
docker swarm leave --force
exit
```

On the **worker**:
```bash
docker swarm leave --force
exit
```

AWS Console:
- [ ] EC2 → Instances → select **all three** (`zk-authaas-manager`, `zk-authaas-worker`, `zk-authaas-k6`)
- [ ] Instance State → **Terminate**
- [ ] Wait for all three to show `terminated`
- [ ] Confirm EC2 dashboard shows **0 running instances**

> ⚠️ Two forgotten `c5.24xlarge` instances left overnight cost ~$196. **Terminate immediately after each session.**

---

## Cost reference

| Instance | Role | Type | On-demand | 2 hr session |
|---|---|---|---|---|
| Manager | Redis, selector, STARK pool | c5.24xlarge | ~$4.08/hr | ~$8.16 |
| Worker | SNARK pool only | c5.24xlarge | ~$4.08/hr | ~$8.16 |
| k6 | Load generator | c5.2xlarge | ~$0.34/hr | ~$0.68 |
| **Total** | | | | **~$17.00** |

---

## Scale reference (alternate worker counts)

The default deployment is 80+80. To run a smaller scale (e.g. for a quick budget-saving session), change the deploy.replicas before deploying, or scale live:

```bash
# On manager — scale both pools to N+N (substitute your N)
docker service scale zk_snark-verifier=N zk_stark-verifier=N

# Sync the selector to the new count
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count N --stark-count N --routing weighted --snark-cost-weight 1.0 --stark-cost-weight 1.0" \
  zk_verifier-selector

docker service logs zk_verifier-selector --tail 1
# Must print: "Selector started ... SNARK=N nodes, STARK=N nodes ..."
```

> The selector keeps an in-memory scoreboard with one slot per worker. If you scale to N workers but the selector still thinks there are 80, workers N..79 will never receive any jobs and the results will look wrong with no error message.

---

## Known Architectural Limitations

### Why 80+80 across two nodes (and not 500+500 on a single node)

`snarkjs.groth16.verify` uses `ffjavascript` for BN128 elliptic curve operations. Internally, ffjavascript determines its worker thread count using `os.cpus().length`. Inside a Docker container, `os.cpus()` reports **all host CPUs** (e.g. 96 on a c5.24xlarge) regardless of the container's CPU limit — Docker uses CFS quota-based throttling, not CPU masking.

Result: every SNARK verifier container would spawn **95 worker threads** and use `Atomics.wait()` to synchronise them. With only `0.15` vCPU allocated per container (the limit needed to fit 500 workers on 96 vCPUs), the CFS scheduler throttles the container to 15ms of CPU per 100ms period — not enough for 95+ threads to make forward progress. The `Atomics.wait()` call blocks indefinitely, the container shows **0% CPU**, and the job stays at `"processing"` forever. This is a deadlock, not starvation.

**The fix applied in this project** — `SNARKVerifierWorker.js` monkey-patches `os.cpus()` at startup to return a single CPU entry before snarkjs/ffjavascript loads. This causes ffjavascript to spawn only 1 worker thread:

```javascript
// At top of SNARKVerifierWorker.js — must execute before require("snarkjs")
const os = require("os");
const _realCpus = os.cpus.bind(os);
os.cpus = () => [_realCpus()[0]];
```

**Even with the patch, 500 SNARK workers on one node is infeasible** — with 1 worker thread per container, each verify needs ~200ms on 1.0 vCPU. Running 500 workers at 1.0 vCPU requires 500 vCPUs — more than any single EC2 instance provides. The two-node architecture (80 workers × 1.0 vCPU = 80 vCPUs on the dedicated SNARK node) is the practical ceiling for real Groth16 on a single c5.24xlarge.

**A second hard limit:** at ~1000 containers on a single node, the Docker overlay VXLAN forwarding table is overwhelmed and service VIPs (used by selector → worker queues) start flapping with `Error 113: No route to host`. Splitting the pools across two nodes keeps each node well below this limit.

**Academic justification** — in a production SaaS deployment, Groth16 verifiers would be compiled Rust binaries (e.g. `arkworks`, `bellman`) completing in <10ms per proof. The bottleneck demonstrated here is specific to Node.js + WASM. The 80+80 configuration validates the routing, queueing, and cost-weighted scheduling architecture — which is the system under study — at meaningful concurrency with real cryptographic work.

**To scale beyond 80+80 with real Groth16**: add more worker EC2 nodes to the Swarm. Each additional c5.24xlarge node can host another 80 SNARK workers (80 vCPUs at 1.0 each), giving linear horizontal scaling. 6 worker nodes ≈ 500 SNARK workers. This is the intended production topology for a SaaS ZK verification service.

---

## Optional: 500+500 single-node experiment (mock verification only)

This is **not recommended** for a real-Groth16 demo — see the limitations section above. Use it only if you specifically want to stress the queueing/selector layer with mocked verification. It runs on a single `c5.24xlarge` and uses the same Step 1 security group, but only one backend instance and the k6 instance.

Differences from the main flow:
- Launch only **one** backend EC2 (no worker node, no Swarm join)
- Skip Step 6 (no labels needed for a single-node Swarm)
- Replace the SNARK Dockerfile with a mocked version that returns success without calling `snarkjs.groth16.verify`
- Scale to 500+500 after deploy:
  ```bash
  docker service scale zk_snark-verifier=500 zk_stark-verifier=500
  docker service update \
    --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 500 --stark-count 500 --routing weighted --snark-cost-weight 1.0 --stark-cost-weight 1.0" \
    zk_verifier-selector
  ```
- Verify overlay subnet is `/20`, not `/24`:
  ```bash
  docker network inspect zk_zk-net | python3 -m json.tool | grep -A5 "Subnet"
  # Must show: "Subnet": "10.1.0.0/20"
  ```

VU sweep range for 500+500:
```bash
python3 sweep_throughput.py \
  --target <backend-private-ip> \
  --vus 50,100,200,300,400,500,600,800,1000 \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --stark-ratio 0.0 \
  --output sweep_baseline_500.csv \
  --clean
```

---

## Common issues

| Symptom | Fix |
|---|---|
| k6 freezes or is killed mid-sweep | Two causes: (1) file descriptor limit — run `ulimit -n 65536` in the same shell before every sweep (resets on logout); (2) OOM — each VU uses ~1–2 MB of RAM, so 5000+ VUs needs ~10 GiB. The default `c5.2xlarge` (16 GiB) handles up to ~5000 VUs. If you need more, upgrade to `c5.4xlarge` (32 GiB) via Instance Settings → Change Instance Type. |
| Manager and worker EC2s can't ping each other; `docker swarm join` times out or worker tasks never schedule | Nodes are in different security groups, or the self-referencing "All traffic" rule is missing. Fix: put both instances in the **same** security group (`zk-authaas-cluster-sg`) and add an inbound rule **All traffic · Source: `<that SG's own ID>`**. See [Step 1](#step-1--create-the-shared-security-group). |
| `docker: unknown command: docker compose` | Compose V2 not installed. Run the plugin install block in Step 4. |
| `Cannot connect to Docker daemon` | Forgot to re-login after `usermod -aG docker`. SSH out and back in. |
| SNARK service stuck at `0/80` on the worker node | Image not built on that node. Re-run the `docker build -f Dockerfile.snark` step on the worker. |
| SNARK tasks scheduled on the manager (or STARK on the worker) | Node labels missing or wrong. Re-run Step 6 and confirm with `docker node inspect`. |
| `Error: Invalid proof` in SNARK worker logs | `verification_key.json` mismatch — rebuild the SNARK image on the worker: `docker build -f Dockerfile.snark --no-cache -t zk-authaas/snark-verifier:latest .` |
| k6 `connection refused` on port 8000 | k6 EC2 not in `zk-authaas-cluster-sg`, or you used the manager's **public** IP instead of **private** IP. |
| Swarm services stuck `pending` | CPU/memory overcommit. Check `docker service ps` and reduce replicas or resource limits. |
| Weight sweep: `docker service update` fails | Stack name mismatch — default service name is `zk_verifier-selector`. Pass `--service <stack>_verifier-selector` if you used a different stack name. |
| Weight sweep: cost stays flat at ≈ 1.5 across all weights | `/stats/cost` endpoint missing — rebuild and redeploy the request-handler image. |
| Weight sweep: throughput barely changes across weights | VU count too low to saturate any node, or the cost-weight moat is too large (each cheap worker must accumulate `weight` in-flight jobs before any expensive worker is used). Lower the weight to ~1.0 or push more VUs. |
| `docker service scale` stalls, no errors | Kernel inotify limit exhausted (forgot Step 3). Run `sudo sysctl fs.inotify.max_user_instances=8192 && sudo sysctl fs.inotify.max_user_watches=524288`. Scaling resumes within 30 seconds. |
| Services stuck at `0/N` replicas, task state "New", NODE field empty | Custom subnet overlaps with Swarm's ingress network (`10.0.0.0/24`). The compose file uses `10.1.0.0/20` to avoid this — verify with `docker network inspect ingress \| grep Subnet`. |
| SNARK jobs stuck at `"processing"`, verifier shows **0% CPU**, never completes | snarkjs/ffjavascript `Atomics.wait()` deadlock — the `os.cpus()` monkey-patch is missing or the SNARK image was built before it was added. Rebuild the SNARK image on the worker, then `docker service update --force zk_snark-verifier`. See [Known Limitations](#known-architectural-limitations). |
| `verifier-selector` flapping with `Error 113: No route to host` | Overlay VXLAN FDB overwhelmed — only happens above ~1000 containers on one node. The two-node split prevents this. If you see it at 80+80, check that SNARK pool is actually pinned to the worker (Step 6 + Step 8 placement check). |
| Swarm network `zk_zk-net` won't remove after `docker stack rm`, hangs indefinitely | Phantom task reference in Swarm raft state. Fix: `docker swarm leave --force && docker swarm init`. |
