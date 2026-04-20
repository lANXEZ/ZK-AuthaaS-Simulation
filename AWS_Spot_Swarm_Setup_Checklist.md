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

> **`docker compose` not found?** The `docker.io` apt package does not include Compose V2. Install it as a CLI plugin before proceeding:
> ```bash
> sudo mkdir -p /usr/local/lib/docker/cli-plugins
> sudo curl -SL https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
>   -o /usr/local/lib/docker/cli-plugins/docker-compose
> sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
> docker compose version   # must print: Docker Compose version v2.27.0
> ```
> This is a one-time step per EC2 instance. The plugin is installed system-wide so it survives re-login.

```bash
git clone <your-repo-url> zk-authaas && cd zk-authaas
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
docker service scale zk_snark-verifier=50 zk_stark-verifier=50
```

**Update the selector to match** (this restarts only the selector container):
```bash
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 50 --stark-count 50" \
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

Switch to round-robin on the backend EC2:
```bash
# On backend EC2:
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 50 --stark-count 50 --routing roundrobin --snark-cost-weight <BEST_WEIGHT> --stark-cost-weight 1.0" \
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
# On backend EC2:
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 50 --stark-count 50 --routing weighted --snark-cost-weight <BEST_WEIGHT> --stark-cost-weight 1.0" \
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

**500 + 500 (switch to `c5.24xlarge` first):**
```bash
docker service scale zk_snark-verifier=500 zk_stark-verifier=500
docker service update \
  --args "python verifierSelector.py --proof-host proof-queue --proof-port 6379 --snark-host snark-queue --snark-port 6379 --stark-host stark-queue --stark-port 6379 --snark-count 500 --stark-count 500 --routing weighted --snark-cost-weight 10.0 --stark-cost-weight 1.0" \
  zk_verifier-selector
```

## Cost reference

| Instance | Type | On-demand | 2 hr session |
|---|---|---|---|
| Backend | c5.4xlarge | ~$0.68/hr | ~$1.36 |
| k6 | t3.small | ~$0.02/hr | ~$0.04 |
| **Total** | | | **~$1.40** |

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
