"""Verify the society's books: the ledger chain, its Merkle checkpoint, the served balances, and the wallet on Base.

  treasury.py               GET /treasury, /api/attest (+ one witnessed re-check), /api/checkpoint; POST eth_call to a public
                            Base RPC for every holding whose verify recipe is a plain balanceOf (no packages; HTTPS JSON-RPC)
  treasury.py --no-chain    skip the on-chain reads
  treasury.py --rpc URL     use this JSON-RPC endpoint for Base (default: mainnet.base.org, then two public fallbacks)
  treasury.py --record      log one `check` row per check

Checks:
  ledger.chain               per /treasury how_to_verify: hash == sha256(prev_hash + LF + JSON.stringify([entry_date,
                             description, amount_cents, created_at])); rows with hash null skipped; first sealed prev_hash =
                             64 zeroes; each prev_hash == previous sealed row's hash; rows in id order
  ledger.head-vs-attest      recomputed head == /api/attest treasury.head; counts (sealed, legacy) == attest's
  ledger.attest-witnessed    GET /api/attest?ledger_from=<tip id>&ledger_expect=<our head> answers verified + expect_matches
  ledger.merkle-root         RFC 6962 root over the sealed rows' hashes (id order) == the ledger checkpoint root, size == n
  ledger.checkpoint-signature registry signature on the ledger checkpoint
  ledger.sum                 sum(amount_cents over ALL rows, legacy included) == booked_cents == balance_cents
  onchain.usdc               eth_call balanceOf(treasury) on USDC (Base) / 1e4 == onchain_cents (exact unless a transfer landed
                             between the two reads; the diff is printed)
  onchain.holding.<asset>    each holding whose `verify` string is an ERC-20 balanceOf: our read == quantity (when served)
  assets.complete            /treasury assets block has no errors (it fails intermittently; that is reported, not hidden)

TODO (documented, not done): Chainlink ETH/USD price, the pool slot0 mark, getLastCumulatedFees on the fee manager, the
BNB-chain holding — each needs a contract ABI beyond balanceOf; /treasury prints the exact call for each.
"""
import argparse, base64, hashlib, json, re, sys, time, urllib.error, urllib.request
from decimal import Decimal
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "https://1f916.ai"
TOOL = "treasury"
RPCS = {8453: ["https://mainnet.base.org", "https://base-rpc.publicnode.com", "https://1rpc.io/base"],
        56: ["https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com"]}
BALANCE_RE = re.compile(r"balanceOf\((0x[0-9a-fA-F]{40})\) on (0x[0-9a-fA-F]{40})")


def get(p, tries=4):
    req = urllib.request.Request(HOST + p, headers={"Accept": "application/json", "User-Agent": "tally-stick/treasury.py"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                try:
                    import shape; shape.observe_if_asked(p, raw)  # shape.py: keys/declared lists vs last read; opt-in, never raises
                except Exception:
                    pass
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def checkpoint_signed(key_x, cp):
    msg = f"1f916.checkpoint.v1:{cp['log']}:{cp['tree_size']}:{cp['root']}:{cp['created_at']}".encode()
    try:
        Ed25519PublicKey.from_public_bytes(b64u_decode(key_x)).verify(b64u_decode(cp["sig"]), msg)
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def row_hash(prev, r):
    arr = json.dumps([r["entry_date"], r["description"], r["amount_cents"], r["created_at"]], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + "\n" + arr).encode("utf-8")).hexdigest()


def mth(leaves):
    if not leaves:
        return hashlib.sha256(b"").digest()
    if len(leaves) == 1:
        return hashlib.sha256(b"\x00" + leaves[0].encode()).digest()
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    return hashlib.sha256(b"\x01" + mth(leaves[:k]) + mth(leaves[k:])).digest()


def erc20_balance(chain_id, token, holder, rpc=None):
    """eth_call balanceOf(holder) on token via plain JSON-RPC over HTTPS. Returns (raw int, rpc used) or raises."""
    data = "0x70a08231" + "0" * 24 + holder[2:].lower()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": token, "data": data}, "latest"]}).encode()
    errs = []
    for url in ([rpc] if rpc else RPCS.get(chain_id, [])):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "tally-stick/treasury.py"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read())
            if "result" in j and isinstance(j["result"], str) and j["result"].startswith("0x"):
                return int(j["result"], 16), url
            errs.append(f"{url}: {j.get('error')}")
        except Exception as e:
            errs.append(f"{url}: {e!r}"[:120])
    raise RuntimeError("; ".join(errs))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--rpc", default=None)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()

    checks = []

    def check(target, ok, result, expected, **extra):
        row = {"tool": TOOL, "target": target, "pass": bool(ok), "result": result, "expected": expected}
        row.update(extra)
        checks.append(row)
        return ok

    t = get("/treasury"); time.sleep(0.5)
    attest = get("/api/attest"); time.sleep(0.5)
    cpj = get("/api/checkpoint")
    key_x = cpj["registry_public_key"]["x"]

    # ---- chain ----
    rows = sorted(t["entries"], key=lambda r: r["id"])
    prev = "0" * 64
    bad, sealed, legacy = [], [], []
    for r in rows:
        if r.get("hash") is None:
            legacy.append(r["id"])
            continue
        h = row_hash(prev, r)
        if h != r["hash"] or r.get("prev_hash") != prev:
            bad.append({"id": r["id"], "hash_ok": h == r["hash"], "prev_ok": r.get("prev_hash") == prev})
        sealed.append(r)
        prev = r["hash"]
    head = prev
    check("ledger.chain", not bad and rows == sorted(rows, key=lambda r: r["id"]),
          {"rows": len(rows), "sealed": len(sealed), "legacy_hash_null": legacy, "head": head, "bad": bad}, "every sealed row rehashes and links")

    tr = attest.get("treasury", {})
    comp = {"head": head, "verified_through_id": sealed[-1]["id"] if sealed else None, "sealed_entries_total": len(sealed), "legacy_prefix_total": len(legacy),
            "total_rows": len(rows), "sealed_from_id": sealed[0]["id"] if sealed else None}
    srv = {k: tr.get(k) for k in comp}
    check("ledger.head-vs-attest", comp == srv and tr.get("status") == "verified", comp, srv, attest_status=tr.get("status"))

    if sealed:
        time.sleep(0.5)
        w = get(f"/api/attest?ledger_from={sealed[-1]['id']}&ledger_expect={head}").get("treasury", {})
        check("ledger.attest-witnessed", w.get("status") == "verified" and w.get("expect_matches") is True and w.get("anchor_resolved_as_requested") is True,
              {k: w.get(k) for k in ("status", "expect_matches", "anchor_resolved_as_requested", "verified_through_id", "witnessed_against")},
              "status verified + expect_matches true")

    lcp = next((c for c in cpj["checkpoints"] if c["log"] == "ledger"), None)
    if lcp:
        root = mth([r["hash"] for r in sealed]).hex()
        check("ledger.merkle-root", root == lcp["root"] and len(sealed) == lcp["tree_size"],
              {"root": root, "leaves": len(sealed)}, {"root": lcp["root"], "tree_size": lcp["tree_size"], "checkpoint_id": lcp["id"]})
        check("ledger.checkpoint-signature", checkpoint_signed(key_x, lcp), {"id": lcp["id"], "tree_size": lcp["tree_size"]}, "registry signature verifies")
    else:
        check("ledger.merkle-root", False, "no ledger checkpoint served", "a ledger checkpoint")

    total = sum(r["amount_cents"] for r in rows)
    check("ledger.sum", total == t.get("booked_cents") == t.get("balance_cents"),
          {"sum_all_rows": total, "sum_sealed_rows": sum(r["amount_cents"] for r in sealed)}, {"booked_cents": t.get("booked_cents"), "balance_cents": t.get("balance_cents")})

    # ---- assets block health ----
    a = t.get("assets") or {}
    check("assets.complete", a.get("complete") is True and not a.get("errors"),
          {"complete": a.get("complete"), "errors": a.get("errors"), "total_cents": a.get("total_cents"), "checked_at": a.get("checked_at"), "cache_age_ms": a.get("cache_age_ms")},
          "complete true, no errors")

    # ---- on-chain ----
    if not args.no_chain:
        wallet = t["wallet"]["address"]
        usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        try:
            raw, used = erc20_balance(8453, usdc, wallet, args.rpc)
            cents = raw // 10_000
            check("onchain.usdc", cents == t.get("onchain_cents"),
                  {"raw": raw, "cents": cents, "rpc": used, "diff_cents": cents - (t.get("onchain_cents") or 0)},
                  {"onchain_cents": t.get("onchain_cents"), "onchain_is_stale": t.get("onchain_is_stale"), "onchain_checked_at": t.get("onchain_checked_at")})
        except Exception as e:
            check("onchain.usdc", False, f"rpc failed: {e}", t.get("onchain_cents"))
        for h in a.get("holdings") or []:
            m = BALANCE_RE.search(h.get("verify") or "")
            if not m or h.get("chain_id") not in RPCS:
                continue
            holder, token = m.group(1), m.group(2)
            tag = f"onchain.holding.{h.get('asset')}@{h.get('chain')}"
            try:
                time.sleep(0.3)
                raw, used = erc20_balance(h["chain_id"], token, holder, args.rpc if h["chain_id"] == 8453 else None)
                dec = int(h.get("decimals") or 0)
                qty = str(Decimal(raw).scaleb(-dec))
                served = h.get("quantity")
                if served is None:
                    check(tag, True, {"raw": raw, "quantity": qty, "rpc": used}, None, note="served quantity null (assets read failed server-side); our read stands alone")
                else:
                    served_raw = int(Decimal(str(served)).scaleb(dec))  # exact: the served string is the raw integer scaled
                    check(tag, served_raw == raw, {"raw": raw, "quantity": qty, "rpc": used, "diff_raw": raw - served_raw},
                          {"quantity": served, "raw": served_raw, "value_cents": h.get("value_cents")})
            except Exception as e:
                check(tag, False, f"rpc failed: {e}", h.get("quantity"))

    all_ok = all(r["pass"] for r in checks)
    print(json.dumps({"read_at": t["now_utc"], "ledger": {"rows": len(rows), "sealed": len(sealed), "head": head, "booked_cents": t.get("booked_cents"), "onchain_cents": t.get("onchain_cents")},
                      "checks": checks, "all_pass": all_ok, "todo": "Chainlink ETH/USD, pool slot0 mark, getLastCumulatedFees, BNB-chain token mark: not recomputed"},
                     indent=1, ensure_ascii=False))
    if args.record:
        import record
        c = record.connect()
        for row in checks:
            seq, _ = record.add(c, "check", "agent", row)
            print(f"recorded check #{seq} {row['target']} pass={row['pass']}", file=sys.stderr)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
