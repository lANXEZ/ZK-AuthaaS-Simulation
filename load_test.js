import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics'; 

const asyncVerificationTime = new Trend('async_verification_time');

// ==========================================
// AWS DEPLOYMENT CONFIGURATION
// ==========================================
// REPLACE THIS STRING with the Private IPv4 address of your Backend EC2 instance.
// e.g., '172.31.45.12'
const TARGET_IP = 'YOUR_EC2_PRIVATE_IP'; 
const BASE_URL = `http://${TARGET_IP}:8000`;

export const options = {
  scenarios: {
    batch_processing_test: {
      executor: 'shared-iterations',
      iterations: 1000, 
      vus: 200, 
      maxDuration: '10m', 
    },
  },
};

export default function () {
  // FIX 1: Jitter. Staggers the 200 VUs starting exactly at the same millisecond
  // This prevents the OS from refusing the massive initial TCP connection spike.
  sleep(Math.random());

  const submitUrl = `${BASE_URL}/verify/submit`; 

  const payload = JSON.stringify({
    scheme: "snark", 
    proof: "zk_proof_data_here",
    public_inputs: ["input_1", "input_2"]
  });

  const params = {
    headers: { 'Content-Type': 'application/json' }
  };

  // ==========================================
  // PHASE 1: SUBMIT TO REDIS QUEUE
  // ==========================================
  const startTime = Date.now(); 
  
  const submitRes = http.post(submitUrl, payload, params);
  
  check(submitRes, { 'accepted by queue': (r) => r.status === 202 || r.status === 200 });
  
  let jobId;
  try {
    jobId = submitRes.json('job_id');
  } catch (e) {
    return; 
  }

  // ==========================================
  // PHASE 2: POLLING LOOP (Check the status)
  // ==========================================
  let isDone = false;
  let attempts = 0;
  let finalStatus = '';
  
  // FIX 2: Increased max attempts because our sleep time is shorter now.
  // 120 attempts * ~1 average second = ~120 seconds of maximum waiting.
  const maxAttempts = 120; 

  while (!isDone && attempts < maxAttempts) {
    // FIX 3: Desynchronized Polling to fix the broken graph lines!
    // Sleeps for a random amount of time between 0.5s and 1.5s
    sleep(0.5 + Math.random()); 
    
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

  // ==========================================
  // PHASE 3: METRICS & VALIDATION
  // ==========================================
  const endTime = Date.now();
  const totalProcessingTime = endTime - startTime;

  if (finalStatus === 'completed') {
    asyncVerificationTime.add(totalProcessingTime); 
  }

  check(finalStatus, { 
    'verification fully completed': (s) => s === 'completed' 
  });
}