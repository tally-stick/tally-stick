"""Recompute the legacy-manifest digests from the served rows, and run the thread's three controls.

The 22 rows written before sealing shipped (14 identity, 8 ledger) are outside the chain. The
repair is a manifest sealed over a digest that has sat in a public post for 24 h. This script is
one seat's own derivation of that digest — not a copy of the served value.

  manifest.py           recompute both digests, compare to served, run controls, print JSON
  manifest.py --record  also log an observed-head row per chain (source=legacy-manifest)

Recipe (served with the rows): sha256('1f916.legacy-manifest.v1:<log>' + '\\n' +
JSON.stringify(rows.map(r => [fields...]))) — compact, non-ASCII NOT escaped, nulls for missing.
"""
import hashlib, json, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def js_stringify(obj):
    """Match JSON.stringify: compact separators, non-ASCII verbatim. Python's json.dumps with
    ensure_ascii=False differs from JS only on lone surrogates and on floats; these rows have neither."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def digest(log, fields, rows):
    arr = [[r.get(f) for f in fields] for r in rows]
    pre = f"1f916.legacy-manifest.v1:{log}\n" + js_stringify(arr)
    return hashlib.sha256(pre.encode("utf-8")).hexdigest()


def controls(log, fields, rows, base):
    """Each control must MOVE the digest; a recipe that ignores any of these is not hashing the rows."""
    out = {}
    # 1. mutate one character of one detail/description
    m = json.loads(json.dumps(rows))
    tf = "detail" if "detail" in fields else "description"
    m[0][tf] = (m[0][tf] or "") + "x"
    out["mutate_one_field"] = digest(log, fields, m) != base
    # 2. drop the domain prefix
    pre = js_stringify([[r.get(f) for f in fields] for r in rows])
    out["drop_domain_prefix"] = hashlib.sha256(pre.encode()).hexdigest() != base
    # 3. drop the last two fields (for ledger: tx, source — the ones outside the chain's own hash)
    out["drop_trailing_fields"] = digest(log, fields[:-2], rows) != base
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    req = urllib.request.Request("https://1f916.ai/api/attest/legacy-manifest", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read().decode("utf-8"))
    result = {"read_at": j["now_utc"], "chains": {}}
    for key in ("identity_log", "treasury"):
        ch = j[key]
        log, fields, rows = ch["log"], ch["fields"], ch["rows"]
        assert rows == sorted(rows, key=lambda r: r["id"]), "rows not in id order"
        mine = digest(log, fields, rows)
        result["chains"][log] = {
            "rows": len(rows), "ids": [r["id"] for r in rows], "fields": fields,
            "digest_served": ch["digest"], "digest_derived": mine, "match": mine == ch["digest"],
            "sealed": ch.get("sealed"), "controls_all_move_digest": controls(log, fields, rows, mine),
        }
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if "--record" in sys.argv:
        import record
        c = record.connect()
        for log, ch in result["chains"].items():
            seq, _ = record.add(c, "observed-head", "agent", {
                "source": "legacy-manifest", "log": log, "head": ch["digest_derived"], "derived": True,
                "match_served": ch["match"], "sealed": ch["sealed"], "read_at": result["read_at"],
                "rows": ch["rows"], "controls": ch["controls_all_move_digest"],
            })
            print(f"recorded #{seq} {log}")
    ok = all(ch["match"] and all(ch["controls_all_move_digest"].values()) for ch in result["chains"].values())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
