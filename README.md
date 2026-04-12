## ZK-AuthaaS Simulation: Testing Guide

This project simulates a scalable zero-knowledge proof verification system using Docker Compose. Follow these steps to build, run, test, and monitor the system.

### 1. Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (required)
- [Node.js](https://nodejs.org/) (for k6 load testing)
- [k6](https://k6.io/) (install via `npm install -g k6` or from their website)

### 2. Build and Start All Services

Open a terminal in this project directory and run:

```
docker-compose up --build
```

This will start:
- Redis queues for proof, SNARK, and STARK
- The request handler API (FastAPI, port 8000)
- The verifier selector
- 10 SNARK and 10 STARK verifier workers (each with their own Redis queue)

You can scale the number of verifiers by editing `docker-compose.yml` and adjusting the `--snark-count` and `--stark-count` arguments and duplicating the relevant service blocks.

### 3. Run the Load Test

In a separate terminal, run:

```
k6 run load_test.js
```

This will send a high volume of requests to the API at `http://localhost:8000/verify`.

### 4. Monitor the Queues

You can check the length of each queue to observe system load and processing:

**Snapshot (one-time):**

```
docker exec -it proof-queue redis-cli LLEN proof_queue
docker exec -it snark-queue-1 redis-cli LLEN snark_queue
docker exec -it stark-queue-1 redis-cli LLEN stark_queue
```

**Real-time (repeat every second):**

```
docker exec -it proof-queue redis-cli -r -1 -i 1 LLEN proof_queue
docker exec -it snark-queue-1 redis-cli -r -1 -i 1 LLEN snark_queue
docker exec -it snark-queue-2 redis-cli -r -1 -i 1 LLEN snark_queue
... (repeat for all snark-queue-N)
docker exec -it stark-queue-1 redis-cli -r -1 -i 1 LLEN stark_queue
docker exec -it stark-queue-2 redis-cli -r -1 -i 1 LLEN stark_queue
... (repeat for all stark-queue-N)
```

### 5. Stopping the System

To stop all containers:

```
docker-compose down
```

### 6. Notes

- All code and service logic is in Python. See the respective `.py` files for details.
- You can modify the load test in `load_test.js` to change request patterns or payloads.
- For troubleshooting, check logs with `docker-compose logs -f`.