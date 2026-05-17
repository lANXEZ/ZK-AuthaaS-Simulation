// Idemix Verifier Worker (on-prem)
// ================================
// Drop-in replacement for SNARKVerifierWorker.js and UProveVerifierWorker.py
// in the on-prem stack. Loops over jobs from snark_queue:N on the queue Redis,
// runs idemix.Signature.Ver() with pre-loaded issuer + revocation public keys,
// then writes status=completed|failed to the proof-queue Redis.
//
// Issuer public key, revocation public key, and the proof are all loaded ONCE
// at startup from .bin files baked into the container image (same parsed
// structs are reused for every verify). The k6 payload is just a trigger —
// this matches the SNARK/U-Prove tests' "verify the same constant proof on
// every request" CPU-cost-benchmark methodology.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/golang/protobuf/proto"
	"github.com/hyperledger/fabric-amcl/amcl/FP256BN"
	"github.com/hyperledger/fabric/idemix"
	"github.com/redis/go-redis/v9"
)

func mustReadFile(path string) []byte {
	b, err := os.ReadFile(path)
	if err != nil {
		log.Fatalf("read %s: %v", path, err)
	}
	return b
}

func main() {
	redisHost := flag.String("redis-host", "snark-queue", "queue redis host")
	redisPort := flag.Int("redis-port", 6379, "queue redis port")
	proofHost := flag.String("proof-queue-host", "proof-queue", "proof-queue redis host")
	proofPort := flag.Int("proof-queue-port", 6379, "proof-queue redis port")
	indexFlag := flag.Int("index", -1, "0-based worker slot (falls back to TASK_SLOT-1)")
	bundleDir := flag.String("bundle-dir", "/app/idemix", "directory with idemix_proof.bin, issuer_public_key.bin, revocation_public_key.bin")
	flag.Parse()

	idx := *indexFlag
	if idx < 0 {
		if s := os.Getenv("TASK_SLOT"); s != "" {
			n, err := strconv.Atoi(s)
			if err != nil {
				log.Fatalf("TASK_SLOT not an int: %v", err)
			}
			idx = n - 1
		} else {
			log.Fatalf("must provide --index or set TASK_SLOT")
		}
	}

	// ---- one-time load + parse ----
	ipkBytes := mustReadFile(*bundleDir + "/issuer_public_key.bin")
	ipk := &idemix.IssuerPublicKey{}
	if err := proto.Unmarshal(ipkBytes, ipk); err != nil {
		log.Fatalf("unmarshal IPK: %v", err)
	}

	revPkBytes := mustReadFile(*bundleDir + "/revocation_public_key.bin")
	genericPk, err := x509.ParsePKIXPublicKey(revPkBytes)
	if err != nil {
		log.Fatalf("parse rev pk: %v", err)
	}
	revPk, ok := genericPk.(*ecdsa.PublicKey)
	if !ok {
		log.Fatalf("rev pk is not ECDSA")
	}

	proofBytes := mustReadFile(*bundleDir + "/idemix_proof.bin")
	signature := &idemix.Signature{}
	if err := proto.Unmarshal(proofBytes, signature); err != nil {
		log.Fatalf("unmarshal proof: %v", err)
	}

	// Disclosure pattern, message, revealed attrs — all constant for this bench
	disclosure := []byte{1, 1, 0, 0}
	revealedAttrs := make([]*FP256BN.BIG, 4)
	revealedAttrs[0] = FP256BN.NewBIGint(1)
	revealedAttrs[1] = FP256BN.NewBIGint(1)
	msg := []byte("auth-challenge-123")

	// Sanity: run one verify before entering the loop
	if err := signature.Ver(disclosure, ipk, msg, revealedAttrs, 0, revPk, 0); err != nil {
		log.Fatalf("startup verify failed: %v", err)
	}
	log.Printf("Idemix worker idx=%d ready; startup verify OK", idx)

	// ---- Redis ----
	ctx := context.Background()
	rQueue := redis.NewClient(&redis.Options{Addr: fmt.Sprintf("%s:%d", *redisHost, *redisPort)})
	rProof := redis.NewClient(&redis.Options{Addr: fmt.Sprintf("%s:%d", *proofHost, *proofPort)})
	queueKey := fmt.Sprintf("snark_queue:%d", idx)
	log.Printf("Idemix worker idx=%d listening on '%s'", idx, queueKey)

	for {
		popped, err := rQueue.BRPop(ctx, 0, queueKey).Result()
		if err != nil {
			log.Printf("[ERROR] BRPOP: %v", err)
			time.Sleep(time.Second)
			continue
		}
		if len(popped) < 2 {
			continue
		}
		raw := popped[1]

		var job map[string]any
		if err := json.Unmarshal([]byte(raw), &job); err != nil {
			log.Printf("[ERROR] job json: %v", err)
			continue
		}
		jobID, _ := job["job_id"].(string)
		if jobID == "" {
			continue
		}
		rProof.Set(ctx, "status:"+jobID, "processing", time.Hour)

		// Verify (idemix.Signature.Ver is stateless — safe to call repeatedly
		// on the same parsed struct).
		verifyErr := signature.Ver(disclosure, ipk, msg, revealedAttrs, 0, revPk, 0)
		status := "completed"
		if verifyErr != nil {
			status = "failed"
		}
		rProof.Set(ctx, "status:"+jobID, status, time.Hour)

		// Feedback (selector ignores it in round-robin, keep for symmetry)
		fb, _ := json.Marshal(map[string]any{"type": "snark", "index": idx})
		rProof.Publish(ctx, "verifier_feedback", string(fb))
	}
}
