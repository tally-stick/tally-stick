"""What landed in the society's repository between two times, and what each commit said.

  commits.py --since 2026-09-14T01:00Z [--until 2026-09-14T04:00Z] [--path .github/workflows/witness.yml] [--n 50]
  commits.py --sha e3b9a338                the one commit: message, files, stats

Answers "did the fix land in code?" (CLAUDE.md: follow a finding; the provenance question in #2124) without a git
checkout or an approval: one GET to /repos/1f916-ai/1f916/commits per 100 commits, ETag-cached so a repeat is a 304,
drawn from the same unauthenticated 60/h budget prs.py tracks (state/prs.json rate; below RESERVE it refuses).
Witness commits ("witness: <ts>", ~288/day) are folded into one count line unless --witness is passed, because the
question is always about the other commits.

Output, one line per commit: sha7  time  author  subject. --json prints the rows.
"""
import argparse, json, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import prs  # API, HEADERS, RESERVE, load/save of the rate budget

ETAGS = STATE / "commits-etags.json"


def load_etags():
    try:
        return json.loads(ETAGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get(st, path):
    """One conditional GET. Returns (status, json). 304 answers from the cache; the rate budget lands in prs.json."""
    et = load_etags()
    hdr = dict(prs.HEADERS)
    cached = et.get(path)
    if cached and cached.get("etag"):
        hdr["If-None-Match"] = cached["etag"]
    req = urllib.request.Request(prs.API + path, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            st["rate"] = {"remaining": int(r.headers.get("X-RateLimit-Remaining", -1)),
                          "reset": int(r.headers.get("X-RateLimit-Reset", 0)), "at": prs.iso(prs.now())}
            body = json.loads(r.read())
            et[path] = {"etag": r.headers.get("ETag"), "body": body}
            ETAGS.write_text(json.dumps(et, ensure_ascii=False), encoding="utf-8")
            return r.status, body
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return 304, cached["body"]
        st["rate"] = {"remaining": int(e.headers.get("X-RateLimit-Remaining", -1)),
                      "reset": int(e.headers.get("X-RateLimit-Reset", 0) or 0), "at": prs.iso(prs.now())}
        if e.code in (403, 429):
            st["backoff_until"] = st["rate"]["reset"] or int(time.time()) + 3600
        return e.code, {"error": e.read()[:300].decode(errors="replace")}


def budget_ok(st):
    if st.get("backoff_until") and time.time() < st["backoff_until"]:
        return f"backing off until {st['backoff_until']}"
    rate = st.get("rate") or {}
    if rate.get("remaining", 99) < prs.RESERVE and time.time() < rate.get("reset", 0):
        return f"GitHub budget {rate['remaining']} < {prs.RESERVE}; wait for reset"
    return None


def norm(ts):
    if not ts:
        return None
    ts = ts.strip()
    if len(ts) == 10:
        ts += "T00:00:00Z"
    if ts.endswith("Z") and len(ts) == 17:
        ts = ts[:-1] + ":00Z"
    return ts


def rows_of(commits):
    out = []
    for c in commits:
        msg = c["commit"]["message"]
        out.append({"sha": c["sha"], "at": c["commit"]["committer"]["date"], "author": (c.get("author") or {}).get("login")
                    or c["commit"]["author"]["name"], "subject": msg.split("\n", 1)[0], "message": msg,
                    "witness": msg.startswith("witness: ")})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--path")
    ap.add_argument("--sha"); ap.add_argument("--n", type=int, default=100, help="max commits to fetch (100 per request)")
    ap.add_argument("--witness", action="store_true", help="list the witness commits instead of folding them into a count")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    st = prs.load()
    why = budget_ok(st)
    if why:
        sys.exit(why)
    if a.sha:
        code, j = get(st, f"/commits/{a.sha}")
        prs.save(st)
        if code not in (200, 304):
            sys.exit(f"GET failed ({code}): {j.get('error', '')[:200]}")
        files = [{"file": f["filename"], "status": f["status"], "+": f.get("additions"), "-": f.get("deletions")} for f in j.get("files", [])]
        if a.json:
            print(json.dumps({"sha": j["sha"], "at": j["commit"]["committer"]["date"], "message": j["commit"]["message"], "files": files}, indent=1))
        else:
            print(f"{j['sha'][:7]}  {j['commit']['committer']['date']}  {(j.get('author') or {}).get('login') or j['commit']['author']['name']}")
            print(j["commit"]["message"].rstrip(), "\n")
            for f in files:
                print(f"  {f['status']:8s} +{f['+']}/-{f['-']}  {f['file']}")
        return
    if not a.since:
        sys.exit("--since is required (ISO time, e.g. 2026-09-14T01:00Z), or --sha")
    q = f"?sha=main&per_page={min(a.n, 100)}&since={norm(a.since)}"
    if a.until:
        q += f"&until={norm(a.until)}"
    if a.path:
        q += f"&path={a.path}"
    code, j = get(st, "/commits" + q)
    prs.save(st)
    if code not in (200, 304):
        sys.exit(f"GET failed ({code}): {j.get('error', '')[:200]}")
    rows = rows_of(j)
    wit = [r for r in rows if r["witness"]]
    shown = rows if a.witness else [r for r in rows if not r["witness"]]
    if a.json:
        print(json.dumps({"since": norm(a.since), "until": norm(a.until), "path": a.path, "fetched": len(rows), "witness_commits": len(wit),
                          "commits": shown, "cached": code == 304}, indent=1, ensure_ascii=False))
        return
    for r in sorted(shown, key=lambda r: r["at"]):
        print(f"{r['sha'][:7]}  {r['at']}  {r['author']:<18s}  {r['subject'][:110]}")
    tail = f"{len(shown)} commits shown, {len(wit)} witness commits folded" if not a.witness else f"{len(shown)} commits"
    print(f"-- {tail}; window {norm(a.since)} .. {norm(a.until) or 'now'}; {'304 from cache' if code == 304 else '200 fetched'}; "
          f"GitHub budget left {st['rate'].get('remaining')}" + (f"; more than {a.n} in window, narrow it" if len(rows) >= min(a.n, 100) else ""))


if __name__ == "__main__":
    main()
