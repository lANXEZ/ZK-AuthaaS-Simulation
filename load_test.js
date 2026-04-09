import http from 'k6/http';
import { check, sleep } from 'k6';

// 1. Define the load (VUs = Virtual Users) [cite: 13]
export const options = {
  stages: [
    { duration: '0m', target: 30 }, // Ramp up to 30 users [cite: 16, 84]
    { duration: '5m', target: 30 }, // Stay at 30 users [cite: 17, 84]
    { duration: '0m', target: 0 },   // Ramp down to 0 users [cite: 18]
  ],
};

// 2. What each user does [cite: 21, 22]
export default function () {
  // Point k6 to your local API [cite: 81]
  const url = 'http://localhost:8000/verify'; 

  // Randomly choose between 'snark' and 'stark' scheme
  const schemes = ["snark", "stark"];
  const scheme = schemes[Math.floor(Math.random() * schemes.length)];

  // The payload expected by your worker [cite: 25, 27, 28]
  const payload = JSON.stringify({
    scheme: scheme, 
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