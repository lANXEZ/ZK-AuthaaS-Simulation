import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

// ==========================================
// ZK-AuthaaS ON-PREM Load Test (5-domain direct routing)
// ==========================================
// Parallel to load_test.js, but each domain has its OWN request-handler:
// k6 picks a domain by phase weights, then POSTs directly to that domain's IP.
// No centralised service. No cross-domain pooling.
//
// USAGE:
//   # SNARK on-prem (default)
//   k6 run -e TARGETS=10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5 \
//          -e DURATION=100s load_test_onprem.js \
//          --out csv=test_results_onprem_snark.csv
//
//   # U-Prove on-prem (pass ALG=uprove; reads bundle from uprove_proof.json
//   # in the same directory)
//   k6 run -e TARGETS=10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5 \
//          -e DURATION=100s -e ALG=uprove load_test_onprem.js \
//          --out csv=test_results_onprem_uprove.csv
//
//   The TARGETS env var is a comma-separated list of the 5 domain IPs
//   (in domain-id order 0..4). Port defaults to 8000.
//
// Metrics are tagged with domain=N exactly like the service test, so
// visualize_k6_per_app.py produces a directly comparable per-domain graph.
// ==========================================

const PORT = __ENV.PORT || '8000';
const ALG  = (__ENV.ALG || 'snark').toLowerCase();
if (ALG !== 'snark' && ALG !== 'uprove') {
  throw new Error(`ALG must be 'snark' or 'uprove' (got '${ALG}').`);
}

const _targetsRaw = (__ENV.TARGETS || '').trim();
if (!_targetsRaw) {
  throw new Error('Set TARGETS env var to a comma-separated list of 5 domain IPs (domain 0..4).');
}
const TARGET_IPS = _targetsRaw.split(',').map(s => s.trim());
if (TARGET_IPS.length !== 5) {
  throw new Error(`TARGETS must contain exactly 5 IPs (got ${TARGET_IPS.length}).`);
}
const BASE_URLS = TARGET_IPS.map(ip => `http://${ip}:${PORT}`);

const VUS = parseInt(__ENV.VUS || '200');
const ITERATIONS = parseInt(__ENV.ITERATIONS || '3000');
const MAX_DURATION = __ENV.MAX_DURATION || '15m';
const DURATION = __ENV.DURATION || '';

// ------------------------------------------
// Distribution phases — same as the service test for direct comparison
// ------------------------------------------
const PHASES = [
  { duration: 20, weights: [60, 10, 10, 10, 10] },   // 0–20 s   domain 0 hot
  { duration: 20, weights: [10, 60, 10, 10, 10] },   // 20–40 s  domain 1 hot
  { duration: 20, weights: [10, 10, 60, 10, 10] },   // 40–60 s  domain 2 hot
  { duration: 20, weights: [10, 10, 10, 60, 10] },   // 60–80 s  domain 3 hot
  { duration: 20, weights: [10, 10, 10, 10, 60] },   // 80–100 s domain 4 hot
];

let _cum = 0;
const PHASE_TABLE = PHASES.map(p => { _cum += p.duration; return { endAt: _cum, weights: p.weights }; });

// ------------------------------------------
// Metrics (identical names to load_test.js so the visualiser works unchanged)
// ------------------------------------------
const e2eLatency = new Trend('e2e_latency', true);
const submitsByDomain = new Counter('submits_by_domain');
const completedByDomain = new Counter('completed_by_domain');
const failedByDomain = new Counter('failed_by_domain');
const submitFailures = new Counter('submit_failures');

// ------------------------------------------
// k6 scenario — same dual-mode dispatch as load_test.js
// ------------------------------------------
export const options = {
  scenarios: {
    onprem_test: DURATION
      ? {
          executor: 'constant-vus',
          vus: VUS,
          duration: DURATION,
        }
      : {
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

// U-Prove bundle is loaded from disk at init time (k6 requires open() to be
// at module top level — runtime open() inside default() is forbidden).
// File expected next to this script on the k6 EC2.
const UPROVE_BUNDLE = ALG === 'uprove' ? JSON.parse(open('./uprove_proof.json')) : null;

// Same valid groth16 proof as load_test.js
const SNARK_PROOF = {
  pi_a: [
    "16893334615242764580836222078829142520432756203770466604081032720388657032757",
    "5095606969395716303621702958471922376961618029789842152295821108717087682311",
    "1",
  ],
  pi_b: [
    [
      "13772398192624595577472662855811728500397412494267729711099372526485968374649",
      "15249941699599606024139723272508104548269790148997217612719623411267570558493",
    ],
    [
      "19735295879188043871505513529932228526631701925990878770250928234435443795397",
      "11046809327765151786114304454515091703284305019483922364766276175300463695885",
    ],
    ["1", "0"],
  ],
  pi_c: [
    "18536201733965390491456176988021021022761142364866628667452517360063595662975",
    "15291715715367874403418883228408929985980666544091293542955491873294267230352",
    "1",
  ],
  protocol: "groth16",
  curve: "bn128",
};

const SNARK_PUBLIC_SIGNALS = [
  "1120771572304984668855649788542860110303223894298952018121329196339919157573",
  "20197087425205130352574209034729275460185533126585197591053247747830393653846",
  "111222333",
  "444555666",
  "1764263975784332459809300572476310454427845461305579380554772042455913567929",
  "10988278040513707334400680073433620711051179041727267619401283491695328957763",
];

export function setup() {
  return { testStart: Date.now() };
}

function pickDomainId(testStart) {
  const elapsedSec = (Date.now() - testStart) / 1000;
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

export default function (data) {
  sleep(Math.random() * 0.5);

  const domainId = pickDomainId(data.testStart);
  const domainTag = { domain: String(domainId) };
  const baseUrl = BASE_URLS[domainId];      // ← the key difference: domain selects the WHOLE URL

  // domain_id in the payload is harmless (request-handler accepts it, ignores it in on-prem).
  // 'scheme' is just a routing label used by the selector — both algorithms set it
  // to 'snark' so they share the same queue plumbing. The worker container running
  // on each domain determines which algorithm actually verifies the proof.
  const payload = JSON.stringify({
    scheme: 'snark',
    proof: ALG === 'uprove' ? UPROVE_BUNDLE : SNARK_PROOF,
    public_inputs: ALG === 'uprove' ? [] : SNARK_PUBLIC_SIGNALS,
    domain_id: domainId,
  });
  const params = { headers: { 'Content-Type': 'application/json' } };

  const submitStart = Date.now();
  const submitRes = http.post(`${baseUrl}/verify/submit`, payload, params);
  check(submitRes, { 'accepted': (r) => r.status === 200 || r.status === 202 }, domainTag);

  if (submitRes.status !== 200 && submitRes.status !== 202) {
    submitFailures.add(1, domainTag);
    return;
  }

  let jobId;
  try { jobId = submitRes.json('job_id'); } catch (e) { submitFailures.add(1, domainTag); return; }
  if (!jobId) { submitFailures.add(1, domainTag); return; }

  submitsByDomain.add(1, domainTag);

  let finalStatus = '';
  const maxAttempts = 120;
  for (let attempts = 0; attempts < maxAttempts; attempts++) {
    sleep(0.1 + Math.random() * 0.2);
    const checkRes = http.get(`${baseUrl}/verify/status/${jobId}`);
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

  check(finalStatus, { 'verification completed': (s) => s === 'completed' }, domainTag);
}
