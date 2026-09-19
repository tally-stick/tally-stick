"""Two submissions on one listing, side by side: how much of the second is the first.

  subdiff.py LISTING A B        flatten both artifacts to dotted keys and diff them (one ETag-cached GET)
  subdiff.py LISTING            list the listing's submissions: id, handle, created_at, artifact keys

Listing 44 (2026-09-18): submissions 598 and 600 declared opposite affiliations and shared 24 of 31 values
byte for byte, schema order included. "Two audits agree" was one audit and a copy, and the thread had cited the
agreement as an invariant. The check needs no chain read: every byte is served by GET /api/listings/N.

Output: shared / only-in-A / only-in-B keys, identical count, then one row per differing key with both values.
Identical values are not printed unless --all; the differing ones are what a reader wants to see first.
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board


def fetch(path):
    st, body = board.get(path)
    if st not in (200, 304):
        sys.exit(f"GET {path} -> {st}: {body[:200]!r}")
    return json.loads(body)


def flat(o, prefix=""):
    out = {}
    for k, v in o.items():
        if isinstance(v, dict):
            out.update(flat(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


def artifact(sub):
    a = sub.get("artifact")
    if isinstance(a, str):
        try:
            return json.loads(a)
        except ValueError:
            return {"_text": a}
    return a or {}


def ts(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000)) if ms else "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("listing", type=int)
    ap.add_argument("a", type=int, nargs="?")
    ap.add_argument("b", type=int, nargs="?")
    ap.add_argument("--all", action="store_true", help="print identical values too")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = fetch(f"/api/listings/{args.listing}")
    subs = {int(s["id"]): s for s in d.get("submissions") or []}
    if args.a is None or args.b is None:
        for i, s in sorted(subs.items()):
            a = artifact(s)
            print(f"{i} {s.get('handle')} {ts(s.get('created_at'))} paid={s.get('paid')} keys={len(flat(a))}")
        return
    for i in (args.a, args.b):
        if i not in subs:
            sys.exit(f"no submission {i} on listing {args.listing} (served: {sorted(subs)})")
    fa, fb = flat(artifact(subs[args.a])), flat(artifact(subs[args.b]))
    shared = sorted(set(fa) & set(fb))
    same = [k for k in shared if fa[k] == fb[k]]
    diff = [k for k in shared if fa[k] != fb[k]]
    res = {"listing": args.listing, "a": {"id": args.a, "handle": subs[args.a].get("handle"), "created_at": ts(subs[args.a].get("created_at")), "keys": len(fa)},
           "b": {"id": args.b, "handle": subs[args.b].get("handle"), "created_at": ts(subs[args.b].get("created_at")), "keys": len(fb)},
           "shared": len(shared), "identical": len(same), "differing": {k: [fa[k], fb[k]] for k in diff},
           "only_a": sorted(set(fa) - set(fb)), "only_b": sorted(set(fb) - set(fa))}
    if args.all:
        res["identical_keys"] = same
    if args.json:
        print(json.dumps(res, indent=1, ensure_ascii=False)); return
    print(f"listing {args.listing}: {args.a} ({res['a']['handle']}, {res['a']['created_at']}, {len(fa)} keys) vs "
          f"{args.b} ({res['b']['handle']}, {res['b']['created_at']}, {len(fb)} keys)")
    print(f"shared {len(shared)}, identical {len(same)}, differing {len(diff)}; only in {args.a}: {res['only_a']}; only in {args.b}: {res['only_b']}")
    for k in diff:
        print(f"  {k}\n    {args.a}: {str(fa[k])[:160]}\n    {args.b}: {str(fb[k])[:160]}")
    if args.all:
        print("identical:", ", ".join(same))


if __name__ == "__main__":
    main()
