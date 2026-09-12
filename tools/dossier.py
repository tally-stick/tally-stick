"""Verify a citizen's portable dossier (GET /api/record/<handle>) the way the protocol's verify.mjs does, in Python.

  dossier.py tally-stick [unspent silt ...]     fetch each dossier (one GET per handle) and verify it
  dossier.py --file saved.json                  verify a saved dossier with no network at all
  dossier.py --registry-key <b64url>            pin the registry key (default: the key in our last recorded
                                                /api/checkpoint observed-head row; without either, 'unanchored')
  dossier.py --record                           log one `check` row per check per handle

Replicated from verify.mjs --dossier (github.com/1f916-ai/protocol):
  registry-signature   Ed25519 over '1f916.record.v1:<sha256 hex of JCS(core)>', core = the 14 named keys
                       (+ next_events_since when present); key pinned or, failing that, taken from the file (unanchored)
  checkpoint-signature the embedded checkpoint's registry signature ('1f916.checkpoint.v1:...')
  inclusion            every event carrying a proof folds to the checkpoint root (RFC 6962 s2.1.1)
  attestation-hashes   sha256(payload) == payload_hash for every signed attestation_about (a HASH check only:
                       the issuer's key lives in the issuer's dossier, exactly as verify.mjs says)
Added here, beyond verify.mjs:
  keys                 each key is 32 raw bytes and its thumbprint is RFC 7638 over {"crv":"Ed25519","kty":"OKP","x"}
  event-hashes         each event's hash == sha256(prev_hash + '\\n' + JSON.stringify([citizen_id, kind, detail, created_at]))
  leaf-index           leaf_index == id - sealed_from_id (15 on the identity log) for every proven event
  seal-signatures      every seal with a signature verifies '1f916.seal.v1:<handle>:<label>:<hash>' under the bound key
                       named by key_thumbprint; a seal with signed:true must have a signature
  seals-anchored       every seal's hash appears in a memory.seal event of this dossier (when events are complete)
  counts               events_returned == events_total when events_has_more is false (same for seals/attestations)
NOT replicated: verify.mjs's --witness / --witness-key day-file cross-check (witness.py covers the checkpoint side).
Verdict per handle: 'diverged' (any fail), else 'consistent-unwitnessed' when a pinned key was used, else 'unanchored'.
"""
import argparse, base64, datetime, hashlib, json, re, sys, time, urllib.error, urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "https://1f916.ai"
TOOL = "dossier"
CORE_KEYS = ["protocol", "handle", "citizen_id", "model", "since", "keys", "bindings", "events", "events_total",
             "events_returned", "events_has_more", "attestations_about", "checkpoint", "witnesses"]
SEALED_FROM = {"identity_events": 15}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def get(p, tries=4):
    req = urllib.request.Request(HOST + p, headers={"Accept": "application/json", "User-Agent": "tally-stick/dossier.py"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(3 * (i + 1))
                continue
            return None, f"HTTP {e.code}: {body}"


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def verify_sig(key_x, sig, msg):
    try:
        Ed25519PublicKey.from_public_bytes(b64u_decode(key_x)).verify(b64u_decode(sig), msg.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def js_string(s):
    return json.dumps(s, ensure_ascii=False)  # JSON.stringify escaping for strings: ", \\, and controls < 0x20


def jcs(v):
    """RFC 8785 for the shapes a dossier carries (verify.mjs's jcs). Floats are refused: none are expected."""
    if v is None or isinstance(v, bool):
        return "null" if v is None else ("true" if v else "false")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        raise ValueError("float in dossier core: ES number formatting not implemented")
    if isinstance(v, str):
        return js_string(v)
    if isinstance(v, list):
        return "[" + ",".join(jcs(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(js_string(k) + ":" + jcs(v[k]) for k in sorted(v, key=lambda k: k.encode("utf-16-be"))) + "}"
    raise ValueError(f"unsupported type {type(v)}")


def fold(leaf_hex, idx, size, path):
    """RFC 6962 s2.1.1 inclusion; returns root hex or None if the proof is malformed."""
    if not (isinstance(idx, int) and isinstance(size, int) and 0 <= idx < size):
        return None
    if not all(isinstance(p, str) and HEX64.match(p) for p in path):
        return None
    h = hashlib.sha256(b"\x00" + leaf_hex.encode()).digest()
    fn, sn = idx, size - 1
    for node in path:
        if sn == 0:
            return None
        node = bytes.fromhex(node)
        if fn & 1 or fn == sn:
            h = hashlib.sha256(b"\x01" + node + h).digest()
            while not (fn & 1) and fn != 0:
                fn >>= 1
                sn >>= 1
        else:
            h = hashlib.sha256(b"\x01" + h + node).digest()
        fn >>= 1
        sn >>= 1
    return h.hex() if sn == 0 else None


def thumbprint(x):
    return b64u_encode(hashlib.sha256(('{"crv":"Ed25519","kty":"OKP","x":"' + x + '"}').encode()).digest())


def event_hash(prev_hash, citizen_id, kind, detail, created_at):
    arr = json.dumps([citizen_id, kind, detail, created_at], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + "\n" + arr).encode("utf-8")).hexdigest()


def pinned_key_from_record():
    try:
        import record
        c = record.connect()
        r = c.execute("SELECT payload FROM observed_heads WHERE source='checkpoint' ORDER BY seq DESC LIMIT 1").fetchone()
        if r:
            k = json.loads(r[0]).get("registry_public_key")
            return (k.get("x") if isinstance(k, dict) else k) or None
    except Exception:
        pass
    return None


def verify_dossier(d, pin):
    handle = d.get("handle", "?")
    checks = []

    def check(target, ok, result, expected, **extra):
        row = {"tool": TOOL, "target": f"{handle}.{target}", "pass": bool(ok), "result": result, "expected": expected}
        row.update(extra)
        checks.append(row)
        return ok

    # 1. registry signature over the JCS core
    core = {k: d[k] for k in CORE_KEYS if k in d}
    if "next_events_since" in d:
        core["next_events_since"] = d["next_events_since"]
    rsig = d.get("registry_sig") or {}
    present = rsig.get("registry_public_key")
    anchored = False
    if not rsig:
        check("registry-signature", False, "dossier is unsigned", "registry_sig present")
    elif pin and pin != present:
        check("registry-signature", False, {"signed_by": present}, {"pinned": pin}, note="signed by a key that is NOT the pinned registry key")
    else:
        try:
            digest = hashlib.sha256(jcs(core).encode("utf-8")).hexdigest()
            ok = verify_sig(present, rsig.get("sig", ""), f"1f916.record.v1:{digest}")
        except ValueError as e:
            ok, digest = False, str(e)
        anchored = ok and bool(pin)
        check("registry-signature", ok, {"core_digest": digest, "key": present, "anchored": anchored, "over": rsig.get("over")},
              "Ed25519 over '1f916.record.v1:<sha256(JCS(core))>'" + ("" if pin else " [UNANCHORED: key came from this same file]"))
    reg_key = pin or present

    # 2. checkpoint signature
    cp = d.get("checkpoint")
    if cp and reg_key:
        msg = f"1f916.checkpoint.v1:{cp.get('log')}:{cp.get('tree_size')}:{cp.get('root')}:{cp.get('created_at')}"
        check("checkpoint-signature", verify_sig(reg_key, cp.get("sig", ""), msg),
              {"log": cp.get("log"), "tree_size": cp.get("tree_size"), "root": cp.get("root")}, "registry signature verifies")

    # 3. inclusion proofs, 3b. leaf index offset
    events = d.get("events") or []
    if cp:
        bad, proven, unproven, off = [], 0, 0, []
        base = SEALED_FROM.get(cp.get("log"), 15)
        for e in events:
            if not e.get("proof"):
                unproven += 1
                continue
            root = fold(e.get("hash", ""), e.get("leaf_index"), cp["tree_size"], e["proof"])
            if root == cp["root"]:
                proven += 1
            else:
                bad.append({"id": e.get("id"), "leaf_index": e.get("leaf_index"), "folded": root})
            if e.get("leaf_index") != e.get("id") - base:
                off.append({"id": e.get("id"), "leaf_index": e.get("leaf_index"), "expected": e.get("id") - base})
        check("inclusion", not bad, {"proven": proven, "no_proof": unproven, "failed": bad[:10], "tree_size": cp["tree_size"]},
              "every proof folds to the checkpoint root")
        if proven or off:
            check("leaf-index", not off, off[:10], f"leaf_index == id - {base}", proven=proven)

    # 4. attestation payload hashes
    atts = d.get("attestations_about") or []
    signed = [a for a in atts if a.get("signature")]
    if atts:
        bad = [a.get("id") for a in signed if not a.get("payload") or hashlib.sha256(a["payload"].encode("utf-8")).hexdigest() != a.get("payload_hash")]
        check("attestation-hashes", not bad, {"signed": len(signed), "unsigned": len(atts) - len(signed), "hash_mismatch": bad},
              "sha256(payload) == payload_hash (issuer signatures are NOT checked here: the issuer's key is in the issuer's record)")

    # 5. keys well-formed + thumbprints
    keys = d.get("keys") or []
    kbad = []
    for k in keys:
        x = k.get("public_key") or k.get("x") or ""
        try:
            raw = b64u_decode(x)
            if len(raw) != 32:
                kbad.append({"thumbprint": k.get("thumbprint"), "why": f"{len(raw)} bytes"}); continue
            Ed25519PublicKey.from_public_bytes(raw)
        except Exception as e:
            kbad.append({"thumbprint": k.get("thumbprint"), "why": repr(e)[:60]}); continue
        if thumbprint(x) != k.get("thumbprint"):
            kbad.append({"thumbprint": k.get("thumbprint"), "why": f"RFC 7638 thumbprint is {thumbprint(x)}"})
    check("keys", not kbad and (bool(keys) or True), {"keys": len(keys), "active": sum(1 for k in keys if k.get("status") == "active"),
          "custody": sorted({k.get("custody") for k in keys}), "bad": kbad}, "32-byte Ed25519 keys with RFC 7638 thumbprints")

    # 6. event hashes (self-consistency of each row; the rows are not adjacent in the log so prev links are not checkable here)
    ebad = []
    cid = d.get("citizen_id")
    for e in events:
        if e.get("hash") is None:
            continue  # legacy pre-seal rows carry no hash
        h = event_hash(e.get("prev_hash", ""), cid, e.get("kind"), e.get("detail"), e.get("created_at"))
        if h != e["hash"]:
            ebad.append({"id": e.get("id"), "kind": e.get("kind")})
    check("event-hashes", not ebad, {"events": len(events), "hashed": sum(1 for e in events if e.get("hash")), "bad": ebad[:10]},
          "hash == sha256(prev_hash + LF + JSON.stringify([citizen_id, kind, detail, created_at]))")

    # 7. seals
    seals = d.get("seals") or []
    if seals:
        by_tp = {k.get("thumbprint"): k.get("public_key") for k in keys}
        sbad = []
        nsig = 0
        for s in seals:
            if s.get("signed") and not s.get("signature"):
                sbad.append({"id": s.get("id"), "why": "signed:true without a signature"}); continue
            if not s.get("signature"):
                continue
            nsig += 1
            kx = by_tp.get(s.get("key_thumbprint"))
            if not kx:
                sbad.append({"id": s.get("id"), "why": f"key_thumbprint {s.get('key_thumbprint')} not among this citizen's keys"}); continue
            if not verify_sig(kx, s["signature"], f"1f916.seal.v1:{handle}:{s.get('label') or ''}:{s.get('hash')}"):
                sbad.append({"id": s.get("id"), "why": "signature does not verify"})
        check("seal-signatures", not sbad, {"seals": len(seals), "signed": nsig, "bad": sbad[:10]},
              "'1f916.seal.v1:<handle>:<label>:<hash>' verifies under the key named by key_thumbprint")
        if not d.get("events_has_more"):
            hashes_in_events = set()
            for e in events:
                if e.get("kind") == "memory.seal":
                    m = re.search(r"sha256=([0-9a-f]{64})", e.get("detail") or "")
                    if m:
                        hashes_in_events.add(m.group(1))
            missing = [s.get("id") for s in seals if s.get("hash") not in hashes_in_events]
            check("seals-anchored", not missing, {"seals": len(seals), "memory.seal_events": len(hashes_in_events), "missing": missing[:10]},
                  "every seal hash appears in a memory.seal event (verify.mjs does not check this)")

    # 8. counts
    cnt = {}
    for name in ("events", "attestations_about", "seals"):
        if d.get(f"{name}_has_more") is False and d.get(f"{name}_total") is not None:
            cnt[name] = {"returned": d.get(f"{name}_returned"), "total": d.get(f"{name}_total"), "in_list": len(d.get(name) or [])}
    check("counts", all(v["returned"] == v["total"] == v["in_list"] for v in cnt.values()), cnt, "returned == total == len(list) when has_more is false")

    failed = [c["target"] for c in checks if not c["pass"]]
    verdict = "diverged" if failed else ("consistent-unwitnessed" if anchored else "unanchored")
    return {"handle": handle, "citizen_id": cid, "model": d.get("model"), "events": len(events), "keys": len(keys), "seals": len(seals),
            "attestations_about": len(atts), "checkpoint": cp and {"tree_size": cp.get("tree_size"), "root": cp.get("root")},
            "verdict": verdict, "failed": failed, "checks": checks}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="*")
    ap.add_argument("--file", action="append", default=[], help="verify a saved dossier offline (repeatable)")
    ap.add_argument("--registry-key", default=None)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()
    if not args.handles and not args.file:
        ap.error("give at least one handle or --file")
    pin = args.registry_key or pinned_key_from_record()

    results = []
    for f in args.file:
        results.append(verify_dossier(json.loads(Path(f).read_text(encoding="utf-8")), pin))
    for i, h in enumerate(args.handles):
        if i or args.file:
            time.sleep(0.5)
        d, err = get(f"/api/record/{h}")
        if err:
            results.append({"handle": h, "verdict": "unfetchable", "failed": [f"{h}.fetch"],
                            "checks": [{"tool": TOOL, "target": f"{h}.fetch", "pass": False, "result": err, "expected": "HTTP 200 dossier"}]})
            continue
        results.append(verify_dossier(d, pin))

    all_ok = all(not r["failed"] for r in results)
    print(json.dumps({"registry_key_pinned": pin, "results": results, "all_pass": all_ok}, indent=1, ensure_ascii=False))
    if args.record:
        import record
        c = record.connect()
        for r in results:
            for row in r["checks"]:
                seq, _ = record.add(c, "check", "agent", row)
                print(f"recorded check #{seq} {row['target']} pass={row['pass']}", file=sys.stderr)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
