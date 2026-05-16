import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

// ==========================================
// ZK-AuthaaS E2E Load Test (5-domain routing)
// ==========================================
// Distributes requests across 5 apps by domainID (0..4) with phase-based
// weights. Each phase has its own duration and weight vector — change
// weights mid-test by editing the PHASES array below.
//
// USAGE:
//   k6 run -e TARGET=<manager-ip> load_test.js
//   k6 run -e TARGET=<manager-ip> -e VUS=200 -e ITERATIONS=20000 load_test.js
//   k6 run -e TARGET=<manager-ip> load_test.js --out csv=test_results_e2e.csv
//
// PER-DOMAIN METRICS:
//   Every metric is tagged with domain=<0..4>. The CSV output's `extra_tags`
//   column contains `domain=N` so visualize_k6_per_app.py can group results.
// ==========================================

const TARGET_IP = __ENV.TARGET || 'localhost';
const PORT = __ENV.PORT || '8000';
const BASE_URL = `http://${TARGET_IP}:${PORT}`;

const VUS = parseInt(__ENV.VUS || '300');
const ITERATIONS = parseInt(__ENV.ITERATIONS || '3000');
const MAX_DURATION = __ENV.MAX_DURATION || '15m';

// ------------------------------------------
// Distribution phases — each domain gets 20 s in the spotlight.
// Hot domain receives 60 % of traffic; the other four share 10 % each (sums to 100 %).
// Each phase: { duration: seconds, weights: [domain0, domain1, domain2, domain3, domain4] }
// After all phases elapse the last phase's weights are kept until the test ends.
// ------------------------------------------
const PHASES = [
  { duration: 20, weights: [60, 10, 10, 10, 10] },   // 0–20 s   domain 0 hot
  { duration: 20, weights: [10, 60, 10, 10, 10] },   // 20–40 s  domain 1 hot
  { duration: 20, weights: [10, 10, 60, 10, 10] },   // 40–60 s  domain 2 hot
  { duration: 20, weights: [10, 10, 10, 60, 10] },   // 60–80 s  domain 3 hot
  { duration: 20, weights: [10, 10, 10, 10, 60] },   // 80–100 s domain 4 hot
];

// Precompute cumulative end times for fast lookup
let _cum = 0;
const PHASE_TABLE = PHASES.map(p => { _cum += p.duration; return { endAt: _cum, weights: p.weights }; });

// ------------------------------------------
// Custom metrics (all tagged with domain=N)
// ------------------------------------------
const e2eLatency = new Trend('e2e_latency', true);
const submitsByDomain = new Counter('submits_by_domain');
const completedByDomain = new Counter('completed_by_domain');
const failedByDomain = new Counter('failed_by_domain');
const submitFailures = new Counter('submit_failures');

// ------------------------------------------
// k6 scenario
// ------------------------------------------
export const options = {
  scenarios: {
    e2e_test: {
      executor: 'shared-iterations',
      iterations: ITERATIONS,
      vus: VUS,
      maxDuration: MAX_DURATION,
    },
  },
  thresholds: {
    'e2e_latency': ['p(95)<30000'],
    'submit_failures': ['count<10'],
  },
};

// ------------------------------------------
// SNARK proof (reused every request)
// ------------------------------------------
const SNARK_PROOF = {
  pi_a: [
    "16893334615242764580836222078829142520432756203770466604081032720388657032757",
    "5095606969395716303621702958471922376961618029789842152295821108717087682311",
    "1"
  ],
  pi_b: [
    [
      "13772398192624595577472662855811728500397412494267729711099372526485968374649",
      "15249941699599606024139723272508104548269790148997217612719623411267570558493"
    ],
    [
      "19735295879188043871505513529932228526631701925990878770250928234435443795397",
      "11046809327765151786114304454515091703284305019483922364766276175300463695885"
    ],
    ["1", "0"]
  ],
  pi_c: [
    "18536201733965390491456176988021021022761142364866628667452517360063595662975",
    "15291715715367874403418883228408929985980666544091293542955491873294267230352",
    "1"
  ],
  protocol: "groth16",
  curve: "bn128"
};

const SNARK_PUBLIC_SIGNALS = [
  "1120771572304984668855649788542860110303223894298952018121329196339919157573",
  "20197087425205130352574209034729275460185533126585197591053247747830393653846",
  "111222333",
  "444555666",
  "1764263975784332459809300572476310454427845461305579380554772042455913567929",
  "10988278040513707334400680073433620711051179041727267619401283491695328957763"
];

// ------------------------------------------
// Test start timestamp shared across VUs
// ------------------------------------------
export function setup() {
  return { testStart: Date.now() };
}

// ------------------------------------------
// Phase + weighted-domain picker
// ------------------------------------------
function pickDomainId(testStart) {
  const elapsedSec = (Date.now() - testStart) / 1000;
  // Find first phase whose endAt > elapsed; fall back to last
  let phase = PHASE_TABLE[PHASE_TABLE.length - 1];
  for (let i = 0; i < PHASE_TABLE.length; i++) {
    if (elapsedSec < PHASE_TABLE[i].endAt) { phase = PHASE_TABLE[i]; break; }
  }
  const weights = phase.weights;
  const total = weights[0] + weights[1] + weights[2] + weights[3] + weights[4];
  let r = Math.random() * total;
  for (let i = 0; i < 5; i++) {
    r -= weights[i];
    if (r < 0) return i;
  }
  return 4;
}

// ------------------------------------------
// Main VU function
// ------------------------------------------
export default function (data) {
  sleep(Math.random() * 0.5);

  const domainId = pickDomainId(data.testStart);
  const domainTag = { domain: String(domainId) };

  const payload = JSON.stringify({
    scheme: 'snark',
    proof: SNARK_PROOF,
    public_inputs: SNARK_PUBLIC_SIGNALS,
    domain_id: domainId,
  });
  const params = { headers: { 'Content-Type': 'application/json' } };

  const submitStart = Date.now();
  const submitRes = http.post(`${BASE_URL}/verify/submit`, payload, params);

  if (!check(submitRes, { 'accepted': (r) => r.status === 200 || r.status === 202 })) {
    submitFailures.add(1, domainTag);
    return;
  }

  let jobId;
  try { jobId = submitRes.json('job_id'); } catch (e) { submitFailures.add(1, domainTag); return; }
  if (!jobId) { submitFailures.add(1, domainTag); return; }

  submitsByDomain.add(1, domainTag);

  // Poll for completion
  let finalStatus = '';
  const maxAttempts = 120;
  for (let attempts = 0; attempts < maxAttempts; attempts++) {
    sleep(0.1 + Math.random() * 0.2);
    const checkRes = http.get(`${BASE_URL}/verify/status/${jobId}`);
    try { finalStatus = checkRes.json('status'); } catch (e) { finalStatus = 'error'; }
    if (finalStatus === 'completed' || finalStatus === 'failed') break;
  }

  const totalMs = Date.now() - submitStart;

  if (finalStatus === 'completed') {
    e2eLatency.add(totalMs, domainTag);
    completedByDomain.add(1, domainTag);
  } else {
    failedByDomain.add(1, domainTag);
  }

  check(finalStatus, { 'verification completed': (s) => s === 'completed' });
}
