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
        redisHost: 'snark-queue',
        redisPort: 6379,
        proofQueueHost: 'proof-queue',
        proofQueuePort: 6379,
        index: null,
    };
    for (let i = 0; i < args.length; i++) {
        if      (args[i] === '--redis-host')        opts.redisHost = args[++i];
        else if (args[i] === '--redis-port')        opts.redisPort = parseInt(args[++i]);
        else if (args[i] === '--proof-queue-host')  opts.proofQueueHost = args[++i];
        else if (args[i] === '--proof-queue-port')  opts.proofQueuePort = parseInt(args[++i]);
        else if (args[i] === '--index')             opts.index = parseInt(args[++i]);
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

const rSnarkQueue = new Redis({ host: opts.redisHost,      port: opts.redisPort });
const rProofQueue = new Redis({ host: opts.proofQueueHost, port: opts.proofQueuePort });

function ts() {
    return new Date().toISOString().replace('T', ' ').slice(0, 19);
}

async function run() {
    console.log(`[${ts()}] SNARK worker idx=${myIndex} listening on '${myQueueKey}'`);

    while (true) {
        try {
            const job = await rSnarkQueue.brpop(myQueueKey, 0);
            if (!job) continue;

            const [, rawData] = job;
            const jobData = JSON.parse(rawData);
            const jobId   = jobData.job_id;
            const payload = jobData.payload || {};

            if (jobId) {
                await rProofQueue.set(`status:${jobId}`, "processing", "EX", 3600);
            }

            const proof         = payload.proof;
            const publicSignals = payload.public_inputs;

            const success     = await snarkjs.groth16.verify(vKey, publicSignals, proof);
            const finalStatus = success ? "completed" : "failed";

            if (jobId) {
                await rProofQueue.set(`status:${jobId}`, finalStatus, "EX", 3600);
            }

            console.log(`[${ts()}] SNARK idx=${myIndex} processed. Job ${jobId} -> ${finalStatus}`);

            try {
                await rProofQueue.publish("verifier_feedback", JSON.stringify({ type: "snark", index: myIndex }));
            } catch (e) {
                console.error(`[${ts()}] Failed to publish feedback: ${e.message}`);
            }

        } catch (e) {
            console.error(`[${ts()}] Error processing job: ${e.message}`);
            await new Promise(r => setTimeout(r, 1000));
        }
    }
}

run().catch(err => {
    console.error("Fatal error:", err);
    process.exit(1);
});
