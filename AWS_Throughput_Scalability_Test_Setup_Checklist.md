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

| Round | Total budget | E2E verifier pool | On-prem verifiers/domain | Overhead budget | Expected verify ceiling¹ | Expected `KNEE_VU`² |
|---|---|---|---|---|---|---|
| 1 | 10 vCPU | 5 | 1 | 5.0 | ~115/s | ~70 |
| 2 | 20 vCPU | 15 | 3 | 5.0 | ~345/s | ~210 |
| 3 | 40 vCPU | 30 | 6 | 10.0 | ~690/s | ~420 |
| 4 | 60 vCPU | 45 | 9 | 15.0 | ~1035/s | ~630 |

¹ verifier count × ~23 verifies/s per 1.0-vCPU snarkjs worker (Track 1 measurement).
² rule of thumb ~14 VUs per verifier, extrapolated from Track 2 (32 workers → knee ≈ 450). Your sweep decides the real value.

The exact per-service CPU splits live in the 8 env files (`e2e-throughput-round{1..4}.env`, `onprem-throughput-round{1..4}.env`) — each file has its allocation table in a header comment. They are a starting proposal: if the Step A9b capacity check shows the plumbing saturating before the verifiers, rebalance that round's env file and re-run.

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
```

Apply the round's CPU cap to the 5 app validators (they run outside the Swarm stack, so the limit is applied directly to the running container — `VALIDATOR_CPUS` is in the same env file):

```bash
# From your laptop, for each App EC2 public IP:
for HOST in <app0-pub> <app1-pub> <app2-pub> <app3-pub> <app4-pub>; do
  ssh -i zk-authaas-ec2-key.pem ubuntu@$HOST \
    "docker update --cpus=0.4 \$(docker ps -qf name=token-validator) && \
     docker inspect -f 'validator NanoCpus: {{.HostConfig.NanoCpus}}' \$(docker ps -qf name=token-validator)"
done
# NanoCpus must print 400000000 (= 0.4 vCPU)
```

> ⚠️ `docker update` survives container restarts but is **reset by `docker compose up`** — if you ever re-run compose on an App EC2, re-apply the cap.

Then run the **Step 9 sanity check from `AWS_E2E_Test_Setup_Checklist.md`** unchanged (one valid proof per domain → all `completed`, every app sees its own traffic) and the **Step 11 smoke test** (`VUS=10 ITERATIONS=50`).

## Step A3 — Per-round procedure (repeat for R = 1, 2, 3, 4)

Work on the k6 EC2 inside `tmux`, with `ulimit -n 65536` set in that shell.

### A3a — Reconfigure to round R (skip for round 1 — already deployed)

```bash
# On Manager (APP0_IP..APP4_IP must still be exported in this shell):
cd ~/zk-authaas
set -a; source e2e-throughput-round<R>.env; set +a
docker stack deploy -c docker-compose.e2e-throughput.yml zk

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
```

```bash
# From laptop: re-apply validator caps with the round's VALIDATOR_CPUS
# (0.4 / 0.32 / 0.84 / 0.94 for rounds 1/2/3/4):
for HOST in <app0-pub> <app1-pub> <app2-pub> <app3-pub> <app4-pub>; do
  ssh -i zk-authaas-ec2-key.pem ubuntu@$HOST \
    "docker update --cpus=<VALIDATOR_CPUS> \$(docker ps -qf name=token-validator)"
done
```

### A3b — VU sweep → record KNEE_VU_R

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

Copy back and plot if you want the knee graph (`python visualize_sweep.py --input sweep_e2e_thr_r<R>.csv --output sweep_e2e_thr_r<R>_graph.png`), or read the knee straight off the CSV.

📝 **Record `KNEE_VU_R`** in this table as you go — Part B consumes it:

| Round | Budget | KNEE_VU | Peak throughput at knee |
|---|---|---|---|
| 1 | 10 | ______ | ______ /s |
| 2 | 20 | ______ | ______ /s |
| 3 | 40 | ______ | ______ /s |
| 4 | 60 | ______ | ______ /s |

> ✅ **Capacity check (do not skip):** the peak throughput at the knee should be roughly `verifier_count × 23/s` (≈ 115 / 345 / 690 / 1035). If it plateaus well below ~80 % of that, the verifiers are NOT the bottleneck — something in the overhead allocation (usually the request-handler) is saturating first, and the round's measurement would be invalid. Check `docker stats` on the manager during load: any management container pinned at exactly its `cpus` limit is the culprit. Rebalance that round's env file (shift CPU from an idle service to the pinned one), redeploy, and re-sweep. Rounds 2 and 3 have the tightest handler allocations — watch them closely.

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

Two panels, two lines each (Service vs On-prem):
- **Total system throughput vs budget** — both should grow with vCPU; the gap is the pooling dividend.
- **Hot-domain throughput vs budget** — the starkest contrast: during its 60 % window a service-side domain can draw on the whole pool, an on-prem domain only on its 1/3/6/9 local verifiers.

The script also prints a summary table with service/on-prem ratios per round — those ratios are the quotable scalability numbers.

---

## Expected results (rough targets)

| Round | Budget | E2E total c/s | On-prem total c/s | On-prem hot-domain ceiling |
|---|---|---|---|---|
| 1 | 10 | ~115 | < 115 (hot domain capped at ~23) | 1 × 23/s |
| 2 | 20 | ~345 | lower | 3 × 23 ≈ 69/s |
| 3 | 40 | ~690 | lower | 6 × 23 ≈ 138/s |
| 4 | 60 | ~1035 | lower | 9 × 23 ≈ 207/s |

The thesis this test proves: with **identical budgets and identical verifier cores**, the service architecture converts vCPU into usable throughput more efficiently under skewed (realistic) load — and the advantage persists or widens as the budget scales, because pooling lets the entire pool chase whichever domain is hot.

---

## Troubleshooting

**E2E knee throughput lands far below `verifiers × 23/s`.**
Overhead bottleneck — see the capacity check in A3b. `docker stats` on the manager; whichever container sits pinned at its `cpus` limit needs more budget in that round's env file. Take it from a service showing low utilisation, keep the round total constant, redeploy, re-sweep.

**`docker stack deploy` warns about undefined variables / selector starts with snark-count=45 in round 1.**
The env file wasn't sourced into the deploying shell. Run `set -a; source e2e-throughput-round<R>.env; set +a` and redeploy. (Defaults in the compose file are round-4 values.)

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
