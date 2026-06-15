# ZK-AuthaaS Throughput Scalability Test Setup Checklist (Track 4)

> **Scope:** Paired service-vs-on-prem **throughput** comparison at four total-vCPU budgets (10 / 20 / 40 / 60), proving the service model's scalability under skewed load.
> **Companions:** `AWS_E2E_Test_Setup_Checklist.md` (Track 2 — same service topology, latency comparison) and `AWS_OnPrem_Test_Setup_Checklist.md` (Track 3 — same on-prem topology). This checklist reuses their setup procedures and only spells out what is different.
> **Goal:** For each budget round, find the service system's saturation point (knee VU), run the shifting-focus workload on both architectures at that VU, and plot per-domain **throughput** — plus one summary graph of throughput vs vCPU.

---

## How this test differs from the latency pairing (Tracks 2 & 3)

| | Tracks 2 + 3 (latency) | Track 4 (this test) |
|---|---|---|
| Measured quantity | per-domain p95 latency | **per-domain throughput (completions/s)** |
| vCPU budget | fixed ~60 total | **swept: 10 → 20 → 40 → 60 (4 rounds)** |
| Budget enforcement | instance sizes | **explicit `cpus:` limit on every container** — instances stay fixed, limits change per round |
| VU level | sweep once, then VUS=200 head-to-head | **one VU sweep per round (on E2E)** → both systems run at that round's `KNEE_VU` |
| Verifiers | E2E: 32 pooled · on-prem: 8×1.1 vCPU per domain | **identical on both sides per round: 5 / 15 / 30 / 45 workers × 1.0 vCPU** (on-prem: 1 / 3 / 6 / 9 per domain) |
| Algorithms | SNARK + U-Prove + Idemix (on-prem) | **SNARK only**, both sides |
| Test runs | 2 head-to-head runs | **8** (4 rounds × 2 systems) |
| Compose files | `docker-compose.yml`, `docker-compose.onprem.yml` | `docker-compose.e2e-throughput.yml`, `docker-compose.onprem-throughput.yml` + 8 round env files |

**The allocation principle:** each round, both systems get the **same total budget and the same number of 1.0-vCPU SNARK verifiers**. The remainder ("overhead") is spent however each architecture needs — E2E on the token pipeline (Redis ×3, handlers, selector, issuers, app validators), on-prem on 5× replicated per-domain plumbing (Redis ×2, handler, selector). When the throughput curves diverge, the only available explanation is **pooled vs partitioned verifiers** — neither side ever has more compute than the other.

---

## The round table

| Round | Total budget | E2E verifier pool | On-prem verifiers/domain | E2E overhead | E2E issuer replicas | Expected E2E throughput¹ |
|---|---|---|---|---|---|---|
| 1 | 10 vCPU | 5 | 1 | 5.0 | 2 | ~210/s (verifier-limited) |
| 2 | 20 vCPU | 15 | 3 | 5.0 | 2 | ~410/s (token-pipeline-limited) |
| 3 | 40 vCPU | 30 | 6 | 10.0 | 4 | ~880/s |
| 4 | 60 vCPU | 45 | 9 | 15.0 | 6 | ~1280/s |

¹ Empirical, post-rebalance. The measured per-verifier rate on the c5.12xlarge worker is ~42/s (not the README's conservative 23/s), so 5/15/30/45 verifiers could in principle do ~210/630/1260/1890. But from round 2 on, the **token-issuer (RS256 signing) is the binding constraint** — see the rebalance note below. Your per-round sweep decides the real knee.

> 🔬 **Why the overhead is issuer-heavy (rounds 2–4 rebalanced 2026-06).** A `docker stats` capture at 500 VU on round 4 showed all token-issuer replicas pinned at their CPU cap while the verifier-selector (~33%), request-handlers (~43% of cap), and token-validators (~6%) all had headroom. RS256 JWT signing is the service model's real cost. The env files now move the wasted validator budget into more issuer replicas (and keep the request-handler matched so it doesn't just become the next wall), holding verifiers and the total budget fixed. The on-prem side has **no token-issuer**, so it pays none of this — which is exactly why the service's edge shows up in the **hot-domain** comparison (pooling under skew), not necessarily in raw total throughput.

The exact per-service CPU splits live in the 8 env files (`e2e-throughput-round{1..4}.env`, `onprem-throughput-round{1..4}.env`) — each file has its allocation table in a header comment. After each round's sweep, run the Step A3b capacity check; if a *different* component is now pinned (handler, or validators during a domain's hot window), shift a few tenths in that round's env file and re-sweep.

> ⚠️ **Round 4 is NOT a re-run of the Track 2/3 configuration.** It uses 45 pooled / 9-per-domain verifiers at 1.0 vCPU (vs 32 / 8×1.1) so the series stays internally consistent. Don't mix its numbers with the latency test's.

---

## EC2 Topology

Both sessions use **fixed instances sized for round 4**; rounds are switched by scaling containers, never by resizing instances.

**Part A — Service (E2E) session: 8 instances**

| Role | Instance | Count | vCPU | Notes |
|---|---|---|---|---|
| Manager | **c5.4xlarge** | 1 | 16 | bigger than Track 2's c5.xlarge — must host up to 10.3 vCPU of capped management services in round 4 |
| Worker | **c5.12xlarge** | 1 | 48 | hosts up to 45 × 1.0-vCPU SNARK verifiers |
| App 0..4 | c5.xlarge | 5 | 4 | TokenValidatorService, CPU-capped per round via `docker update` |
| k6 | c5.large | 1 | 2 | load generator |

**Part B — On-prem session: 6 instances**

| Role | Instance | Count | vCPU | Notes |
|---|---|---|---|---|
| Domain 0..4 | c5.4xlarge | 5 | 16 | hosts up to 12.0 vCPU of capped containers in round 4 |
| k6 | c5.large | 1 | 2 | load generator |

Estimated spot cost: Part A ≈ $1.3/hr × ~6 hr ≈ **$8**, Part B ≈ $0.75/hr × ~4 hr ≈ **$3.50** (on-demand roughly 3×). Run the parts as **two separate sessions** — Part B needs the four `KNEE_VU` values recorded in Part A.

---

## A note on where each command runs (`bash` vs `powershell`)

Code blocks are fenced by **where you type them**, because this repo is driven from a Windows laptop:

- ` ```bash ` blocks run **on an EC2 instance** (Ubuntu) — *after* you `ssh` in. Copy them into that remote shell as-is.
- ` ```powershell ` blocks run **on your Windows laptop** (the `PS C:\...>` prompt). These are the `scp` copy-backs, the `python` visualizers, and the two SSH loops that apply validator CPU caps.

If you paste a `bash` `for ... do ... done` loop straight into PowerShell you'll get `Missing opening '(' after keyword 'for'` — that loop was meant for an EC2 shell, or (for the laptop-side validator-cap loops) has already been rewritten in PowerShell `foreach` form below.

**One Windows-specific gotcha:** when a laptop-side `ssh` command carries a remote command string, keep that string **single-quoted with no inner double quotes**. Windows PowerShell 5.1 strips embedded `"` when passing arguments to a native `.exe`, which silently mangles the remote command (e.g. a `docker inspect -f "… {{.Field}}"` format string gets split into separate arguments and fails with `no such object`). The loops below are already written this way.

---

# Part A — Service (E2E) session

## Step A1 — Launch and base setup

Follow `AWS_E2E_Test_Setup_Checklist.md` **Steps 0–7 verbatim**, with only these substitutions:

- [ ] Manager instance type: **`c5.4xlarge`** (not c5.xlarge)
- [ ] Worker instance type: **`c5.12xlarge`** (not c5.9xlarge)
- [ ] Worker storage: 30 GiB gp3 (unchanged); remember the **inotify sysctl bump** on the worker
- [ ] In Step 7 (image build), build from the Track 4 compose file instead:
  ```bash
  # On Manager:
  cd ~/zk-authaas
  docker compose -f docker-compose.e2e-throughput.yml build request-handler verifier-selector token-issuer

  # On Worker:
  cd ~/zk-authaas
  docker compose -f docker-compose.e2e-throughput.yml build snark-verifier
  ```

Everything else — security group, the 5 App EC2s (Step 2, including `DOMAIN_ID` and `PROOF_QUEUE_HOST`), swarm init/join, `pool=snark` node label, k6 install — is identical. Record all private IPs as in Track 2's Step 1c.

## Step A2 — Deploy round 1

```bash
# On Manager — app IPs first (needed by token-issuer):
export APP0_IP=<app0-private-ip>
export APP1_IP=<app1-private-ip>
export APP2_IP=<app2-private-ip>
export APP3_IP=<app3-private-ip>
export APP4_IP=<app4-private-ip>

# Persist them to a file so EVERY later round (and any fresh SSH shell) can
# re-source the same values. A missing export here is the #1 cause of
# "submits accepted but 0% completed": docker stack deploy silently substitutes
# 127.0.0.1 for any unset APP*_IP, so the token-issuer POSTs every JWT to itself
# and no validator ever writes status=completed.
cat > ~/app_ips.env <<EOF
export APP0_IP=$APP0_IP
export APP1_IP=$APP1_IP
export APP2_IP=$APP2_IP
export APP3_IP=$APP3_IP
export APP4_IP=$APP4_IP
EOF

cd ~/zk-authaas
set -a; source e2e-throughput-round1.env; set +a
docker stack deploy -c docker-compose.e2e-throughput.yml zk
```

Verify:

```bash
docker service ls
# Round 1 expected replicas:
#   zk_proof-queue        1/1
#   zk_snark-queue        1/1
#   zk_token-queue        1/1
#   zk_request-handler    2/2
#   zk_verifier-selector  1/1
#   zk_snark-verifier     5/5
#   zk_token-issuer       2/2
# (no stark services in this track)

# Selector must report the round's verifier count:
TASK=$(docker service ps zk_verifier-selector --filter "desired-state=running" -q)
docker inspect --format '{{.Status.ContainerStatus.ContainerID}}' $TASK | xargs docker logs 2>&1 | head -3
# Startup line must show snark-count=5

# token-issuer must have the REAL app IPs, NOT 127.0.0.1 (reads a local
# container's args — never hangs, unlike `docker service logs`):
docker inspect $(docker ps -qf name=zk_token-issuer | head -1) --format '{{json .Args}}'
# the --app-urls value must list your 5 private IPs; if it shows 127.0.0.1 the
# APP*_IP exports were missing at deploy time — fix and redeploy.
```

Apply the round's CPU cap to the 5 app validators (they run outside the Swarm stack, so the limit is applied directly to the running container — `VALIDATOR_CPUS` is in the same env file):

```powershell
# From your laptop (PowerShell), for each App EC2 public IP.
# NOTE 1: do not use $HOST as the loop variable — it is a PowerShell automatic
#         variable. Use $APP (or any other name).
# NOTE 2: the remote command has NO inner double quotes on purpose. Windows
#         PowerShell 5.1 strips embedded " when handing an argument to a native
#         .exe (ssh), which would split the docker inspect format string and
#         produce "no such object" errors. The bare {{...}} template has no
#         spaces, so bash on the EC2 accepts it unquoted.
# The single-quoted string is passed to the EC2's bash verbatim (so $(...) and
# && run there, not locally).
foreach ($APP in @("<app0-pub>","<app1-pub>","<app2-pub>","<app3-pub>","<app4-pub>")) {
  Write-Host "=== $APP ==="
  ssh -i zk-authaas-ec2-key.pem ubuntu@$APP `
    'docker update --cpus=0.4 $(docker ps -qf name=token-validator) && docker inspect -f {{.HostConfig.NanoCpus}} $(docker ps -qf name=token-validator)'
}
# Each host prints its container id, then 400000000 (= 0.4 vCPU)
```

> ⚠️ `docker update` survives container restarts but is **reset by `docker compose up`** — if you ever re-run compose on an App EC2, re-apply the cap.

Then run the **Step 9 sanity check from `AWS_E2E_Test_Setup_Checklist.md`** unchanged (one valid proof per domain → all `completed`, every app sees its own traffic) and the **Step 11 smoke test** (`VUS=10 ITERATIONS=50`).

## Step A3 — Per-round procedure (repeat for R = 1, 2, 3, 4)

Work on the k6 EC2 inside `tmux`, with `ulimit -n 65536` set in that shell.

### A3a — Reconfigure to round R (skip for round 1 — already deployed)

```bash
# On Manager. ALWAYS re-source the app IPs first — a fresh SSH shell has lost
# the exports, and deploying without them silently wires the token-issuer to
# 127.0.0.1 (→ submits accepted but 0% completed).
source ~/app_ips.env
cd ~/zk-authaas

# Guard: refuse to deploy unless all 5 app IPs are real (non-empty, non-loopback).
# Safe to paste interactively — it never exits your shell, it just skips the deploy.
BAD=""
for V in APP0_IP APP1_IP APP2_IP APP3_IP APP4_IP; do
  case "${!V}" in ""|127.*|localhost) BAD="$BAD $V";; esac
done
if [ -n "$BAD" ]; then
  echo "[ABORT] App IPs unset/loopback:$BAD — fix ~/app_ips.env, do NOT deploy."
else
  set -a; source e2e-throughput-round<R>.env; set +a
  docker stack deploy -c docker-compose.e2e-throughput.yml zk
fi

# Clear state from the previous round:
docker exec $(docker ps -qf name=zk_proof-queue) redis-cli FLUSHALL
docker exec $(docker ps -qf name=zk_snark-queue) redis-cli FLUSHALL
docker exec $(docker ps -qf name=zk_token-queue) redis-cli FLUSHALL
docker service update --force zk_verifier-selector   # reset in-memory pseudo-queues

# Verify replica counts changed (snark-verifier must be 5/15/30/45 for R=1/2/3/4)
docker service ls
# Verify the CURRENT selector container logs snark-count=<round's count>
TASK=$(docker service ps zk_verifier-selector --filter "desired-state=running" -q)
docker inspect --format '{{.Status.ContainerStatus.ContainerID}}' $TASK | xargs docker logs 2>&1 | head -3
# Verify token-issuer got the REAL app IPs (NOT 127.0.0.1):
docker inspect $(docker ps -qf name=zk_token-issuer | head -1) --format '{{json .Args}}'
```

```powershell
# From your laptop (PowerShell): re-apply validator caps with the round's
# VALIDATOR_CPUS. Set the value ONCE on the first line — do not leave a
# <placeholder> inside the ssh command (bash would read <NAME> as a file
# redirection and fail with "No such file or directory").
$VALIDATOR_CPUS = "0.15"   # round value: 0.4 / 0.15 / 0.3 / 0.5 for R1/R2/R3/R4
foreach ($APP in @("<app0-pub>","<app1-pub>","<app2-pub>","<app3-pub>","<app4-pub>")) {
  # Double-quoted so $VALIDATOR_CPUS expands locally; `$(...) is escaped so the
  # container-id lookup runs on the EC2's bash, not in PowerShell.
  ssh -i zk-authaas-ec2-key.pem "ubuntu@$APP" `
    "docker update --cpus=$VALIDATOR_CPUS `$(docker ps -qf name=token-validator)"
}
```

### A3b — VU sweep → record KNEE_VU_R

> ✅ **Before sweeping, run the Step 9 single-job sanity check** (one valid proof
> per domain → all `completed`). It takes seconds and catches a broken pipeline
> — e.g. token-issuer on 127.0.0.1, a down validator, or a verifier/selector
> count mismatch — *before* you waste a full sweep getting 0% completion.

```bash
# On k6 EC2 (inside tmux)
ulimit -n 65536
python3 sweep_throughput.py \
  --target <manager-private-ip> \
  --vus <ROUND_VU_LIST> \
  --iterations-per-vu 10 \
  --cooldown 15 \
  --output sweep_e2e_thr_r<R>.csv \
  --clean
```

Suggested `<ROUND_VU_LIST>` per round (bracket the expected knee):

| Round | VU list |
|---|---|
| 1 | `10,25,50,75,100,150,250` |
| 2 | `25,50,100,150,200,300,450` |
| 3 | `50,100,200,300,400,600,900` |
| 4 | `50,100,200,400,600,800,1200,1800` |

Then copy the sweep CSV back to your laptop and render the knee graph. The
sweep ran on the **k6 EC2**, so `scp` from there (use its **public** IP):

```powershell
# From your laptop (PowerShell). Set the round you just swept.
$R = 2                          # 1 / 2 / 3 / 4
$K6_PUB = "<k6-public-ip>"      # k6 EC2 public IPv4
cd "C:\Work\VSCodeRepo\ZK-AuthaaS-Simulation"

# Pull the sweep CSV down …
scp -i "zk-authaas-ec2-key.pem" "ubuntu@${K6_PUB}:~/sweep_e2e_thr_r${R}.csv" .

# … and plot the throughput-vs-VUs knee curve (saves a PNG next to the CSV).
python visualize_sweep.py --input "sweep_e2e_thr_r${R}.csv" --output "sweep_e2e_thr_r${R}_graph.png"
```

Open `sweep_e2e_thr_r<R>_graph.png` and read the **knee** — the VU level where
the throughput curve stops climbing and flattens. That VU is `KNEE_VU_R`. (You
can also eyeball the knee straight from the CSV's throughput column if you'd
rather skip the graph.)

📝 **Record `KNEE_VU_R`** in this table as you go — Part B consumes it:

| Round | Budget | KNEE_VU | Peak throughput at knee |
|---|---|---|---|
| 1 | 10 | ______ | ______ /s |
| 2 | 20 | ______ | ______ /s |
| 3 | 40 | ______ | ______ /s |
| 4 | 60 | ______ | ______ /s |

> ✅ **Capacity check (do not skip):** during a sustained run at the knee, take a `docker stats` snapshot on the manager and on an app EC2 (CPUPerc is per-core, so compare each container to its own `cpus` cap). In the **rebalanced** config the **token-issuer and request-handler should both sit near their caps** (they're matched), while the selector, Redis, and validators have headroom — that's a healthy, well-balanced round. If instead *one* component is pinned and the rest are idle, shift a few tenths of a vCPU in that round's env file toward the pinned service (taking it from whatever's idle, keeping the round total fixed), redeploy, and re-sweep. Watch the **validators during a domain's hot window** — each one carries 60 % of traffic in turn, so a validator cap that looks fine under uniform load can pin during its hot phase.
>
> ```bash
> # Manager, during the knee load:
> docker stats --no-stream --format "{{.Name}}\t{{.CPUPerc}}"
> # token-issuer & request-handler near cap = balanced; one pinned + rest idle = rebalance
> ```

### A3c — Shifting-focus throughput run at KNEE_VU_R

```bash
# On k6 EC2 (inside tmux, after ulimit)
k6 run \
  -e TARGET=<manager-private-ip> \
  -e VUS=<KNEE_VU_R> \
  -e DURATION=100s \
  load_test.js \
  --out csv=test_results_e2e_thr_r<R>.csv
```

Same 5-phase rotation as Track 2: each domain is hot (60 % of traffic) for 20 s, the other four get 10 % each. Watch `submit_failures` (should be 0) and the `verification completed` check ratio (> 95 %).

The CSV name **must** keep the `_r<R>` suffix — all four files are needed at the end.

→ Loop back to A3a for the next round.

## Step A4 — Collect results and tear down

```powershell
# From your laptop:
cd "C:\Work\VSCodeRepo\ZK-AuthaaS-Simulation"
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e_thr_r1.csv .
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e_thr_r2.csv .
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e_thr_r3.csv .
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_e2e_thr_r4.csv .
```

Tear down exactly as Track 2 Step 14: `docker stack rm zk` + `docker swarm leave --force` on manager/worker, `docker compose -f docker-compose.app.yml down` on each app, **terminate all 8 instances**. Keep your filled-in `KNEE_VU` table — Part B needs it.

---

# Part B — On-prem session

## Step B1 — Launch and base setup

Follow `AWS_OnPrem_Test_Setup_Checklist.md` **Steps 0–3 verbatim** (same 5 × c5.4xlarge domains + 1 × c5.large k6, same SG, same Docker/k6 installs), with one substitution on each domain EC2 — build and start the **Track 4** stack at round 1 instead of `docker-compose.onprem.yml`:

```bash
# On each domain EC2 (0..4):
cd ~/zk-authaas
docker compose -f docker-compose.onprem-throughput.yml build
docker compose --env-file onprem-throughput-round1.env \
  -f docker-compose.onprem-throughput.yml up -d
sleep 5
docker compose -f docker-compose.onprem-throughput.yml ps
# Round 1 expects 5 running containers:
#   proof-queue, snark-queue, request-handler, verifier-selector, snark-verifier-0
```

Run the **Step 4 smoke test from `AWS_OnPrem_Test_Setup_Checklist.md`** unchanged (one valid groth16 proof per domain → `completed`).

## Step B2 — Per-round procedure (repeat for R = 1, 2, 3, 4)

### B2a — Reconfigure all 5 domains to round R (skip for round 1)

```bash
# On EACH domain EC2 (0..4) — always 'down' with --profile r4 so every
# verifier from the previous round is caught:
cd ~/zk-authaas
docker compose --profile r4 -f docker-compose.onprem-throughput.yml down

# Bring up round R (profile flag per round: R1 none, R2 --profile r2,
# R3 --profile r3, R4 --profile r4):
docker compose --env-file onprem-throughput-round<R>.env <PROFILE_FLAG> \
  -f docker-compose.onprem-throughput.yml up -d
sleep 5
docker compose --profile r4 -f docker-compose.onprem-throughput.yml ps
```

Expected running containers per domain: **5** (R1), **7** (R2), **10** (R3), **13** (R4) — that's 4 infra + 1/3/6/9 verifiers. The selector's startup log must show `snark-count` = 1/3/6/9 respectively.

### B2b — Shifting-focus run at the SAME KNEE_VU_R from Part A

```bash
# On k6 EC2 (inside tmux, after ulimit -n 65536)
k6 run \
  -e TARGETS=$DOMAIN0_IP,$DOMAIN1_IP,$DOMAIN2_IP,$DOMAIN3_IP,$DOMAIN4_IP \
  -e VUS=<KNEE_VU_R> \
  -e DURATION=100s \
  load_test_onprem.js \
  --out csv=test_results_onprem_thr_r<R>.csv
```

No sweep on this side — the on-prem system inherits the service system's knee VU, exactly as Track 3 inherited Track 2's VUS=200. That's the point: identical offered load, per round.

**What to expect:** the hot domain's throughput line should **flatten at its own verifier ceiling** (~23 / 69 / 138 / 207 c/s for 1/3/6/9 workers) while 60 % of traffic piles onto it — the queue grows and per-domain completions can't follow the offered load. The four cold domains sit far below their ceilings. The service system at the same VU keeps the hot domain near `0.6 × pool capacity` instead.

→ Loop back to B2a for the next round.

## Step B3 — Collect results and tear down

```powershell
cd "C:\Work\VSCodeRepo\ZK-AuthaaS-Simulation"
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_onprem_thr_r1.csv .
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_onprem_thr_r2.csv .
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_onprem_thr_r3.csv .
scp -i "zk-authaas-ec2-key.pem" ubuntu@<k6-public-ip>:~/test_results_onprem_thr_r4.csv .
```

Teardown: on each domain EC2 `docker compose --profile r4 -f docker-compose.onprem-throughput.yml down`, then **terminate all 6 instances**.

---

# Step C — Visualize

## C1 — Per-round per-domain throughput graphs (8 graphs, same style as the latency graphs)

```powershell
cd "C:\Work\VSCodeRepo\ZK-AuthaaS-Simulation"

# Service side
python visualize_k6_throughput_per_app.py --input test_results_e2e_thr_r1.csv    --output e2e_thr_r1_throughput_graph.png
python visualize_k6_throughput_per_app.py --input test_results_e2e_thr_r2.csv    --output e2e_thr_r2_throughput_graph.png
python visualize_k6_throughput_per_app.py --input test_results_e2e_thr_r3.csv    --output e2e_thr_r3_throughput_graph.png
python visualize_k6_throughput_per_app.py --input test_results_e2e_thr_r4.csv    --output e2e_thr_r4_throughput_graph.png

# On-prem side
python visualize_k6_throughput_per_app.py --input test_results_onprem_thr_r1.csv --output onprem_thr_r1_throughput_graph.png
python visualize_k6_throughput_per_app.py --input test_results_onprem_thr_r2.csv --output onprem_thr_r2_throughput_graph.png
python visualize_k6_throughput_per_app.py --input test_results_onprem_thr_r3.csv --output onprem_thr_r3_throughput_graph.png
python visualize_k6_throughput_per_app.py --input test_results_onprem_thr_r4.csv --output onprem_thr_r4_throughput_graph.png
```

Each graph: five coloured completions/s lines (one per domain), dashed focus-switch markers at 20/40/60/80 s, "Dom N hot" labels — the throughput twin of the Track 2/3 latency graph. The script also prints per-domain totals plus each domain's **hot-window** and **cold** average rates.

## C2 — The headline: throughput-vs-vCPU scaling curve

```powershell
python visualize_throughput_scaling.py `
  --budgets 10,20,40,60 `
  --e2e    test_results_e2e_thr_r1.csv,test_results_e2e_thr_r2.csv,test_results_e2e_thr_r3.csv,test_results_e2e_thr_r4.csv `
  --onprem test_results_onprem_thr_r1.csv,test_results_onprem_thr_r2.csv,test_results_onprem_thr_r3.csv,test_results_onprem_thr_r4.csv `
  --output throughput_scaling_graph.png
```

A single graph: **average (total system) throughput vs vCPU budget**. At each budget (10/20/40/60) there are two dots — Service (blue circle) and On-prem (red square), no connecting lines — so the head-to-head gap is readable per round. Throughput = all completed verifications across the 5 domains ÷ the run duration. Both systems grow with vCPU; the gap between the paired dots is the pooling dividend.

The script also prints a summary table that additionally reports each round's **hot-domain** throughput and the service/on-prem ratios (total and hot) — those ratios are the quotable scalability numbers, even though only total throughput is plotted.

---

## Expected results (rough targets, post-rebalance)

| Round | Budget | E2E total c/s¹ | E2E binding constraint | On-prem hot-domain ceiling² |
|---|---|---|---|---|
| 1 | 10 | ~210 | verifiers (5) | 1 × ~42 ≈ 42/s |
| 2 | 20 | ~410 | token pipeline (issuer+handler) | 3 × ~42 ≈ 126/s |
| 3 | 40 | ~880 | token pipeline | 6 × ~42 ≈ 252/s |
| 4 | 60 | ~1280 | token pipeline | 9 × ~42 ≈ 378/s |

¹ Empirical estimates after moving idle validator budget into the token-issuer. Verify each with the sweep + the A3b capacity check.
² On-prem has no token-issuer, so each domain is verifier-limited; the hot domain caps at its own 1/3/6/9 workers × ~42/s.

The thesis this test proves: under **skewed** (realistic) load the service architecture pools its entire verifier set against whichever domain is hot, so its **hot-domain throughput** keeps climbing with the budget, while on-prem strands 60 % of traffic on one domain's fraction of the workers. On raw *uniform* total throughput the service pays a token-issuance tax on-prem doesn't — so the headline lives in the **hot-domain** comparison and the shifting-focus runs, where pooling wins.

> ⚠️ **The round 2–4 env files were rebalanced (issuer-heavy) after the bottleneck analysis.** Any E2E sweep/run done *before* that rebalance (the old ~388/528/580 numbers) is stale — **re-sweep rounds 2, 3, and 4** with the current env files. Round 1 is verifier-limited and unchanged, so its existing sweep stands.

---

## Troubleshooting

**E2E throughput plateaus well below the verifier capacity (verifiers × ~42/s).**
The token pipeline is the bottleneck, not the verifiers. Run `docker stats` on the manager during load: in the rebalanced config the **token-issuer** (RS256 signing) and **request-handler** should both be near their caps — that's expected and is the service model's real ceiling. If only *one* is pinned and others are idle, shift budget toward it in that round's env file (keep the total fixed) and re-sweep. The token-issuer scales by **replica count** (signing is single-threaded per process), so add `ISSUER_REPLICAS`, not just `ISSUER_CPUS`.

**A validator pins only during one phase of the shifting-focus run.**
Each domain takes its turn as the hot domain (60 % of traffic), so a `VALIDATOR_CPUS` that's fine under uniform load can saturate during that domain's 20 s window. Raise `VALIDATOR_CPUS` for that round (it's applied to all five, since the hot role rotates).

**`docker stack deploy` warns about undefined variables / selector starts with snark-count=45 in round 1.**
The env file wasn't sourced into the deploying shell. Run `set -a; source e2e-throughput-round<R>.env; set +a` and redeploy. (Defaults in the compose file are round-4 values.)

**Submits accepted (HTTP 200) but 0% of jobs reach `completed`.**
The token-issuer is POSTing JWTs to `127.0.0.1` instead of your app validators, so no validator ever writes `status=completed`. Cause: `APP*_IP` were not set in the shell at `docker stack deploy` time, and the compose file silently falls back to `127.0.0.1`. Confirm with:
```bash
docker inspect $(docker ps -qf name=zk_token-issuer | head -1) --format '{{json .Args}}'
# look at the value after --app-urls
```
Fix: `source ~/app_ips.env` (created in Step A2), then redeploy with the round env file and FLUSHALL the queues. The Step A3a guard prevents this when you re-source `~/app_ips.env` first; the Step A3b single-job sanity check catches it before a full sweep.

**Selector dispatch rate caps around the same value across rounds 3 and 4.**
The selector is single-threaded; Track 1 demonstrated ~1800 jobs/s through the same v2 selector, well above round 4's ~1035/s — but if you see the dispatch-rate log plateau while `proof_queue` grows, raise `SELECTOR_CPUS` (it's the one component that doesn't scale by adding replicas).

**On-prem round shows the wrong container count.**
Profile flag mismatch — e.g. `up -d` with `--profile r3` but the round-2 env file. The profile decides how many verifier containers exist; `SNARK_COUNT` decides how many queues the selector feeds. Both must come from the same round. If they disagree, either jobs pile up on queues no verifier reads (count > containers) or verifiers idle (count < containers — budget silently unused).

**On-prem `down` leaves verifier containers running.**
`down` without a profile only stops profile-less services. Always tear down with `--profile r4`.

**Validator caps silently reset.**
Any `docker compose -f docker-compose.app.yml up` on an App EC2 recreates the container without the `docker update` cap. Re-apply the round's `VALIDATOR_CPUS` after any compose operation on an app.

**k6 freezes mid-run at high VU rounds.**
`ulimit -n 65536` in the same shell (inside tmux), every session. The c5.large k6 node handles round 4's ~1200–1800 sweep VUs fine (~1–2 MB/VU); if you sweep far beyond 2000 VUs, upgrade to c5.xlarge.

---

## Data flow

Identical to the companion tests — no pipeline changes, only budgets and the measured quantity differ:
- Service side: see "Data Flow Summary" in `AWS_E2E_Test_Setup_Checklist.md` (submit → selector → pooled verifiers → token-issuer → per-app validator → status write-back).
- On-prem side: see "Data Flow Summary (on-prem)" in `AWS_OnPrem_Test_Setup_Checklist.md` (submit to domain N → domain N's selector → domain N's verifiers only).

Throughput is read from the `completed_by_domain` counter both k6 scripts already emit (one sample per completed iteration, tagged `domain=N`).
