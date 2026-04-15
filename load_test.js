import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics'; // Imported to track total async time

// 1. Define a Custom Metric
// This will appear in your CSV exports and terminal output, giving you the true processing latency
const asyncVerificationTime = new Trend('async_verification_time');

// 2. Define the load (VUs = Virtual Users)
export const options = {
  stages: [
    { duration: '2s', target: 250 },  // Step 1: Baseline load
    { duration: '1m', target: 250 },  // Step 2: Medium load
    { duration: '0s', target: 0 },    // Cooldown
  ],
};

// 3. What each user does
export default function () {
  // NOTE: You must update these URLs to match your actual backend routing
  const submitUrl = 'http://localhost:8000/verify/submit'; 

  // The payload expected by your worker
  const payload = JSON.stringify({
    scheme: "snark", // Fixed to 'snark' for this test run
    proof: "zk_proof_data_here",
    public_inputs: ["input_1", "input_2"]
  });

  const params = {
    headers: { 'Content-Type': 'application/json' }
  };

  // ==========================================
  // PHASE 1: SUBMIT TO REDIS QUEUE
  // ==========================================
  const startTime = Date.now(); // Start the stopwatch
  
  const submitRes = http.post(submitUrl, payload, params);
  
  // Check if the server accepted the payload into the queue (often a 202 or 200 status)
  check(submitRes, { 'accepted by queue': (r) => r.status === 202 || r.status === 200 });
  
  // Extract the tracking ID. 
  // NOTE: Change 'job_id' to whatever key your API actually returns.
  let jobId;
  try {
    jobId = submitRes.json('job_id');
  } catch (e) {
    // If the server fails under load and doesn't return JSON, exit this iteration safely
    return; 
  }

  // ==========================================
  // PHASE 2: POLLING LOOP (Check the status)
  // ==========================================
  let isDone = false;
  let attempts = 0;
  let finalStatus = '';
  const maxAttempts = 30; // Max 60 seconds of waiting (30 attempts * 2s sleep)

  while (!isDone && attempts < maxAttempts) {
    sleep(2); // Crucial: Wait 2 seconds so we don't DDoS our own backend
    
    // NOTE: Update this URL to match your status checking endpoint
    const checkUrl = `http://localhost:8000/verify/status/${jobId}`;
    const checkRes = http.get(checkUrl);
    
    try {
      finalStatus = checkRes.json('status'); // Expected to return 'pending', 'processing', 'completed', or 'failed'
    } catch (e) {
       finalStatus = 'error'; // Catch server timeouts during heavy load
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

  // Only record the time if the ZK math actually finished successfully
  if (finalStatus === 'completed') {
    asyncVerificationTime.add(totalProcessingTime); // Add to our custom metric!
  }

  // Check if the worker successfully verified it before giving up
  check(finalStatus, { 
    'verification fully completed': (s) => s === 'completed' 
  });
}