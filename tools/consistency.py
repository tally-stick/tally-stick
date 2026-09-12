"""RFC 6962 consistency proof between two signed checkpoints: the log only ever APPENDED between them.

  consistency.py                          our last recorded checkpoint (identity_events) -> the live one
  consistency.py --log ledger             same for the ledger log
  consistency.py --from 11744 --to 11890  explicit tree sizes (the API resolves checkpoints by tree_size only;
                                          a checkpoint ID is not accepted there, so 'id:17643' is resolved from
                                          OUR recorded observed-head rows when you pass one)
  consistency.py --self-test              no network: exhaustive synthetic trees, sizes 1..64, every a<b, plus
                                          negative controls (tampered proof / wrong root / wrong size must fail)
  consistency.py --record                 log one `check` row per check

Checks (each a check row):
  consistency.<log>.<a>-><b>.from-signature   registry signature on the served `from` checkpoint
  consistency.<log>.<a>-><b>.to-signature     registry signature on the served `to` checkpoint
  consistency.<log>.<a>-><b>.from-root-matches-ours
                                             served `from` root == the root WE recorded at that size (if we have one)
  consistency.<log>.<a>-><b>.to-root-matches-live
                                             served `to` root == the live /api/checkpoint root when sizes agree
  consistency.<log>.<a>-><b>.proof            the proof reconstructs both roots (RFC 9162 s2.1.4.2)

Algorithm: RFC 9162 s2.1.4.2, integer-safe (Python ints), every hash validated as 64 lowercase hex.
Exit 0 on all pass, 1 on any fail, 2 on a usage/shape problem.
"""
import argparse, base64, datetime, hashlib, json, re, sys, time, urllib.error, urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "https://1f916.ai"
TOOL = "consistency"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def get(p, tries=4):
    req = urllib.request.Request(HOST + p, headers={"Accept": "application/json", "User-Agent": "tally-stick/consistency.py"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def registry_key_from(jwk):
    x = jwk["x"] if isinstance(jwk, dict) else jwk
    return Ed25519PublicKey.from_public_bytes(b64u_decode(x)), x


def checkpoint_signed(key, log, cp):
    msg = f"1f916.checkpoint.v1:{log}:{cp['tree_size']}:{cp['root']}:{cp['created_at']}".encode()
    try:
        key.verify(b64u_decode(cp["sig"]), msg)
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------- RFC 6962 / 9162 tree math ----------------
def node_hash(l, r):
    return hashlib.sha256(b"\x01" + l + r).digest()


def leaf_hash(leaf_hex):
    return hashlib.sha256(b"\x00" + leaf_hex.encode()).digest()


def verify_consistency(m, n, old_root, new_root, proof):
    """RFC 9162 s2.1.4.2. m, n tree sizes; roots and proof are lowercase hex. Returns bool."""
    if not (isinstance(m, int) and isinstance(n, int) and m >= 0 and n >= 0):
        return False
    if not (HEX64.match(old_root or "") and HEX64.match(new_root or "")):
        return False
    if not isinstance(proof, list) or not all(isinstance(p, str) and HEX64.match(p) for p in proof):
        return False
    if m > n:
        return False
    if m == n:
        return len(proof) == 0 and old_root == new_root
    if m == 0:
        return len(proof) == 0
    if len(proof) == 0:
        return False
    fn, sn = m - 1, n - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    path = [bytes.fromhex(p) for p in proof]
    i = 0
    if fn == 0:
        fr = sr = bytes.fromhex(old_root)
    else:
        fr = sr = path[0]
        i = 1
    while i < len(path):
        c = path[i]
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            while not (fn & 1) and fn != 0:
                fn >>= 1
                sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
        i += 1
    return fr.hex() == old_root and sr.hex() == new_root and sn == 0


# --- reference tree + proof generator, only for --self-test (RFC 6962 s2.1 MTH / s2.1.2 SUBPROOF) ---
def mth(leaves):
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaf_hash(leaves[0])
    k = 1
    while k * 2 < n:
        k *= 2
    return node_hash(mth(leaves[:k]), mth(leaves[k:]))


def subproof(m, leaves, b):
    n = len(leaves)
    if m == n:
        return [] if b else [mth(leaves)]
    k = 1
    while k * 2 < n:
        k *= 2
    if m <= k:
        return subproof(m, leaves[:k], b) + [mth(leaves[k:])]
    return subproof(m - k, leaves[k:], False) + [mth(leaves[:k])]


def gen_proof(m, leaves):
    return [] if m == 0 else [h.hex() for h in subproof(m, leaves, True)]


def self_test(max_n=64):
    leaves = [hashlib.sha256(f"leaf-{i}".encode()).hexdigest() for i in range(max_n)]
    roots = [None] + [mth(leaves[:n]).hex() for n in range(1, max_n + 1)]
    ok = bad_pass = 0
    pairs = 0
    neg = 0
    for a in range(1, max_n + 1):
        for b in range(a, max_n + 1):
            pairs += 1
            pf = gen_proof(a, leaves[:b])
            if not verify_consistency(a, b, roots[a], roots[b], pf):
                bad_pass += 1
                print(f"FAIL positive {a}->{b}", file=sys.stderr)
                continue
            ok += 1
            if a < b:
                # negative controls: wrong old root, wrong new root, wrong m, tampered / truncated proof
                wrong = roots[a - 1] if a > 1 else roots[a + 1]
                assert not verify_consistency(a, b, wrong, roots[b], pf), f"wrong old root accepted {a}->{b}"
                assert not verify_consistency(a, b, roots[a], roots[b - 1], pf), f"wrong new root accepted {a}->{b}"
                if pf:
                    t = list(pf)
                    t[0] = hashlib.sha256(t[0].encode()).hexdigest()
                    assert not verify_consistency(a, b, roots[a], roots[b], t), f"tampered proof accepted {a}->{b}"
                    assert not verify_consistency(a, b, roots[a], roots[b], pf[:-1]), f"truncated proof accepted {a}->{b}"
                    assert not verify_consistency(a, b, roots[a], roots[b], pf + [pf[-1]]), f"extended proof accepted {a}->{b}"
                if a + 1 < b:
                    assert not verify_consistency(a + 1, b, roots[a + 1], roots[b], pf), f"wrong m accepted {a}->{b}"
                neg += 1
    # the RFC 6962 worked example: tree of 7, proofs from sizes 3,4,6 use specific node sets — covered above by exhaustion.
    return {"sizes": f"1..{max_n}", "pairs": pairs, "positive_pass": ok, "positive_fail": bad_pass, "negative_control_groups": neg}


# ---------------- record helpers ----------------
def recorded_checkpoints(c, log):
    """All checkpoints we hold for a log from observed-head rows (source 'checkpoint'), newest first: [(seq, ts, cp)]."""
    out = []
    for seq, ts, payload in c.execute("SELECT seq, ts, payload FROM observed_heads WHERE source='checkpoint' ORDER BY seq DESC"):
        p = json.loads(payload)
        for cp in p.get("checkpoints", []):
            if cp.get("log") == log:
                out.append((seq, ts, cp))
    return out


def resolve_size(val, recorded):
    """'id:<checkpoint id>' -> tree_size from our record; otherwise int tree_size."""
    if isinstance(val, str) and val.startswith("id:"):
        cid = int(val[3:])
        for _, _, cp in recorded:
            if cp.get("id") == cid:
                return cp["tree_size"]
        sys.exit(f"checkpoint id {cid} is not in our recorded observed-head rows; pass a tree_size instead")
    return int(val)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="identity_events")
    ap.add_argument("--from", dest="frm", default=None, help="tree_size, or id:<checkpoint id> from our record")
    ap.add_argument("--to", default=None, help="tree_size, or id:<checkpoint id>; default = live /api/checkpoint")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--max-n", type=int, default=64)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        r = self_test(args.max_n)
        print(json.dumps({"self_test": r, "all_pass": r["positive_fail"] == 0}, indent=1))
        sys.exit(0 if r["positive_fail"] == 0 else 1)

    import record
    c = record.connect()
    recorded = recorded_checkpoints(c, args.log)
    ours_by_size = {}
    for seq, ts, cp in recorded:
        ours_by_size.setdefault(cp["tree_size"], (seq, ts, cp))  # newest row wins

    live = get("/api/checkpoint")
    key, key_x = registry_key_from(live["registry_public_key"])
    live_cp = next((x for x in live.get("checkpoints", []) if x["log"] == args.log), None)
    if live_cp is None:
        sys.exit(f"no live checkpoint for log {args.log!r}")

    if args.frm is None:
        if not recorded:
            sys.exit(f"no recorded checkpoint for {args.log} in record/tally.db; pass --from")
        a = recorded[0][2]["tree_size"]
    else:
        a = resolve_size(args.frm, recorded)
    b = live_cp["tree_size"] if args.to is None else resolve_size(args.to, recorded)
    if a > b:
        sys.exit(f"from ({a}) > to ({b}): the log would have SHRUNK; that is a heads.py alarm, not a consistency query")

    checks = []

    def check(target, ok, result, expected, **extra):
        row = {"tool": TOOL, "target": target, "pass": bool(ok), "result": result, "expected": expected}
        row.update(extra)
        checks.append(row)
        return ok

    tag = f"{args.log}.{a}->{b}"
    time.sleep(0.5)
    try:
        pr = get(f"/api/checkpoint/consistency?log={args.log}&from={a}&to={b}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        check(f"consistency.{tag}.proof", False, f"HTTP {e.code}: {body}", "a proof object")
        print(json.dumps({"log": args.log, "from": a, "to": b, "checks": checks, "all_pass": False}, indent=1, ensure_ascii=False))
        if args.record:
            for row in checks:
                record.add(c, "check", "agent", row)
        sys.exit(1)

    shape_notes = []
    for k in ("from", "to", "proof"):
        if k not in pr:
            shape_notes.append(f"response lacks {k!r}")
    if shape_notes:
        check(f"consistency.{tag}.shape", False, shape_notes, "from/to/proof present")
    else:
        f, t = pr["from"], pr["to"]
        if f["tree_size"] != a or t["tree_size"] != b:
            shape_notes.append(f"served sizes {f['tree_size']}->{t['tree_size']} differ from requested {a}->{b}")
        check(f"consistency.{tag}.from-signature", checkpoint_signed(key, args.log, f),
              {"tree_size": f["tree_size"], "root": f["root"], "created_at": iso(f["created_at"])}, "registry signature verifies")
        check(f"consistency.{tag}.to-signature", checkpoint_signed(key, args.log, t),
              {"tree_size": t["tree_size"], "root": t["root"], "created_at": iso(t["created_at"])}, "registry signature verifies")
        if a in ours_by_size:
            seq, ts, ours = ours_by_size[a]
            check(f"consistency.{tag}.from-root-matches-ours", f["root"] == ours["root"], f["root"], ours["root"], baseline_seq=seq, baseline_ts=ts)
        if b in ours_by_size:
            seq, ts, ours = ours_by_size[b]
            check(f"consistency.{tag}.to-root-matches-ours", t["root"] == ours["root"], t["root"], ours["root"], baseline_seq=seq, baseline_ts=ts)
        if b == live_cp["tree_size"]:
            check(f"consistency.{tag}.to-root-matches-live", t["root"] == live_cp["root"], t["root"], live_cp["root"], live_checkpoint_id=live_cp["id"])
        ok = verify_consistency(f["tree_size"], t["tree_size"], f["root"], t["root"], pr["proof"])
        check(f"consistency.{tag}.proof", ok, {"proof_len": len(pr["proof"]), "from_root": f["root"], "to_root": t["root"]},
              "proof reconstructs both roots (RFC 9162 s2.1.4.2)")

    all_ok = all(r["pass"] for r in checks)
    print(json.dumps({"log": args.log, "from": a, "to": b, "live_checkpoint": {"id": live_cp["id"], "tree_size": live_cp["tree_size"]},
                      "registry_public_key": key_x, "shape_notes": shape_notes, "checks": checks, "all_pass": all_ok},
                     indent=1, ensure_ascii=False))
    if args.record:
        for row in checks:
            seq, _ = record.add(c, "check", "agent", row)
            print(f"recorded check #{seq} {row['target']} pass={row['pass']}", file=sys.stderr)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
