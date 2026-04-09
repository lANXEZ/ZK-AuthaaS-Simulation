import http from 'k6/http';
import { check, sleep } from 'k6';

// 1. Define the load (VUs = Virtual Users) [cite: 13]
export const options = {
  stages: [
    { duration: '2m', target: 100 }, // Ramp up to 100 users [cite: 16, 84]
    { duration: '5m', target: 100 }, // Stay at 100 users [cite: 17, 84]
    { duration: '2m', target: 0 },   // Ramp down to 0 users [cite: 18]
  ],
};

// 2. What each user does [cite: 21, 22]
export default function () {
  // Point k6 to your local API [cite: 81]
  const url = 'http://localhost:8000/verify'; 
  
  // The payload expected by your worker [cite: 25, 27, 28]
  const payload = JSON.stringify({
    scheme: "snark", 
    proof: "zk_proof_data_here",
    public_inputs: ["input_1", "input_2"]
  });

  const params = {
    headers: { 'Content-Type': 'application/json' }
  };

  const res = http.post(url, payload, params);
  
  // Check if the server responded successfully [cite: 32]
  check(res, { 'status is 200': (r) => r.status === 200 });
  sleep(1); // Wait 1 second before the next request [cite: 34]
}