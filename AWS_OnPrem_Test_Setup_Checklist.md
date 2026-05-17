# ZK-AuthaaS On-Prem Baseline Test Setup Checklist

> **Scope:** Direct k6 → per-domain pipeline. No centralised service, no token issuance.  
> **Companion test:** `AWS_E2E_Test_Setup_Checklist.md` — same workload, "as-a-service" topology.  
> **Goal:** Produce a per-domain latency graph (same style as the service test) so the two architectures can be compared head-to-head under the same load.

---

## What's different from the service test

| | Service test | On-prem test |
|---|---|---|
| Topology | 1 manager + 1 worker + 5 apps + 1 k6 = 8 EC2s | **5 domain EC2s + 1 k6 = 6 EC2s** |
| Total vCPU budget | ~60 (4 + 36 + 20) | **60 (12 × 5 domains)** |
| Verifiers | 32 SNARK on shared worker EC2 | **8 SNARK or 8 U-Prove per domain × 5 = 40 total** |
| Cross-domain pooling | Yes — all 32 workers serve any domain | **No** — each domain has only its own 8 |
| Token issuance | Yes (RS256 JWT, validator on app side) | **No** — verify success IS the completion |
| Selector routing | Weighted (cost-aware) | **Round-robin** |
| k6 endpoint | One (the manager) | **5** (one per domain) |
| Verification algorithm | groth16 SNARK only | **groth16 SNARK, U-Prove, or Idemix** (selectable per run) |

The expected story: under the **hot-domain phase weights (60% to one domain)**, the on-prem model has to absorb the burst with just 8 verifiers while the other 32 sit idle. The service model pools all 32 against whichever domain is hot.

---

## EC2 Topology

| Role            | Instance      | Count | vCPU | RAM   | Purpose                                              |
|-----------------|---------------|-------|------|-------|------------------------------------------------------|
| **Domain 0..4** | c5.4xlarge    | 5     | 16   | 32 GB | Self-contained stack per domain (cap to ~12 used)    |
| **k6**          | c5.large      | 1     | 2    | 4 GB  | Load generator, picks domain by phase weights        |

Total: **6 EC2 instances.** The c5.4xlarge has 16 vCPU but `docker-compose.onprem.yml` sets explicit `cpus:` limits on every service that sum to **11.8 vCPU** per domain (close to the 12-vCPU target, with ~0.2 vCPU headroom for the kernel + Docker daemon). The remaining ~4 vCPU on each c5.4xlarge stays idle to ensure the comparison vs. the service model's 60-vCPU total is enforced, not just guidance.

Per-domain CPU budget (set in compose file):

| Service                | cpus limit |
|------------------------|-----------|
| proof-queue (Redis)    | 0.3 |
| snark-queue (Redis)    | 0.3 |
| request-handler        | 1.8 |
| verifier-selector      | 0.6 |
| 8 × snark-verifier     | 1.1 each → 8.8 |
| **Total per domain**   | **11.8 vCPU** |

Do not bump SNARK count higher than 8 or the comparison stops being apples-to-apples.

Estimated spot cost: **~$0.74/hr** → **~$4.80/session** (6.5 hr including setup/teardown).

---

## Step 0 — Pre-launch (local machine)

- [ ] Confirm `zk-authaas-ec2-key.pem` exists in project root (the public key is NOT needed — no JWT pipeline)
- [ ] You'll be working from the SAME repo as the service test, just running a different compose file (`docker-compose.onprem.yml`)

---

## Step 1 — Create the security group and launch EC2 instances

### Step 1a — Create the security group

Reuse `zk-authaas-cluster-sg` from the service test if it still exists, **or** create a new one with the same rules:

| # | Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|---|
| 1 | SSH | TCP | 22 | My IP | SSH to any EC2 |
| 2 | Custom TCP | TCP | 8000 | My IP | Direct `curl` to a domain's request-handler from your laptop |
| 3 | All traffic | All | All | this same SG (self-referencing) | k6 EC2 → domain EC2s, plus any future inter-node traffic |

Rule 3 is the important one — k6 needs to reach each domain's port 8000 from inside the SG.

### Step 1b — Launch 6 EC2 instances

Same VPC, same subnet (same AZ), same security group.

**Domain EC2s — repeat 5 times:**
- [ ] EC2 → Launch Instance — set **Number of instances** to **5**
- [ ] Name (rename after launch): `zk-authaas-onprem-domain-0` … `zk-authaas-onprem-domain-4`
- [ ] AMI: **Ubuntu 22.04 LTS** · Type: **`c5.4xlarge`** (16 vCPU / 32 GiB)
- [ ] Key pair: `zk-authaas-ec2-key`
- [ ] Security group: `zk-authaas-cluster-sg`
- [ ] Storage: **30 GiB gp3**
- [ ] Record **Public IPv4** (for SSH) and **Private IPv4** (k6 will hit this)

**k6 loader EC2:**
- [ ] Name: `zk-authaas-onprem-k6`
- [ ] AMI: **Ubuntu 22.04 LTS** · Type: **`c5.large`** (2 vCPU / 4 GiB)
- [ ] Same SG / VPC / subnet as the domains
- [ ] Storage: **20 GiB gp3**
- [ ] Record **Public IPv4**

### Step 1c — Record private IPs

```bash
# Fill in your actual private IPs
DOMAIN0_IP=<private-ip>
DOMAIN1_IP=<private-ip>
DOMAIN2_IP=<private-ip>
DOMAIN3_IP=<private-ip>
DOMAIN4_IP=<private-ip>
K6_IP=<private-ip>
```

### Step 1d — Connectivity sanity check (from k6 EC2)

```bash
ssh -i zk-authaas-ec2-key.pem ubuntu@<k6-public-ip>
# Once SSH'd in, ping each domain
for IP in $DOMAIN0_IP $DOMAIN1_IP $DOMAIN2_IP $DOMAIN3_IP $DOMAIN4_IP; do
  echo "--- $IP ---"
  ping -c2 $IP
done
# Expected: 0 % packet loss everywhere
```

---

## Step 2 — Per-Domain EC2 Setup (repeat for all 5 domains)

Identical setup on each domain EC2. `N=0..4` is just the domain number for naming clarity — the stack itself is the same on every node.

```bash
# SSH into domain-N's EC2
ssh -i zk-authaas-ec2-key.pem ubuntu@<domainN-public-ip>

# Install Docker engine
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit

# Re-SSH so the docker group membership takes effect
ssh -i zk-authaas-ec2-key.pem ubuntu@<domainN-public-ip>

# Install Docker Compose V2 plugin (docker.io does NOT include it)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version   # must print: Docker Compose version v2.27.0

# Clone the project repo
rm -rf ~/zk-authaas   # safety: wipe any stale dir
git clone https://github.com/lANXEZ/ZK-AuthaaS-Simulation.git ~/zk-authaas
cd ~/zk-authaas

# Build and start the on-prem stack
# For SNARK runs:
docker compose -f docker-compose.onprem.yml build
docker compose -f docker-compose.onprem.yml up -d
#
# To switch this domain to U-Prove instead, run:
#   docker compose -f docker-compose.onprem.yml down
#   docker compose -f docker-compose.onprem-uprove.yml build
#   docker compose -f docker-compose.onprem-uprove.yml up -d
# (See "Step 5b — U-Prove run" below for the full A/B procedure.)

# Wait for everything to come up
sleep 5

# Confirm all 12 containers are running:
#   1 request-handler, 1 selector, 2 redis (proof/snark), 8 snark verifiers
docker compose -f docker-compose.onprem.yml ps
# All must show STATE=running (NOT "exited" or "restarting").

# Smoke check
curl -s -X POST http://localhost:8000/verify/submit \
  -H "Content-Type: application/json" \
  -d '{"scheme":"snark","proof":{"pi_a":["1","2","3"],"pi_b":[["1","2"],["3","4"],["1","0"]],"pi_c":["1","2","3"],"protocol":"groth16","curve":"bn128"},"public_inputs":["1","2","3","4","5","6"]}'
# Expected: {"status":"accepted","job_id":"...","domain_id":0}
# (The verifier will reject this stub proof and mark the job 'failed' —
#  that's fine; we're just confirming the request-handler is wired up.
#  The full valid proof in Step 4 will exercise the verifier successfully.)
```

> **If a container is restart-looping:** `docker compose -f docker-compose.onprem.yml logs <service-name>` to see the error. Common causes: Redis not reachable yet (transient — wait 10 s and re-check), or a SNARK worker crash (snarkjs version mismatch).

---

## Step 3 — k6 EC2 Setup

```bash
ssh -i zk-authaas-ec2-key.pem ubuntu@<k6-public-ip>

# Install k6
sudo apt update && sudo apt install -y gpg curl
curl -s https://dl.k6.io/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/k6-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update && sudo apt install -y k6

# Clone the project repo for load_test_onprem.js + visualize_k6_per_app.py
rm -rf ~/zk-authaas
git clone https://github.com/lANXEZ/ZK-AuthaaS-Simulation.git ~/zk-authaas
ln -sf ~/zk-authaas/load_test_onprem.js ~/load_test_onprem.js
ln -sf ~/zk-authaas/visualize_k6_per_app.py ~/visualize_k6_per_app.py
# (U-Prove no longer needs a proof file on the k6 EC2 — the C# SDK worker
#  has the bundle baked into its container image.)
```

---

## Step 4 — Smoke Test (End-to-End)

Confirm one full submit → verify → completed cycle against each domain.

```bash
# On the k6 EC2 — uses the same valid groth16 proof as the service test
ulimit -n 65536

# Sanity-test each domain by submitting one valid proof and polling its status
for i in 0 1 2 3 4; do
  IP_VAR="DOMAIN${i}_IP"
  IP=${!IP_VAR}
  JID=$(curl -s -X POST http://$IP:8000/verify/submit \
    -H "Content-Type: application/json" \
    -d '{
      "scheme":"snark",
      "proof":{"pi_a":["16893334615242764580836222078829142520432756203770466604081032720388657032757","5095606969395716303621702958471922376961618029789842152295821108717087682311","1"],"pi_b":[["13772398192624595577472662855811728500397412494267729711099372526485968374649","15249941699599606024139723272508104548269790148997217612719623411267570558493"],["19735295879188043871505513529932228526631701925990878770250928234435443795397","11046809327765151786114304454515091703284305019483922364766276175300463695885"],["1","0"]],"pi_c":["18536201733965390491456176988021021022761142364866628667452517360063595662975","15291715715367874403418883228408929985980666544091293542955491873294267230352","1"],"protocol":"groth16","curve":"bn128"},
      "public_inputs":["1120771572304984668855649788542860110303223894298952018121329196339919157573","20197087425205130352574209034729275460185533126585197591053247747830393653846","111222333","444555666","1764263975784332459809300572476310454427845461305579380554772042455913567929","10988278040513707334400680073433620711051179041727267619401283491695328957763"]
    }' | python3 -c "import sys, json; print(json.load(sys.stdin)['job_id'])")
  sleep 2
  STATUS=$(curl -s http://$IP:8000/verify/status/$JID | python3 -c "import sys, json; print(json.load(sys.stdin)['status'])")
  echo "domain $i → job $JID → $STATUS"
done
# Expected every line: status=completed
```

---

## Step 5 — Shifting-Focus Test

Same 5-phase rotation as the service test (20 s per domain × 5 = 100 s).
Use the **same VUS** as the service test for a fair head-to-head (e.g. `VUS=200`).

```bash
# On k6 EC2 (inside tmux, after ulimit)
ulimit -n 65536

k6 run \
  -e TARGETS=$DOMAIN0_IP,$DOMAIN1_IP,$DOMAIN2_IP,$DOMAIN3_IP,$DOMAIN4_IP \
  -e VUS=200 \
  -e DURATION=100s \
  load_test_onprem.js \
  --out csv=test_results_onprem.csv
```

**What to expect during the run:**
- Throughput per phase is split 60/10/10/10/10 across domains.
- The **hot domain** sees ~60% of submits in its window — landing on **just 8 verifiers** in that one domain. If 8 verifiers can't keep up, the hot domain's p95 latency will spike during its window. This is the central story.
- Cold domains (10% each) stay near baseline.

Summary thresholds to watch:
- `submit_failures` should be 0
- `verification completed` check ratio should stay > 95%
- `e2e_latency` p95 — compare to the service test's value at the same VUS

This produces `test_results_onprem.csv` (SNARK on-prem baseline). Save it before
moving on to the U-Prove run, otherwise the next step's output will overwrite it.

---

## Step 5b — U-Prove run (same workload, different algorithm)

After capturing the SNARK on-prem results above, swap each domain's worker
stack from SNARK to U-Prove and re-run the same k6 script with `-e ALG=uprove`.
The request-handler, selector, and Redis containers are unchanged — only the
8 verifier replicas per domain are different.

**On every domain EC2 (0..4):**
```bash
cd ~/zk-authaas
docker compose -f docker-compose.onprem.yml down
git pull   # picks up uprove_verifier.py, UProveVerifierWorker.py, Dockerfile.uprove, docker-compose.onprem-uprove.yml
docker compose -f docker-compose.onprem-uprove.yml build
docker compose -f docker-compose.onprem-uprove.yml up -d
sleep 5
docker compose -f docker-compose.onprem-uprove.yml ps
# All 12 containers should be 'running' — now with uprove-verifier-0..7 instead of snark-verifier-0..7
```

**On the k6 EC2:**
```bash
cd ~/zk-authaas && git pull && cd ~
ulimit -n 65536

k6 run \
  -e TARGETS=$DOMAIN0_IP,$DOMAIN1_IP,$DOMAIN2_IP,$DOMAIN3_IP,$DOMAIN4_IP \
  -e VUS=200 \
  -e DURATION=100s \
  -e ALG=uprove \
  load_test_onprem.js \
  --out csv=test_results_onprem_uprove.csv
```

> **Note on the U-Prove implementation:** this worker runs the
> **Microsoft U-Prove C# SDK** (the official, production-grade reference
> implementation; sources under `uprove-cs/sdk/`). The proof bundle
> generated by `UProveGen` is baked into the worker image at build time;
> the SDK's `PresentationProof.Verify()` runs on every job. First build
> per domain pulls ~600 MB of .NET 6 base images plus a NuGet restore —
> expect 5–10 min before the workers come up. Subsequent rebuilds hit
> the layer cache.

---

## Step 5c — Idemix run (same workload, third algorithm)

Same A/B procedure as Step 5b, but swap to the Idemix stack. The Idemix
verifier is a compiled Go binary using `hyperledger/fabric/idemix` — the
proof + issuer public key + revocation public key are baked into the image
from `idemix/*.bin`, so the k6 payload is just a trigger.

**On every domain EC2 (0..4):**
```bash
cd ~/zk-authaas
docker compose -f docker-compose.onprem-uprove.yml down 2>/dev/null
docker compose -f docker-compose.onprem.yml down 2>/dev/null
git pull   # picks up idemix/, Dockerfile.idemix, docker-compose.onprem-idemix.yml
# First build of the Idemix image takes 3–5 minutes (Go module download + compile
# of hyperledger/fabric dependency tree). Subsequent builds are cached.
docker compose -f docker-compose.onprem-idemix.yml build
docker compose -f docker-compose.onprem-idemix.yml up -d
sleep 8   # Go binary loads + parses .bin files at startup; give it a moment
docker compose -f docker-compose.onprem-idemix.yml ps
# All 12 containers should be 'running' — now with idemix-verifier-0..7
```

> **Verify the startup parse succeeded:**
> ```bash
> docker compose -f docker-compose.onprem-idemix.yml logs idemix-verifier-0 | tail -5
> # Expected: "Idemix worker idx=0 ready; startup verify OK" followed by
> # "Idemix worker idx=0 listening on 'snark_queue:0'"
> ```

**On the k6 EC2:**
```bash
cd ~/zk-authaas && git pull && cd ~
ulimit -n 65536

k6 run \
  -e TARGETS=$DOMAIN0_IP,$DOMAIN1_IP,$DOMAIN2_IP,$DOMAIN3_IP,$DOMAIN4_IP \
  -e VUS=200 \
  -e DURATION=100s \
  -e ALG=idemix \
  load_test_onprem.js \
  --out csv=test_results_onprem_idemix.csv
```

> **Note on Idemix verify cost:** unlike U-Prove's pure-Python implementation,
> Idemix here is a Go binary built against `hyperledger/fabric/idemix` — fully
> implemented bilinear-pairing verification (BN256 curve). Expect per-verify
> latency in the few-millisecond range, between SNARK (snarkjs) and U-Prove
> (simplified pure-Python). Three points on the comparison curve.

---

## Step 6 — Visualize and Compare

Copy the CSV back and render the per-domain latency graph using the same visualiser as the service test.

```powershell
# From your laptop — pull all three CSVs back
cd "E:\Work\VSCode Repo\ZK-AuthaaS Simulation"
scp -i "zk-authaas-ec2-key.pem" ubuntu@18.215.167.184:~/test_results_onprem.csv          .
scp -i "zk-authaas-ec2-key.pem" ubuntu@18.215.167.184:~/test_results_onprem_uprove.csv   .
scp -i "zk-authaas-ec2-key.pem" ubuntu@18.215.167.184:~/test_results_onprem_idemix.csv   .

python visualize_k6_per_app.py --input test_results_onprem.csv         --output onprem_snark_latency_graph.png
python visualize_k6_per_app.py --input test_results_onprem_uprove.csv  --output onprem_uprove_latency_graph.png
python visualize_k6_per_app.py --input test_results_onprem_idemix.csv  --output onprem_idemix_latency_graph.png
```

You now have four graphs in identical style:
- `latency_graph.png` (service model, from the earlier test)
- `onprem_snark_latency_graph.png` (on-prem with SNARK verifier)
- `onprem_uprove_latency_graph.png` (on-prem with U-Prove verifier)
- `onprem_idemix_latency_graph.png` (on-prem with Idemix verifier)

**Reading the comparison:**
- During each hot-domain window, the on-prem graph should show that domain's line **rise visibly** (its 8 verifiers are now serving 60% of total traffic), while the cold-domain lines stay flat.
- The service graph (from the earlier test) was nearly flat across all domains, because the pool of 32 workers absorbed the skew.
- The **average and p95** of the hot domain's window are the headline numbers — quote them side by side.

---

## Step 7 — Teardown

```bash
# On EACH domain EC2 (0..4) — bring down whichever stack is currently running:
docker compose -f ~/zk-authaas/docker-compose.onprem.yml         down 2>/dev/null
docker compose -f ~/zk-authaas/docker-compose.onprem-uprove.yml  down 2>/dev/null
docker compose -f ~/zk-authaas/docker-compose.onprem-idemix.yml  down 2>/dev/null

# Terminate all 6 EC2 instances (AWS Console or CLI):
aws ec2 terminate-instances --instance-ids \
  <domain0-id> <domain1-id> <domain2-id> <domain3-id> <domain4-id> <k6-id>
```

Confirm all 6 instances show `terminated` (not `stopped`) in the EC2 console before walking away.

---

## Expected Performance (rough targets)

| Metric                            | On-prem  | Service (for comparison) |
|-----------------------------------|----------|--------------------------|
| Verifiers per domain (hot)        | 8        | 32 (pooled, all domains) |
| Hot-domain theoretical ceiling    | ~180 req/s | ~250 req/s             |
| Cold-domain latency               | low      | low                      |
| Hot-domain latency (during phase) | **elevated** — this is the headline | flat (pooled workers absorb) |
| Total throughput at VUS=200       | TBD by your run | ~250 req/s            |

---

## Data Flow Summary (on-prem)

```
k6 picks domain_id = N (0..4) per request based on current phase weights
  │  picks BASE_URLS[N]
  │  POST /verify/submit {proof, public_inputs, domain_id: N}
  ▼
DOMAIN N's request-handler         (this domain's container only)
  │  set status:{job_id} = "pending"
  │  lpush proof_queue              (this domain's Redis)
  ▼
DOMAIN N's verifier-selector       (round-robin across this domain's 8 slots)
  │  lpush snark_queue:{slot}
  ▼
DOMAIN N's SNARK verifier (1 of 8) (this domain's worker)
  │  set status:{job_id} = "processing"
  │  groth16.verify(...)
  │  ├─ success: set status:{job_id} = "completed"   ← verify IS the completion
  │  └─ failure: set status:{job_id} = "failed"
  │
k6 polls GET /verify/status/{job_id} → sees "completed"
  └── records latency tagged with domain=N → per-domain metrics in CSV
```

The single critical contrast with the service flow: when domain N is hot, only **domain N's 8 verifiers** are doing any work — domains 0..N-1, N+1..4 each have 8 verifiers sitting idle.
