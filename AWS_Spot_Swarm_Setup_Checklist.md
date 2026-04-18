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

Copy the load test from your laptop (run on laptop):
```bash
scp -i zk-authaas-key.pem load_test.js ubuntu@<k6-public-ip>:~/load_test.js
```

### 5. Run the test

On the k6 EC2 — always use the backend's **private IP**:
```bash
k6 run \
  -e TARGET=<backend-private-ip> \
  -e VUS=200 \
  -e ITERATIONS=5000 \
  -e STARK_RATIO=0.0 \
  load_test.js \
  --out csv=test_results.csv
```

### 6. Collect results

```bash
# On your laptop:
scp -i zk-authaas-key.pem ubuntu@<k6-public-ip>:~/test_results.csv ./test_results.csv
python visualize_k6.py
```

### 7. TEAR DOWN — do this every session, no exceptions

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

| Verifier count | Command |
|---|---|
| 10 + 10 (default) | *(no change needed)* |
| 50 + 50 | `docker service scale zk_snark-verifier=50 zk_stark-verifier=50` |
| 500 + 500 | `docker service scale zk_snark-verifier=500 zk_stark-verifier=500` · switch to `c5.24xlarge` |

> After scaling, update `--snark-count` / `--stark-count` in the selector and redeploy the stack.

## Cost reference

| Instance | Type | On-demand | 2 hr session |
|---|---|---|---|
| Backend | c5.4xlarge | ~$0.68/hr | ~$1.36 |
| k6 | t3.small | ~$0.02/hr | ~$0.04 |
| **Total** | | | **~$1.40** |

## Common issues

| Symptom | Fix |
|---|---|
| `Cannot connect to Docker daemon` | Forgot to re-login after `usermod -aG docker`. SSH out and back in. |
| SNARK services stuck at `0/10` | Memory overcommit — check `docker service ps zk_snark-verifier --no-trunc`. Reduce replicas or raise the `memory` limit in `docker-compose.yml`. |
| `Error: Invalid proof` in SNARK worker logs | `verification_key.json` mismatch — rebuild the image: `docker compose build --no-cache` |
| k6 `connection refused` on port 8000 | Security group missing the k6 private IP rule (Step 2), or using public IP instead of private IP. |
| Swarm services stuck `pending` | CPU/memory overcommit. Check `docker service ps` and reduce replicas or resource limits. |
