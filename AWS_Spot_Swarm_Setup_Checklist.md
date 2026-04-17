# AWS EC2 Spot + Docker Swarm Setup Checklist

For the ZK-AuthaaS Simulation project. Target audience: student team on a tight budget. Assumes you already have an AWS account.

---

## 0. Before you start (one-time, do this once per team)

- Create an AWS account. Check for credits through **AWS Educate**, **GitHub Student Pack**, or your university's **AWS Academy** partnership — these often provide $100+ in credits.
- In the AWS Console, go to **Billing → Budgets** and set an alert at **$20** and another at **$50**. AWS will email you the moment you cross either.
- Create an **IAM user** for yourself (don't use the root account for daily work). Give it `AmazonEC2FullAccess` for this project.
- Install the AWS CLI locally (`aws configure`) and generate an **SSH key pair** in your target region (EC2 → Key Pairs → Create). Download the `.pem` file and `chmod 400` it.
- Pick **one region** and stick with it. `us-east-1` is usually cheapest. Record it.

---

## 1. Launch a spot instance (do this at the start of each experiment session)

1. EC2 Console → **Launch Instance** → switch to **Spot** request under Advanced Details.
2. **AMI**: Amazon Linux 2023 or Ubuntu 22.04 (either works).
3. **Instance type**:
   - Phase 2 (50+50 verifiers, development): `c5.4xlarge` (16 vCPU, 32GB) — spot ~$0.15–0.25/hr
   - Phase 3 (500+500 verifiers, full experiments): `c5.24xlarge` (96 vCPU, 192GB) — spot ~$1.00–1.80/hr
4. **Key pair**: select the one you generated in step 0.
5. **Security group**: create a new one named `zk-authaas-sg` with these inbound rules:
   - SSH (port 22) from **My IP** only
   - Custom TCP (port 8000) from **My IP** only — this is for k6 hitting the API
   - Leave all other ports closed
6. **Storage**: 30GB gp3 is plenty.
7. **Spot request type**: One-time (not persistent) — simpler for short experiments.
8. Launch. Wait for the instance to reach "running" and note the **Public IPv4**.

---

## 2. Connect and install Docker

```bash
ssh -i your-key.pem ec2-user@<public-ip>       # Amazon Linux 2023
# or ubuntu@<public-ip> for Ubuntu

# Amazon Linux 2023:
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
exit      # then SSH back in so the group change takes effect
```

Verify: `docker ps` should work without sudo.

---

## 3. Get your code onto the instance

```bash
git clone <your-repo-url> zk-authaas
cd zk-authaas
```

If the repo is private, either push to a public fork temporarily, use `scp`, or set up a deploy key.

---

## 4. Initialize Swarm and adjust the compose file

```bash
docker swarm init
```

Modify `docker-compose.yml` so the verifier services use Swarm's `deploy.replicas` instead of duplicated service blocks. Example snippet:

```yaml
snark_verifier:
  image: zk/snark_verifier:latest
  command: ["python", "SNARKVerifierWorker.py", "--redis-host", "snark-queue", "--index", "{{.Task.Slot}}"]
  deploy:
    replicas: 50          # bump to 500 for Phase 3
    resources:
      limits:
        cpus: '0.15'
        memory: 200M
```

Two important notes:
- Swarm uses `docker stack deploy`, which ignores `build:` directives — build images first with `docker compose build` and push to a local registry, or change `image:` to a pre-built one.
- `{{.Task.Slot}}` gives each replica a unique index (1..N), which you can pass to your worker's `--index` argument.

---

## 5. Deploy

```bash
docker compose build          # build images locally first
docker stack deploy -c docker-compose.yml zk
docker service ls             # confirm all services are running
docker service logs zk_request_handler -f   # tail logs to check for errors
```

To scale up/down on the fly:
```bash
docker service scale zk_snark_verifier=500 zk_stark_verifier=500
```

---

## 6. Run the load test

Edit `load_test.js` on your **local machine** (not the EC2):
```js
const TARGET_IP = '<EC2 public IP>';
```

Then from your laptop:
```bash
k6 run load_test.js --out csv=test_results.csv
```

Copy results back locally if the test ran on the instance:
```bash
scp -i your-key.pem ec2-user@<ip>:~/zk-authaas/test_results.csv ./
python visualize_k6.py
```

---

## 7. TEAR DOWN (do this every single time, no exceptions)

```bash
docker stack rm zk
exit
```

Then in the AWS Console:
- EC2 → Instances → select your instance → **Instance State → Terminate**

Terminating (not just stopping) a spot instance costs you nothing further. A stopped spot instance might still incur EBS storage fees.

Double-check the EC2 dashboard shows zero running instances before you close the tab. This is the single most important habit for keeping your bill near zero.

---

## Cost tracking cheatsheet

| Session type              | Instance        | Duration | Approx cost |
|---------------------------|-----------------|----------|-------------|
| Dev validation (50+50)    | c5.4xlarge spot | 2 hrs    | ~$0.40      |
| Full experiment (500+500) | c5.24xlarge spot| 2 hrs    | ~$3–6       |
| Full paper (5–10 runs)    | c5.24xlarge spot| 15 hrs   | ~$25–50     |

Everything local (laptop docker-compose with 10+10) is free, and you should iterate there as much as possible before paying for cloud compute.

---

## Common pitfalls

- **Spot interruption mid-experiment**: happens occasionally. You get a 2-min warning. Just restart and re-run; it's not a disaster for short experiments.
- **"Cannot connect to Docker daemon"**: you forgot to log out and back in after `usermod -aG docker`.
- **k6 getting connection refused**: security group doesn't allow your IP on port 8000, or the request handler container isn't up yet.
- **Swarm services stuck "pending"**: usually means CPU/memory overcommit. Check `docker service ps <service>` and reduce `replicas` or resource limits.
- **Forgot to terminate for a weekend**: a `c5.24xlarge` on-demand (not spot) for 48 hours is ~$200. This is why the billing alert matters.

---

## One-time-per-team optional upgrade

If you find yourselves doing many experiments, write a small **teardown script** that your team runs at the end of every session:

```bash
#!/bin/bash
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=zk-authaas" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
aws ec2 terminate-instances --instance-ids $INSTANCE_ID
```

Tag your instance with `Project=zk-authaas` when launching and this will clean everything up with one command.
