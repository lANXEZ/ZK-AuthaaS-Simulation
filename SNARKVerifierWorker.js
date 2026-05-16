// Limit ffjavascript worker threads to 1 — it reads os.cpus() to spawn workers.
// Inside Docker, os.cpus() returns ALL host CPUs (e.g. 96 on c5.24xlarge) even
// when the container is limited to 1.0 vCPU, causing Atomics deadlocks.
const os = require("os");
const _realCpus = os.cpus.bind(os);
os.cpus = () => [_realCpus()[0]];

const snarkjs = require("snarkjs");
const Redis = require("ioredis");
const fs = require("fs");
const path = require("path");

// ==========================================
// SNARK Verifier Worker (Node.js / snarkjs)
// ==========================================
// Reads jobs from a dedicated key "snark_queue:{index}" on a shared SNARK Redis.
// Index is derived from the Swarm task slot (1-based) via TASK_SLOT env var,
// or overridden with --index for manual runs.

function parseArgs() {
    const args = process.argv.slice(2);
    const opts = {
        redisHost:       'snark-queue',
        redisPort:       6379,
        proofQueueHost:  'proof-queue',
        proofQueuePort:  6379,
        tokenQueueHost:  'token-queue',
        tokenQueuePort:  6379,
        index:           null,
        mode:            'service',   // 'service' (push to verified_queue for token-issuer)
                                       // 'onprem'  (set status=completed directly, no token pipeline)
    };
    for (let i = 0; i < args.length; i++) {
        if      (args[i] === '--redis-host')        opts.redisHost       = args[++i];
        else if (args[i] === '--redis-port')        opts.redisPort       = parseInt(args[++i]);
        else if (args[i] === '--proof-queue-host')  opts.proofQueueHost  = args[++i];
        else if (args[i] === '--proof-queue-port')  opts.proofQueuePort  = parseInt(args[++i]);
        else if (args[i] === '--token-queue-host')  opts.tokenQueueHost  = args[++i];
        else if (args[i] === '--token-queue-port')  opts.tokenQueuePort  = parseInt(args[++i]);
        else if (args[i] === '--index')             opts.index           = parseInt(args[++i]);
        else if (args[i] === '--mode')              opts.mode            = args[++i];
    }
    if (opts.mode !== 'service' && opts.mode !== 'onprem') {
        console.error(`Invalid --mode: ${opts.mode} (must be 'service' or 'onprem')`);
        process.exit(1);
    }
    return opts;
}

const opts = parseArgs();

// Resolve 0-based index: CLI arg wins, otherwise derive from Swarm TASK_SLOT (1-based)
let myIndex;
if (opts.index !== null) {
    myIndex = opts.index;
} else {
    const slotEnv = process.env.TASK_SLOT;
    if (!slotEnv) {
        console.error("Must provide --index or set TASK_SLOT env var (Swarm's {{.Task.Slot}})");
        process.exit(1);
    }
    myIndex = parseInt(slotEnv) - 1;
}

const myQueueKey = `snark_queue:${myIndex}`;

// Load verification key once at startup — never reloaded per-job
const vKey = JSON.parse(fs.readFileSync(path.join(__dirname, "verification_key.json"), "utf8"));

const rSnarkQueue  = new Redis({ host: opts.redisHost,      port: opts.redisPort });
const rProofQueue  = new Redis({ host: opts.proofQueueHost, port: opts.proofQueuePort });
// token-queue only exists in service mode; on-prem skips token issuance entirely.
const rTokenQueue  = opts.mode === 'service'
    ? new Redis({ host: opts.tokenQueueHost, port: opts.tokenQueuePort })
    : null;

function ts() {
    return new Date().toISOString().replace('T', ' ').slice(0, 19);
}

async function run() {
    console.log(`[${ts()}] SNARK worker idx=${myIndex} listening on '${myQueueKey}'`);

    while (true) {
        let jobId = null;
        try {
            const job = await rSnarkQueue.brpop(myQueueKey, 0);
            if (!job) continue;

            const [, rawData] = job;
            const jobData = JSON.parse(rawData);
            jobId         = jobData.job_id;
            const payload = jobData.payload || {};

            if (jobId) {
                await rProofQueue.set(`status:${jobId}`, "processing", "EX", 3600);
            }

            const proof         = payload.proof;
            const publicSignals = payload.public_inputs;

            const success = await snarkjs.groth16.verify(vKey, publicSignals, proof);

            if (success) {
                if (opts.mode === 'onprem') {
                    // On-prem: no token pipeline. Verification success IS the completion event,
                    // so flip status to "completed" directly and k6's poll will resolve.
                    await rProofQueue.set(`status:${jobId}`, "completed", "EX", 3600);
                } else {
                    // Service mode: verification passed — enqueue to token-queue for the
                    // token-creation module. Status is intentionally left as "processing";
                    // the token module sets "completed" once a token has been issued.
                    const verifiedEntry = JSON.stringify({
                        job_id:        jobId,
                        scheme:        "snark",
                        public_inputs: publicSignals,
                        verified_at:   Date.now() / 1000,   // Unix timestamp (seconds, float)
                    });
                    await rTokenQueue.lpush("verified_queue", verifiedEntry);
                }
            } else {
                // Verification failed — immediately mark so k6 can stop polling.
                if (jobId) {
                    await rProofQueue.set(`status:${jobId}`, "failed", "EX", 3600);
                }
                console.log(`[${ts()}] SNARK idx=${myIndex} verification FAILED. Job ${jobId} -> failed`);
            }

            // Always publish feedback so the selector decrements this worker's pseudo-queue depth.
            // The worker is free to accept the next job regardless of whether the proof passed.
            try {
                await rProofQueue.publish("verifier_feedback", JSON.stringify({ type: "snark", index: myIndex }));
            } catch (e) {
                console.error(`[${ts()}] Failed to publish feedback: ${e.message}`);
            }

        } catch (e) {
            console.error(`[${ts()}] Error processing job: ${e.message}`);
            if (jobId) {
                await rProofQueue.set(`status:${jobId}`, "failed", "EX", 3600).catch(() => {});
                await rProofQueue.publish("verifier_feedback", JSON.stringify({ type: "snark", index: myIndex })).catch(() => {});
            }
            await new Promise(r => setTimeout(r, 1000));
        }
    }
}

run().catch(err => {
    console.error("Fatal error:", err);
    process.exit(1);
});
