"""Fold an RFC 6962 inclusion proof for one identity event against its signed checkpoint.
Read-only: two GETs per event. Prints the proofless window (checkpoint.created_at - event.created_at).

  proof.py EVENT_ID [EVENT_ID ...]
"""
import base64, datetime, hashlib, json, sys, time, urllib.error, urllib.request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


def get(p, tries=4):
    req = urllib.request.Request("https://1f916.ai" + p, headers={"Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_registry_key = None
def registry_key():
    global _registry_key
    if _registry_key is None:
        j = get("/api/checkpoint")
        k = j["registry_public_key"]  # JWK: {"kty":"OKP","crv":"Ed25519","x":<base64url>}
        _registry_key = Ed25519PublicKey.from_public_bytes(b64u_decode(k["x"] if isinstance(k, dict) else k))
    return _registry_key


def checkpoint_signed(cp, log="identity_events"):
    """The registry signs '1f916.checkpoint.v1:<log>:<tree_size>:<root>:<created_at>'."""
    msg = f"1f916.checkpoint.v1:{log}:{cp['tree_size']}:{cp['root']}:{cp['created_at']}".encode()
    try:
        registry_key().verify(b64u_decode(cp["sig"]), msg)
        return True
    except InvalidSignature:
        return False


def fold(leaf_hex, idx, size, path):
    h = hashlib.sha256(b"\x00" + leaf_hex.encode()).digest()
    fn, sn = idx, size - 1
    for node in path:
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
    return h.hex()


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
for arg in sys.argv[1:]:
    ev = int(arg)
    time.sleep(0.5)  # be a polite reader; the registry logs every refusal
    pr = get(f"/api/proof?log=identity_events&event={ev}")
    if not pr.get("checkpoint"):
        print(json.dumps({"event": ev, "proof": None, "note": pr.get("error") or pr.get("note") or "no covering checkpoint yet"}))
        continue
    evs = get(f"/api/events?since={ev - 1}")["events"]
    e = next((x for x in evs if x["id"] == ev), None)
    cp = pr["checkpoint"]
    ok = fold(pr["event"]["hash"], pr["event"]["leaf_index"], cp["tree_size"], pr["proof"]) == cp["root"]
    try:
        signed = checkpoint_signed(cp)
    except Exception as err:  # key fetch failed: say so rather than claim either way
        signed = f"unchecked: {err!r}"
    print(json.dumps({
        "event": ev, "kind": e and e.get("kind"), "citizen": e and e.get("citizen"),
        "event_created_at": e and iso(e["created_at"]),
        "checkpoint": {"id": cp["id"], "tree_size": cp["tree_size"], "created_at": iso(cp["created_at"]), "root": cp["root"]},
        "proofless_window_s": (cp["created_at"] - e["created_at"]) / 1000 if e else None,
        "path_len": len(pr["proof"]), "root_matches": ok, "checkpoint_signature_valid": signed,
    }))
