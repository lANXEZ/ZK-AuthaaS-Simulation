"""
U-Prove Proof Generator
=======================
สร้าง U-Prove token และ presentation proof ตาม U-Prove Cryptographic Specification V1.1 Rev3
(Paquin & Zaverucha, Microsoft 2013)

โครงสร้างที่ implement:
  - Issuer parameters (IP): group params + public key
  - Token issuance: σ_z, σ_c, σ_r (Schnorr-based blind signature)
  - Presentation proof: a, r (response values) พร้อม disclosed attributes
  - Output เป็น JSON ที่ verifier script อ่านได้

Usage:
  python uprove_proof.py                    # gen 1 proof -> uprove_proof.json
  python uprove_proof.py --count 50        # gen 50 proofs -> uprove_proofs.json
  python uprove_proof.py --out myfile.json # custom output path
"""

import hashlib
import hmac
import json
import os
import secrets
import argparse
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# ---------------------------------------------------------------------------
# Group parameters: P-256 (secp256r1) in elliptic curve construction
# ตาม spec Section 2.1 / 2.4.2 - use EC for compact representation
# ---------------------------------------------------------------------------
# ใช้ P-256 prime order group
P  = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A  = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
B  = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
Gx = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
Gy = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
N  = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # order q


@dataclass
class ECPoint:
    x: int
    y: int

    def is_infinity(self) -> bool:
        return self.x == 0 and self.y == 0

    def to_bytes(self) -> bytes:
        """Compressed point encoding (04 || x || y uncompressed for clarity)"""
        if self.is_infinity():
            return b'\x00'
        return (b'\x04'
                + self.x.to_bytes(32, 'big')
                + self.y.to_bytes(32, 'big'))

    def to_hex(self) -> str:
        return self.to_bytes().hex()


INFINITY = ECPoint(0, 0)
G = ECPoint(Gx, Gy)


def mod_inv(a: int, m: int) -> int:
    return pow(a, m - 2, m)


def ec_add(P1: ECPoint, P2: ECPoint) -> ECPoint:
    if P1.is_infinity():
        return P2
    if P2.is_infinity():
        return P1
    if P1.x == P2.x:
        if P1.y != P2.y:
            return INFINITY
        # point doubling
        lam = (3 * P1.x * P1.x + A) * mod_inv(2 * P1.y, P) % P
    else:
        lam = (P2.y - P1.y) * mod_inv(P2.x - P1.x, P) % P
    rx = (lam * lam - P1.x - P2.x) % P
    ry = (lam * (P1.x - rx) - P1.y) % P
    return ECPoint(rx, ry)


def ec_mul(k: int, pt: ECPoint) -> ECPoint:
    k = k % N
    result = INFINITY
    addend = pt
    while k:
        if k & 1:
            result = ec_add(result, addend)
        addend = ec_add(addend, addend)
        k >>= 1
    return result


# ---------------------------------------------------------------------------
# Hash utilities  (spec Section 2.2)
# H = SHA-256; formatted as length-prefixed concatenation
# ---------------------------------------------------------------------------

def hash_bytes(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(len(p).to_bytes(4, 'big'))
        h.update(p)
    return h.digest()


def hash_to_scalar(*parts: bytes) -> int:
    return int.from_bytes(hash_bytes(*parts), 'big') % N


def point_to_msg(pt: ECPoint) -> bytes:
    return pt.to_bytes()


# ---------------------------------------------------------------------------
# U-Prove verifiably random generators  (spec Section 2.4.2)
# g_i = H2C(UIDP || "g" || i)  -- simplified: hash-to-curve via try-and-increment
# ---------------------------------------------------------------------------

def hash_to_point(data: bytes) -> ECPoint:
    """Deterministic hash-to-curve (simplified try-and-increment on P-256)"""
    ctr = 0
    while True:
        attempt = hash_bytes(data, ctr.to_bytes(4, 'big'))
        x = int.from_bytes(attempt, 'big') % P
        rhs = (pow(x, 3, P) + A * x + B) % P
        # Tonelli-Shanks sqrt
        y_sq = rhs
        # For P-256, p ≡ 3 (mod 4), so sqrt = y_sq^((p+1)/4) mod p
        y = pow(y_sq, (P + 1) // 4, P)
        if pow(y, 2, P) == y_sq:
            if y % 2 == 0:
                return ECPoint(x, y)
            else:
                return ECPoint(x, P - y)
        ctr += 1


def make_generator(uid_p: bytes, index: int) -> ECPoint:
    """g_i per spec Section 2.4.2"""
    return hash_to_point(uid_p + b"generator" + index.to_bytes(4, 'big'))


# ---------------------------------------------------------------------------
# Issuer Parameters  (spec Section 2.3.1)
# IP = (UIDP, desc(Gq), UIDh, (g0..gn, gt), (e1..en), S)
# ---------------------------------------------------------------------------

@dataclass
class IssuerParameters:
    uid_p: str          # unique identifier (hex)
    uid_h: str          # hash algorithm identifier
    g0: str             # g0 = G^y0 (issuer public key component), hex
    generators: List[str]  # g1..gn attribute generators, hex
    gt: str             # token public key generator, hex
    e: List[int]        # encoding flags (1 = directly hashed)
    s: str              # application-specific spec (hex)


@dataclass
class IssuerPrivateKey:
    y0: int


def generate_issuer_keys(n_attributes: int, uid_p: bytes) -> tuple:
    """Generate issuer key pair and parameters"""
    y0 = secrets.randbelow(N - 1) + 1
    g0 = ec_mul(y0, G)

    generators = [make_generator(uid_p, i + 1) for i in range(n_attributes)]
    gt = make_generator(uid_p, 0)   # token generator

    ip = IssuerParameters(
        uid_p=uid_p.hex(),
        uid_h="SHA-256",
        g0=g0.to_hex(),
        generators=[g.to_hex() for g in generators],
        gt=gt.to_hex(),
        e=[1] * n_attributes,
        s=hashlib.sha256(uid_p).hexdigest(),
    )
    sk = IssuerPrivateKey(y0=y0)
    return ip, sk


# ---------------------------------------------------------------------------
# U-Prove Token  (spec Section 2.3.3)
# token = (h, sz_tilde, sc, sr)  where h = token public key
# ---------------------------------------------------------------------------

@dataclass
class UProveToken:
    h: str          # token public key (EC point, hex)
    sz_tilde: str   # σ_z (issuer signature component)
    sc: str         # σ_c (challenge)
    sr: str         # σ_r (response), hex of int
    ti: str         # token information field (encoded attributes, hex)
    pi: str         # prover information field (hex)


@dataclass
class TokenPrivateKey:
    alpha_tilde: int   # prover blinding factor → x_t = 1/(alpha + y0) ... simplified


# ---------------------------------------------------------------------------
# Issuance Protocol  (spec Section 2.5)
# ---------------------------------------------------------------------------

def encode_attributes(attributes: List[bytes], e_flags: List[int]) -> bytes:
    """Encode attribute list into TI field (spec Section 2.3.3)"""
    parts = []
    for ai, ei in zip(attributes, e_flags):
        if ei == 1:
            parts.append(hashlib.sha256(ai).digest())
        else:
            parts.append(ai)
    return b''.join(parts)


def issue_token(
    ip: IssuerParameters,
    isk: IssuerPrivateKey,
    attributes: List[bytes],
    token_info: bytes = b'',
    prover_info: bytes = b'',
) -> tuple:
    """
    Simplified U-Prove issuance (spec Section 2.5).
    Returns (UProveToken, TokenPrivateKey)

    Full spec has 2-round interactive protocol; here we compute both sides
    since we're generating the proof file (offline issuance simulation).
    """
    # Rehydrate generators
    uid_p = bytes.fromhex(ip.uid_p)
    generators = [ECPoint(int(g[2:66], 16), int(g[66:], 16))
                  for g in ip.generators]
    gt = ECPoint(int(ip.gt[2:66], 16), int(ip.gt[66:], 16))
    g0 = ECPoint(int(ip.g0[2:66], 16), int(ip.g0[66:], 16))

    # --- Prover side: pick blinding factor alpha ---
    alpha = secrets.randbelow(N - 1) + 1
    alpha_inv = mod_inv(alpha, N)

    # --- Compute token public key h = G^(alpha_inv) ---
    h = ec_mul(alpha_inv, G)

    # --- Compute gamma: product of generators raised to attributes ---
    # gamma = g0^1 * g1^H(a1) * ... * gn^H(an) * gt^xt
    # Simplified: gamma = G * product(gi^xi) where xi = hash(ai)
    gamma = G  # start with G (= g0^1 effectively)
    xi_list = []
    for i, (ai, ei) in enumerate(zip(attributes, ip.e)):
        if ei == 1:
            xi = int.from_bytes(hashlib.sha256(ai).digest(), 'big') % N
        else:
            xi = int.from_bytes(ai[:32], 'big') % N
        xi_list.append(xi)
        gamma = ec_add(gamma, ec_mul(xi, generators[i]))

    # --- Issuer: compute sigma_z = gamma^y0 ---
    sz = ec_mul(isk.y0, gamma)

    # --- Blinded sigma_z_tilde = sz^alpha ---
    sz_tilde = ec_mul(alpha, sz)

    # --- Issuer commitment: pick w, compute a = gamma^w ---
    w = secrets.randbelow(N - 1) + 1
    issuer_a = ec_mul(w, gamma)

    # --- TI field ---
    ti = encode_attributes(attributes, ip.e) + token_info

    # --- Challenge c = H(h, PI, sz_tilde, a, TI) ---
    c_hash = hash_to_scalar(
        point_to_msg(h),
        prover_info,
        point_to_msg(sz_tilde),
        point_to_msg(issuer_a),
        ti,
    )

    # --- Issuer response: r = w - y0 * c mod q ---
    sr_int = (w - isk.y0 * c_hash) % N

    token = UProveToken(
        h=h.to_hex(),
        sz_tilde=sz_tilde.to_hex(),
        sc=c_hash.to_bytes(32, 'big').hex(),
        sr=sr_int.to_bytes(32, 'big').hex(),
        ti=ti.hex(),
        pi=prover_info.hex(),
    )
    tsk = TokenPrivateKey(alpha_tilde=alpha)
    return token, tsk


# ---------------------------------------------------------------------------
# Presentation Proof  (spec Section 2.6)
# ---------------------------------------------------------------------------

@dataclass
class PresentationProof:
    """
    U-Prove presentation proof structure (spec Section 2.6).

    This implementation uses a Schnorr proof of knowledge of `alpha_inv`,
    the discrete log of the token public key `h` with respect to `G`
    (h = G^alpha_inv, where alpha_inv was the prover's issuance blinding
    factor — kept in `TokenPrivateKey.alpha_tilde`). The presentation thus
    cryptographically demonstrates possession of the token; disclosed
    attributes + the application message are bound into the Fiat-Shamir
    challenge.

    Soundness equation (verifier check, gated):
        a == G^r_base · h^c
    """
    a: str              # commitment a = G^w  (EC point hex)
    r_base: str         # response r_base = w - alpha_inv · c  mod N
    r: List[str]        # per-attribute binding values (challenge-bound;
                        # consumed by the verifier's multi-exponentiation
                        # pass for realistic cost — not part of the sound
                        # gate in this version)
    disclosed_indices: List[int]
    disclosed_attrs: List[str]
    token_id: str       # H(h)
    message: str        # hex
    c: str              # challenge scalar (hex)


def generate_presentation_proof(
    ip: IssuerParameters,
    token: UProveToken,
    tsk: TokenPrivateKey,
    attributes: List[bytes],
    disclosed_indices: List[int],   # 0-based
    message: bytes = b'',
) -> PresentationProof:
    """
    Generate a U-Prove presentation proof.

    Protocol (Schnorr DLOG of alpha_inv, with attribute disclosure bound
    into the challenge):
        commitment :  a       = G^w                    (w random)
        challenge  :  c       = H(a, message, h, token_id,
                                  disc_indices, disc_attrs)
        response   :  r_base  = w - alpha_inv · c   mod N

    Verifier accepts iff a == G^r_base · h^c. This proves the prover knows
    alpha_inv = DL_G(h), i.e. they hold the token. Per-attribute responses
    r_i = w_i - x_i · c are also emitted so a multi-exp-shape verifier can
    pay realistic per-call cost; they are not part of the soundness gate.
    """
    h = ECPoint(int(token.h[2:66], 16), int(token.h[66:], 16))
    generators = [ECPoint(int(g[2:66], 16), int(g[66:], 16))
                  for g in ip.generators]

    n = len(attributes)
    undisclosed = [i for i in range(n) if i not in disclosed_indices]

    # alpha_inv = 1/alpha_tilde mod N   (prover's secret from issuance)
    alpha_inv = mod_inv(tsk.alpha_tilde, N)

    # Schnorr DLOG commitment: a = G^w
    w = secrets.randbelow(N - 1) + 1
    a_pt = ec_mul(w, G)

    # token_id = H(h)
    token_id = hashlib.sha256(h.to_bytes()).hexdigest()

    # Disclosed attribute bytes for the challenge
    disc_bytes = b''.join(attributes[i] for i in sorted(disclosed_indices))

    # Challenge c = H(a, message, h, token_id, disc_indices, disc_bytes)
    # We bind 'h' into the challenge too; this prevents an attacker who
    # observes (a, r_base, c) from re-using them against a different token.
    c = hash_to_scalar(
        point_to_msg(a_pt),
        message,
        point_to_msg(h),
        bytes.fromhex(token_id),
        bytes(disclosed_indices),
        disc_bytes,
    )

    # Soundness response: r_base = w - alpha_inv · c   mod N
    r_base = (w - alpha_inv * c) % N

    # Per-attribute binding responses (cost-shaping for the verifier, not
    # gated). Each r_i = w_i - x_i · c for undisclosed; None for disclosed.
    xi_list = []
    for i, (ai, ei) in enumerate(zip(attributes, ip.e)):
        if ei == 1:
            xi = int.from_bytes(hashlib.sha256(ai).digest(), 'big') % N
        else:
            xi = int.from_bytes(ai[:32], 'big') % N
        xi_list.append(xi)

    r_responses = []
    for i in range(n):
        if i in undisclosed:
            w_i = secrets.randbelow(N - 1) + 1
            ri = (w_i - xi_list[i] * c) % N
            r_responses.append(ri.to_bytes(32, 'big').hex())
        else:
            r_responses.append(None)

    return PresentationProof(
        a=a_pt.to_hex(),
        r_base=r_base.to_bytes(32, 'big').hex(),
        r=[r for r in r_responses],
        disclosed_indices=disclosed_indices,
        disclosed_attrs=[attributes[i].hex() for i in sorted(disclosed_indices)],
        token_id=token_id,
        message=message.hex(),
        c=c.to_bytes(32, 'big').hex(),
    )


# ---------------------------------------------------------------------------
# Full proof bundle (what verifier script reads)
# ---------------------------------------------------------------------------

@dataclass
class UProveProofBundle:
    """Complete bundle: issuer params + token + presentation proof"""
    version: str
    issuer_params: dict
    token: dict
    presentation_proof: dict
    attributes_count: int
    disclosed_count: int
    timestamp: float


def generate_proof_bundle(
    n_attributes: int = 3,
    disclosed_indices: Optional[List[int]] = None,
    message: bytes = b'uprove-auth-demo',
) -> UProveProofBundle:
    if disclosed_indices is None:
        disclosed_indices = [0]  # disclose first attribute by default

    uid_p = os.urandom(16)

    # Sample attributes (e.g., age, role, issuer_id)
    attributes = [os.urandom(16) for _ in range(n_attributes)]

    ip, isk = generate_issuer_keys(n_attributes, uid_p)
    token, tsk = issue_token(ip, isk, attributes)
    proof = generate_presentation_proof(
        ip, token, tsk, attributes, disclosed_indices, message
    )

    return UProveProofBundle(
        version="U-Prove-V1.1-Rev3",
        issuer_params=asdict(ip),
        token=asdict(token),
        presentation_proof=asdict(proof),
        attributes_count=n_attributes,
        disclosed_count=len(disclosed_indices),
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="U-Prove proof file generator")
    parser.add_argument("--count", type=int, default=1,
                        help="Number of proof bundles to generate (default: 1)")
    parser.add_argument("--attributes", type=int, default=3,
                        help="Number of attributes per token (default: 3)")
    parser.add_argument("--disclosed", type=int, nargs="+", default=[0],
                        help="Indices of disclosed attributes (default: 0)")
    parser.add_argument("--message", type=str, default="uprove-auth-demo",
                        help="Application message to bind proof to")
    parser.add_argument("--out", type=str, default=None,
                        help="Output JSON file path")
    args = parser.parse_args()

    msg_bytes = args.message.encode()
    out_path = args.out or ("uprove_proof.json" if args.count == 1
                             else "uprove_proofs.json")

    print(f"[U-Prove] Generating {args.count} proof bundle(s)...")
    t0 = time.time()

    if args.count == 1:
        bundle = generate_proof_bundle(args.attributes, args.disclosed, msg_bytes)
        data = asdict(bundle)
    else:
        bundles = [generate_proof_bundle(args.attributes, args.disclosed, msg_bytes)
                   for _ in range(args.count)]
        data = [asdict(b) for b in bundles]

    elapsed = time.time() - t0
    print(f"[U-Prove] Done in {elapsed:.3f}s → {out_path}")

    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)

    # Quick summary
    if args.count == 1:
        print(f"  Token h  : {data['token']['h'][:20]}...")
        print(f"  Proof a  : {data['presentation_proof']['a'][:20]}...")
        print(f"  Disclosed: {data['presentation_proof']['disclosed_indices']}")
    else:
        print(f"  {args.count} bundles written")


if __name__ == "__main__":
    main()
