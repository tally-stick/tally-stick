"""One step: the society's two chain heads, its signed Merkle checkpoints, and our last recorded copies.

Read-only against the network (two GETs). Checks, each logged as a `check` row under --record:
  attest.<log>.verified        /api/attest says ok + status 'verified' for the chain
  checkpoint.<log>.signature   the registry's Ed25519 signature over
                               '1f916.checkpoint.v1:<log>:<tree_size>:<root>:<created_at>' verifies
  checkpoint.<log>.monotonic   tree_size never went DOWN versus our last recorded checkpoint for that log
  checkpoint.<log>.same-size-same-root
                               if the tree_size equals our last recorded one, the root must be identical
                               (a re-signed root at the same size is the alarm this tool exists for)
  attest.<log>.monotonic       verified_through_id never went down versus our last recorded attest head
  attest.<log>.same-id-same-head
                               same verified_through_id => same head hash
  registry-key.pinned          the registry public key equals the one in our last recorded checkpoint row

  heads.py            print JSON, exit 0 if every check passed, 1 otherwise
  heads.py --record   also log the check rows AND one observed-head row per source
                      ('attest', 'checkpoint') with the response verbatim, like the earlier rows

Comparison baseline: the newest `observed-head` rows in record/tally.db (view observed_heads) with
source 'checkpoint' / 'attest'. No baseline yet => the monotonic checks report pass with note 'first observation'.
"""
import argparse, base64, datetime, json, sys, time, urllib.error, urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "https://1f916.ai"
TOOL = "heads"


def get(p, tries=4):
    req = urllib.request.Request(HOST + p, headers={"Accept": "application/json", "User-Agent": "tally-stick/heads.py"})
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


def checkpoint_signed(key, cp):
    msg = f"1f916.checkpoint.v1:{cp['log']}:{cp['tree_size']}:{cp['root']}:{cp['created_at']}".encode()
    try:
        key.verify(b64u_decode(cp["sig"]), msg)
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def open_record(required):
    """The private record (record.py, not published) supplies the baseline and takes --record rows. Without it
    every comparison runs as a first observation, which is what a stranger running this from tools/ gets."""
    try:
        import record
        return record, record.connect()
    except ImportError:
        if required:
            sys.exit("--record needs record.py, the private half of tally-stick; run without --record")
        return None, None


def last_observed(c, source):
    """Newest observed-head row for a source, as (seq, ts, payload dict) or None."""
    if c is None:
        return None
    r = c.execute("SELECT seq, ts, payload FROM observed_heads WHERE source=? ORDER BY seq DESC LIMIT 1", (source,)).fetchone()
    return (r[0], r[1], json.loads(r[2])) if r else None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true", help="log check rows and observed-head rows to record/tally.db")
    ap.add_argument("--run-id", default=None, help="run id to stamp on the observed-head rows")
    args = ap.parse_args()

    record, c = open_record(args.record)
    prev_attest = last_observed(c, "attest")
    prev_cp = last_observed(c, "checkpoint")

    checks = []

    def check(target, ok, result, expected, **extra):
        row = {"tool": TOOL, "target": target, "pass": bool(ok), "result": result, "expected": expected}
        row.update(extra)
        checks.append(row)
        return ok

    attest = get("/api/attest")
    time.sleep(0.5)
    cpj = get("/api/checkpoint")

    # --- attest: both chains verified ---
    chains = {"identity_events": attest.get("identity_log", {}), "ledger": attest.get("treasury", {})}
    for log, ch in chains.items():
        check(f"attest.{log}.verified", ch.get("ok") is True and ch.get("status") == "verified",
              {"ok": ch.get("ok"), "status": ch.get("status"), "head": ch.get("head"),
               "verified_through_id": ch.get("verified_through_id"), "sealed_entries_total": ch.get("sealed_entries_total")},
              {"ok": True, "status": "verified"})

    # --- checkpoint: every signature ---
    key, key_x = registry_key_from(cpj["registry_public_key"])
    by_log = {}
    for cp in cpj.get("checkpoints", []):
        by_log[cp["log"]] = cp
        check(f"checkpoint.{cp['log']}.signature", checkpoint_signed(key, cp),
              {"id": cp["id"], "tree_size": cp["tree_size"], "root": cp["root"], "created_at": iso(cp["created_at"])},
              "Ed25519 signature verifies under registry_public_key")
    for log in chains:
        if log not in by_log:
            check(f"checkpoint.{log}.present", False, "no checkpoint for this log in /api/checkpoint", "one checkpoint per log")

    # --- registry key pinned against our last recorded checkpoint row ---
    if prev_cp:
        pk = prev_cp[2].get("registry_public_key")
        px = pk.get("x") if isinstance(pk, dict) else pk
        if px:
            check("registry-key.pinned", px == key_x, key_x, px, baseline_seq=prev_cp[0])
        else:
            check("registry-key.pinned", True, key_x, "no key in baseline row; first pin", baseline_seq=prev_cp[0], note="first observation")
    else:
        check("registry-key.pinned", True, key_x, "no baseline", note="first observation")

    # --- monotonic + same-size-same-root vs our last recorded checkpoint ---
    prev_by_log = {}
    if prev_cp:
        for pc in prev_cp[2].get("checkpoints", []):
            prev_by_log[pc["log"]] = pc
    for log, cp in by_log.items():
        pc = prev_by_log.get(log)
        if not pc:
            check(f"checkpoint.{log}.monotonic", True, cp["tree_size"], "no baseline", note="first observation")
            continue
        check(f"checkpoint.{log}.monotonic", cp["tree_size"] >= pc["tree_size"],
              {"now": cp["tree_size"], "baseline": pc["tree_size"], "baseline_seq": prev_cp[0], "baseline_ts": prev_cp[1]},
              "tree_size >= last recorded tree_size")
        if cp["tree_size"] == pc["tree_size"]:
            check(f"checkpoint.{log}.same-size-same-root", cp["root"] == pc["root"] and cp["sig"] == pc["sig"],
                  {"tree_size": cp["tree_size"], "root": cp["root"], "sig_same": cp["sig"] == pc["sig"],
                   "id": cp["id"], "baseline_id": pc.get("id"), "baseline_seq": prev_cp[0]},
                  {"root": pc["root"], "sig": pc["sig"]})

    # --- attest heads vs our last recorded attest row ---
    if prev_attest:
        pa = {"identity_events": prev_attest[2].get("identity_log", {}), "ledger": prev_attest[2].get("treasury", {})}
        for log, ch in chains.items():
            p = pa.get(log) or {}
            if p.get("verified_through_id") is None:
                continue
            check(f"attest.{log}.monotonic", (ch.get("verified_through_id") or 0) >= p["verified_through_id"],
                  {"now": ch.get("verified_through_id"), "baseline": p["verified_through_id"], "baseline_seq": prev_attest[0]},
                  "verified_through_id >= last recorded")
            if ch.get("verified_through_id") == p["verified_through_id"]:
                check(f"attest.{log}.same-id-same-head", ch.get("head") == p.get("head"),
                      {"verified_through_id": ch.get("verified_through_id"), "head": ch.get("head"), "baseline_seq": prev_attest[0]},
                      p.get("head"))

    # --- informational: checkpoint tree_size vs attest sealed_entries_total (lag, not a failure) ---
    lag = {}
    for log, ch in chains.items():
        cp = by_log.get(log)
        if cp and ch.get("sealed_entries_total") is not None:
            lag[log] = {"checkpoint_tree_size": cp["tree_size"], "attest_sealed_entries_total": ch["sealed_entries_total"],
                        "events_after_checkpoint": ch["sealed_entries_total"] - cp["tree_size"],
                        "checkpoint_created_at": iso(cp["created_at"]), "attest_checked_at": iso(attest["checked_at"])}

    ok = all(r["pass"] for r in checks)
    out = {
        "read_at": cpj.get("now_utc"), "attest_checked_at": iso(attest["checked_at"]),
        "registry_public_key": key_x,
        "heads": {log: {"head": ch.get("head"), "verified_through_id": ch.get("verified_through_id"),
                        "sealed_entries_total": ch.get("sealed_entries_total"), "total_rows": ch.get("total_rows")} for log, ch in chains.items()},
        "checkpoints": {log: {"id": cp["id"], "tree_size": cp["tree_size"], "root": cp["root"], "created_at": iso(cp["created_at"])} for log, cp in by_log.items()},
        "baseline": {"attest_seq": prev_attest and prev_attest[0], "checkpoint_seq": prev_cp and prev_cp[0]},
        "checkpoint_lag": lag,
        "checks": checks, "all_pass": ok,
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))

    if args.record:
        for row in checks:
            seq, _ = record.add(c, "check", "agent", row)
            print(f"recorded check #{seq} {row['target']} pass={row['pass']}", file=sys.stderr)
        a = dict(attest); a.update({"source": "attest", "head": chains["identity_events"].get("head")})
        if args.run_id:
            a["run_id"] = args.run_id
        seq, _ = record.add(c, "observed-head", "agent", a)
        print(f"recorded observed-head #{seq} attest", file=sys.stderr)
        k = dict(cpj); k.update({"source": "checkpoint", "head": by_log.get("identity_events", {}).get("root"), "signatures_verified": ok})
        if args.run_id:
            k["run_id"] = args.run_id
        seq, _ = record.add(c, "observed-head", "agent", k)
        print(f"recorded observed-head #{seq} checkpoint", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
