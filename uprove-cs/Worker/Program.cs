// U-Prove Verifier Worker (on-prem) — C# / Microsoft U-Prove SDK
// ==============================================================
// Drop-in replacement for the pure-Python UProveVerifierWorker.py.
//
// Architecture (same pattern as SNARK/Idemix workers):
//   - Issuer params + token + proof are loaded ONCE at startup from a
//     baked-in JSON bundle (uprove_proof_cs.json) and deserialized into
//     SDK objects. PresentationProof.Verify() is then invoked on every
//     job from the queue.
//   - BRPOP from `snark_queue:<index>` on the queue Redis (same routing
//     key as the SNARK + Idemix workers, kept identical so the selector
//     and request-handler don't need to know which algorithm is running).
//   - On verify success: SET status:<job_id>="completed" on proof-queue.
//   - On verify failure: SET status:<job_id>="failed".

using System;
using System.IO;
using System.Threading;
using Newtonsoft.Json.Linq;
using StackExchange.Redis;
using UProveCrypto;

namespace ZkAuthaaS.UProveCsWorker;

internal static class Program
{
    private static int Main(string[] args)
    {
        // ---- CLI parsing ------------------------------------------------
        string redisHost = "snark-queue";
        int    redisPort = 6379;
        string proofHost = "proof-queue";
        int    proofPort = 6379;
        int?   indexFlag = null;
        string bundlePath = "/app/uprove_proof_cs.json";

        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--redis-host":        redisHost  = args[++i]; break;
                case "--redis-port":        redisPort  = int.Parse(args[++i]); break;
                case "--proof-queue-host":  proofHost  = args[++i]; break;
                case "--proof-queue-port":  proofPort  = int.Parse(args[++i]); break;
                case "--index":             indexFlag  = int.Parse(args[++i]); break;
                case "--bundle":            bundlePath = args[++i]; break;
            }
        }

        // Resolve worker index (CLI wins, else TASK_SLOT-1 like Swarm convention)
        int myIndex;
        if (indexFlag is int v) myIndex = v;
        else
        {
            var slot = Environment.GetEnvironmentVariable("TASK_SLOT");
            if (string.IsNullOrEmpty(slot))
            {
                Console.Error.WriteLine("Must provide --index or set TASK_SLOT");
                return 1;
            }
            myIndex = int.Parse(slot) - 1;
        }

        // ---- One-time bundle parse -------------------------------------
        var bundle = JObject.Parse(File.ReadAllText(bundlePath));

        string ipJson    = bundle["issuer_params_json"]!.ToString();
        string tokenStr  = bundle["token_json"]!.ToString();
        string proofStr  = bundle["proof_json"]!.ToString();
        int[]  discIdx   = bundle["disclosed_indices"]!.ToObject<int[]>()!;
        byte[] message   = Convert.FromHexString(bundle["message"]!.ToString());

        IssuerParameters    ip    = new IssuerParameters(ipJson);
        ip.Verify();
        UProveToken         token = ip.Deserialize<UProveToken>(tokenStr);
        PresentationProof   proof = ip.Deserialize<PresentationProof>(proofStr);

        // Pre-build the verifier params struct so we don't reallocate per-call.
        var vppp = new VerifierPresentationProtocolParameters(ip, discIdx, message, token)
        {
            Committed              = null,
            PseudonymAttributeIndex = 0,
            PseudonymScope         = null,
            DeviceMessage          = null,
        };

        // Startup smoke verify — fail fast if the bundle is malformed so
        // Docker restarts us instead of silently looping with broken state.
        try
        {
            proof.Verify(vppp);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Startup verify failed: {ex.Message}");
            return 2;
        }

        Console.WriteLine($"U-Prove (C# SDK) worker idx={myIndex} ready; startup verify OK");

        // ---- Redis -----------------------------------------------------
        // SyncTimeout must comfortably exceed the BRPOP block window below;
        // 60 s default sync timeout would interrupt a 30 s BRPOP call.
        var qOpts = ConfigurationOptions.Parse($"{redisHost}:{redisPort}");
        qOpts.SyncTimeout = 60_000;
        var pOpts = ConfigurationOptions.Parse($"{proofHost}:{proofPort}");
        pOpts.SyncTimeout = 10_000;

        var rQueue = ConnectionMultiplexer.Connect(qOpts).GetDatabase();
        var rProof = ConnectionMultiplexer.Connect(pOpts).GetDatabase();
        string queueKey = $"snark_queue:{myIndex}";
        Console.WriteLine($"U-Prove (C# SDK) worker idx={myIndex} listening on '{queueKey}'");

        var statusTtl = TimeSpan.FromHours(1);

        // ---- Main loop -------------------------------------------------
        while (true)
        {
            string? jobId = null;
            try
            {
                // True BRPOP via low-level Execute (StackExchange.Redis doesn't
                // expose a typed wrapper, but the RESP protocol command works
                // directly). 30 s server-side block; if nothing arrives the
                // result is nil and we loop. Keeps the connection healthy
                // and well under our 60 s client-side SyncTimeout.
                RedisResult brpopResult = rQueue.Execute("BRPOP", queueKey, 30);
                if (brpopResult.IsNull) continue;
                RedisResult[] brpop = (RedisResult[])brpopResult;
                if (brpop.Length < 2) continue;
                string raw = (string)brpop[1]!;

                JObject job = JObject.Parse(raw);
                jobId = (string?)job["job_id"];
                if (string.IsNullOrEmpty(jobId)) continue;

                rProof.StringSet($"status:{jobId}", "processing", statusTtl);

                bool ok;
                try
                {
                    proof.Verify(vppp);
                    ok = true;
                }
                catch
                {
                    ok = false;
                }

                rProof.StringSet($"status:{jobId}", ok ? "completed" : "failed", statusTtl);

                // Feedback (round-robin selector ignores it, keep for symmetry)
                rProof.Publish("verifier_feedback",
                    $"{{\"type\":\"snark\",\"index\":{myIndex}}}");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[ERROR] worker {myIndex}: {ex.Message}");
                if (jobId is not null)
                {
                    try { rProof.StringSet($"status:{jobId}", "failed", statusTtl); }
                    catch { /* swallow */ }
                }
                Thread.Sleep(1000);
            }
        }
    }
}
