# ZK-AuthaaS Simulation

A distributed zero-knowledge proof verification simulator built to study load-balancing algorithms across heterogeneous verifier pools. The system is composed of a FastAPI request handler, a custom verifier selector that routes proofs by scheme + weighted cost, and pools of SNARK and STARK verifier workers connected through Redis queues. Load is driven by k6.

This project is deployed with **Docker Swarm** so that verifier counts can be scaled with a single command from 10 up to 1000 replicas without editing the compose file.

---

## 1. Prerequisites

Install the following on the machine that will run the stack (your laptop for local testing, or an EC2 instance for cloud tests):

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows / macOS) or Docker Engine (Linux). Swarm mode is included.
- [k6](https://k6.io/docs/getting-started/installation/) for load generation.
- [Python 3.8+](https://www.python.org/) for the throughput sweep and visualization scripts.
- Python packages: `pip install pandas matplotlib numpy`

On Windows PowerShell the monitoring script also requires loosening the execution policy once:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 2. Architecture

```
 ┌─────────┐   POST /verify/submit   ┌─────────────────┐
 │   k6    │ ──────────────────────► │ request-handler │
 └─────────┘                         │   (FastAPI)     │
      ▲                              └────────┬────────┘
      │ poll /verify/status                   │ lpush proof_queue
      │                                       ▼
      │                              ┌─────────────────┐
      │                              │   proof-queue   │  (Redis :6379)
      │                              └────────┬────────┘
      │                                       │ brpop proof_queue
      │                                       ▼
      │                              ┌─────────────────┐
      │                              │ verifier-       │  cost-weighted least-queue
      │                              │   selector      │  + pub/sub feedback
      │                              └────────┬────────┘
      │                         lpush snark_queue:{i}   lpush stark_queue:{i}
      │                                       ▼                     ▼
      │                              ┌───────────────┐   ┌───────────────┐
      │                              │  snark-queue  │   │  stark-queue  │
      │                              │ (Redis :6380) │   │ (Redis :6381) │
      │                              └───────┬───────┘   └───────┬───────┘
      │          brpop snark_queue:{i}       │                   │  brpop stark_queue:{i}
      │                                      ▼                   ▼
      │                              ┌──────────────┐   ┌──────────────┐
      │                              │ snark-verifier│   │ stark-verifier│
      │                              │  (N replicas) │   │  (N replicas) │
      │                              └──────┬───────┘   └──────┬───────┘
      │                                     │                  │
      │                                     └────── publish ───┴── verifier_feedback
      │                                                        │
      └──────── set status:{job_id} ───────────────────────────┘
```

A single Redis per verifier type hosts one list per node (key `snark_queue:{index}` / `stark_queue:{index}`). Each worker reads only its own key, which gives the selector fine-grained routing control while keeping the number of Redis processes small (3 total instead of one per node).

---

## 3. Build and Deploy

Docker Swarm handles the orchestration. The same commands work locally (for development) and on an EC2 instance (for experiments).

```
docker swarm init
docker compose build                              # build all service images
docker stack deploy -c docker-compose.yml zk      # deploy the stack as "zk"
docker service ls                                 # wait for all services to reach target replicas
```

Expected output of `docker service ls` at default scale:

```
NAME                      MODE         REPLICAS   IMAGE
zk_proof-queue            replicated   1/1        redis:latest
zk_snark-queue            replicated   1/1        redis:latest
zk_stark-queue            replicated   1/1        redis:latest
zk_request-handler        replicated   1/1        zk-authaas/request-handler:latest
zk_verifier-selector      replicated   1/1        zk-authaas/verifier-selector:latest
zk_snark-verifier         replicated   10/10      zk-authaas/snark-verifier:latest
zk_stark-verifier         replicated   10/10      zk-authaas/stark-verifier:latest
```

The API becomes available at `http://localhost:8000/verify/submit` once `zk_request-handler` is up.

### Scaling on the fly

The number of verifiers is controlled by `deploy.replicas` in `docker-compose.yml`, but you can also change it at runtime:

```
docker service scale zk_snark-verifier=50 zk_stark-verifier=50
```

Important: the selector is started with `--snark-count` and `--stark-count` arguments that must match the replica counts. If you change replicas on the fly, also restart the selector with the matching counts, or edit `docker-compose.yml` and redeploy.

---

## 4. Run the Load Test

In a separate terminal (from your laptop):

```
k6 run load_test.js
```

Defaults are suitable for local testing against 10+10 verifiers. Configuration is via environment variables — no need to edit the file:

```
k6 run -e TARGET=localhost -e VUS=200 -e ITERATIONS=2000 load_test.js
k6 run -e TARGET=172.31.45.12 -e VUS=1000 -e ITERATIONS=50000 load_test.js   # EC2
k6 run -e STARK_RATIO=0.0 load_test.js                                       # all SNARK
k6 run -e STARK_RATIO=1.0 load_test.js                                       # all STARK
k6 run -e STARK_RATIO=0.5 load_test.js                                       # 50/50 mix
```

Suggested VU counts per deployment:

| Deployment                           | Verifiers   | VUS   | ITERATIONS |
|--------------------------------------|-------------|-------|------------|
| Local laptop                         | 10 + 10     | 50    | 500        |
| EC2 `c5.4xlarge` (spot)              | 50 + 50     | 200   | 5 000      |
| EC2 `c5.24xlarge` (spot, full run)   | 500 + 500   | 1 000 | 50 000     |

For CSV output suitable for `visualize_k6.py`:

```
k6 run load_test.js --out csv=test_results.csv
```

---

## 5. Monitor the Queues Live

Use the included PowerShell script to watch all queues in real time with color-coded depth:

```powershell
.\monitor_queues.ps1
.\monitor_queues.ps1 -SnarkCount 50 -StarkCount 50 -Refresh 2
```

It displays the incoming `proof_queue` and every `snark_queue:{i}` / `stark_queue:{i}` alongside its depth. Queues at 0 appear gray, low depth yellow, high depth red. This is the fastest way to visually confirm your selector is distributing evenly across nodes.

For a single-queue one-shot check:

```powershell
docker exec $(docker ps -qf "name=zk_snark-queue" | Select-Object -First 1) redis-cli LLEN snark_queue:3
```

---

## 6. Find Your Maximum Throughput (Sweep)

`sweep_throughput.py` runs k6 at a series of VU levels and records per-run metrics (throughput, p50/p95/p99 latency, failures) to `sweep_results.csv`:

```
python sweep_throughput.py                     # default local sweep, all SNARK
python sweep_throughput.py --stark-ratio 0.5   # 50/50 mix
python sweep_throughput.py --vus 50,100,200,400,800 --clean
python sweep_throughput.py --target 172.31.45.12    # remote EC2
```

The "knee" in the resulting curve — the VU level where `throughput_req_per_sec` stops growing — is the optimal operating point. Beyond the knee, latency grows without throughput gain.

To visualize the sweep results:

```
python visualize_sweep.py
```

This reads `sweep_results.csv` and produces `sweep_throughput_graph.png` with two stacked panels. The top panel shows throughput on the left y-axis and p50/p95/p99 latency on the right y-axis — the peak-throughput point is annotated. The bottom panel shows verification and submit failure counts at each VU level. The knee is the VU count where throughput peaks and latency starts climbing without further throughput gain.

---

## 7. Visualize a Single Load-Test Run

After a load test that produced CSV output (i.e. run with `--out csv=test_results.csv`):

```
python visualize_k6.py
```

This reads `test_results.csv` and writes `k6_performance_graph.png` with RPS and latency curves over time. Use this for analyzing the *shape* of a single run; use `visualize_sweep.py` for comparing multiple VU levels.

---

## 8. Tear Down

```
docker stack rm zk
```

To exit Swarm mode entirely (if you don't plan to redeploy):

```
docker swarm leave --force
```

---

## 9. Moving to AWS

Once the stack runs correctly on your laptop, follow [`AWS_Spot_Swarm_Setup_Checklist.md`](./AWS_Spot_Swarm_Setup_Checklist.md) for the full EC2 spot instance workflow, cost budgeting, and teardown discipline. The compose file, source code, and load test all work unchanged on EC2 — only the `TARGET` of the k6 test changes.

---

## 10. File Reference

| File                               | Role                                                                 |
|------------------------------------|----------------------------------------------------------------------|
| `docker-compose.yml`               | Unified Swarm-compatible stack definition                            |
| `requestHandler.py`                | FastAPI service exposing `/verify/submit` and `/verify/status/{id}`  |
| `verifierSelector.py`              | Cost-weighted least-queue router with pub/sub feedback               |
| `SNARKVerifierWorker.py`           | SNARK worker; listens on `snark_queue:{index}`                       |
| `STARKVerifierWorker.py`           | STARK worker; listens on `stark_queue:{index}`                       |
| `Dockerfile.{requesthandler,selector,snark,stark}` | Image builds for each service                        |
| `load_test.js`                     | k6 load driver                                                       |
| `sweep_throughput.py`              | Automated VU sweep for throughput analysis                           |
| `visualize_sweep.py`               | Plots throughput-vs-VUs knee graph from `sweep_results.csv`          |
| `monitor_queues.ps1`               | Real-time queue depth monitor (Windows PowerShell)                   |
| `visualize_k6.py`                  | Plots RPS + latency over time from a k6 CSV                          |
| `AWS_Spot_Swarm_Setup_Checklist.md`| Step-by-step AWS EC2 spot deployment guide                           |

---

## 11. Troubleshooting

**Services stuck in `0/1`.** Usually CPU or memory overcommit, or an image that failed to build. Check with `docker service ps zk_<service-name> --no-trunc` to see the task failure reason.

**`command not found: watch` on Windows.** That's a Linux command — use `monitor_queues.ps1` instead.

**k6 reports many `submit_failures`.** The FastAPI request-handler is saturated. Scale it up: `docker service scale zk_request-handler=3`.

**Queues stay full and never drain.** Either no verifier replicas are running (check `docker service ls`) or the selector's `--snark-count` / `--stark-count` don't match the replica counts so some queues receive jobs no worker is listening on.

**`docker stack rm` leaves networks behind.** Wait ~15 seconds before redeploying; Swarm tears networks down asynchronously.

**Throughput plateaus far below theoretical ceiling.** Check `monitor_queues.ps1` — if queues are empty, the bottleneck is upstream (request-handler, selector, or k6's own CPU); if queues are full, the bottleneck is the verifier pool and you need more replicas.
