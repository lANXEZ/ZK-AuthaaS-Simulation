import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

// ==========================================
// ZK-AuthaaS Load Test
// ==========================================
// Works with the Swarm-deployed stack (both locally and on EC2).
//
// USAGE:
//   Local Swarm (defaults to localhost:8000):
//     k6 run load_test.js
//
//   Against an EC2 instance:
//     k6 run -e TARGET=172.31.45.12 load_test.js
//
//   Custom load profile:
//     k6 run -e VUS=100 -e ITERATIONS=2000 load_test.js
//
//   Pure SNARK / pure STARK / mixed:
//     k6 run -e STARK_RATIO=0.0 load_test.js   # all SNARK
//     k6 run -e STARK_RATIO=1.0 load_test.js   # all STARK
//     k6 run -e STARK_RATIO=0.5 load_test.js   # 50/50 mix (default)
//
//   With CSV output for visualize_k6.py:
//     k6 run -e TARGET=localhost load_test.js --out csv=test_results.csv
//
// SUGGESTED PROFILES:
//   Local laptop (10+10 verifiers):          VUS=50   ITERATIONS=500
//   c5.4xlarge spot (50+50 verifiers):       VUS=200  ITERATIONS=5000
//   c5.24xlarge spot (500+500, full run):    VUS=1000 ITERATIONS=50000
// ==========================================

// ------------------------------------------
// Configuration (override via env vars)
// ------------------------------------------
const TARGET_IP = __ENV.TARGET || 'localhost';
const PORT = __ENV.PORT || '8000';
const BASE_URL = `http://${TARGET_IP}:${PORT}`;

// Fraction of requests that should be STARK (0.0 = all SNARK, 1.0 = all STARK)
const STARK_RATIO = parseFloat(__ENV.STARK_RATIO || '0.5');

// Load profile
const VUS = parseInt(__ENV.VUS || '50');
const ITERATIONS = parseInt(__ENV.ITERATIONS || '500');
const MAX_DURATION = __ENV.MAX_DURATION || '10m';

// ------------------------------------------
// Custom metrics
// ------------------------------------------
const asyncVerificationTime = new Trend('async_verification_time');
const snarkVerificationTime = new Trend('snark_verification_time');
const starkVerificationTime = new Trend('stark_verification_time');
const failedVerifications = new Counter('failed_verifications');
const submitFailures = new Counter('submit_failures');

// ------------------------------------------
// k6 options
// ------------------------------------------
export const options = {
  scenarios: {
    batch_processing_test: {
      executor: 'shared-iterations',
      iterations: ITERATIONS,
      vus: VUS,
      maxDuration: MAX_DURATION,
    },
  },
  // Summary thresholds (optional - k6 marks run as failed if any breach)
  thresholds: {
    'async_verification_time': ['p(95)<30000'],  // 95% of requests finish in <30s
    'failed_verifications': ['count<10'],        // fewer than 10 failures overall
  },
};

// ------------------------------------------
// Main VU function
// ------------------------------------------
export default function () {
  // Jitter to stagger the initial TCP connection storm across VUs
  sleep(Math.random());

  // Randomize scheme per iteration so both verifier pools get exercised.
  // This is what actually stress-tests your verifier selector's routing.
  //const scheme = Math.random() < STARK_RATIO ? 'stark' : 'snark';
  const scheme = 'snark'; // For pure SNARK testing, uncomment this line and comment the above line.

  const submitUrl = `${BASE_URL}/verify/submit`;
  const payload = JSON.stringify({
    scheme: scheme,
    proof: 'zk_proof_data_here',
    public_inputs: ['input_1', 'input_2'],
  });
  const params = {
    headers: { 'Content-Type': 'application/json' },
  };

  // --------------------------------------
  // PHASE 1: Submit to the request handler
  // --------------------------------------
  const startTime = Date.now();
  const submitRes = http.post(submitUrl, payload, params);

  const submitOk = check(submitRes, {
    'accepted by queue': (r) => r.status === 202 || r.status === 200,
  });

  if (!submitOk) {
    submitFailures.add(1);
    return;
  }

  let jobId;
  try {
    jobId = submitRes.json('job_id');
  } catch (e) {
    submitFailures.add(1);
    return;
  }

  if (!jobId) {
    submitFailures.add(1);
    return;
  }

  // --------------------------------------
  // PHASE 2: Poll for status
  // --------------------------------------
  // Desynchronized polling (0.5s to 1.5s jitter) avoids synchronized
  // polling spikes that would smear the latency graph.
  let isDone = false;
  let attempts = 0;
  let finalStatus = '';
  const maxAttempts = 120;  // ~120s max wait

  while (!isDone && attempts < maxAttempts) {
    sleep(0.1 + Math.random() * 0.2);  // 0.1 to 0.3 seconds

    const checkUrl = `${BASE_URL}/verify/status/${jobId}`;
    const checkRes = http.get(checkUrl);

    try {
      finalStatus = checkRes.json('status');
    } catch (e) {
      finalStatus = 'error';
    }

    if (finalStatus === 'completed' || finalStatus === 'failed') {
      isDone = true;
    }
    attempts++;
  }

  // --------------------------------------
  // PHASE 3: Record metrics & validate
  // --------------------------------------
  const endTime = Date.now();
  const totalProcessingTime = endTime - startTime;

  if (finalStatus === 'completed') {
    asyncVerificationTime.add(totalProcessingTime);
    if (scheme === 'snark') {
      snarkVerificationTime.add(totalProcessingTime);
    } else {
      starkVerificationTime.add(totalProcessingTime);
    }
  } else {
    failedVerifications.add(1);
  }

  check(finalStatus, {
    'verification fully completed': (s) => s === 'completed',
  });
}
