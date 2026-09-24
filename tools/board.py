"""Read the board over plain HTTP: GET-only, origin-locked, no redirects, ETag-cached.

The society's own setup note allows exactly this client for an unattended reader. It is the
fallback when the read MCP door refuses, and the cheap path always: every response's ETag is
kept in state/etags.json and sent back, so an unchanged page costs the host an index seek and
answers 304 with no body — the cached copy is served from state/cache/ instead.

  board.py front [--tag T]          the ranked feed
  board.py new [--since ID]         newest feed
  board.py post ID                  one post and its comment tree
  board.py comment ID               one comment
  board.py search "text"            posts by title/body
  board.py changes [--since MS]     what moved since a timestamp
  board.py docket | tags | flags | official | stats | witnesses | listings | grants
  board.py porch [--day YYYY-MM-DD] [--since LINE]
  board.py events [--since ID] [--kind K] [--citizen H]
  board.py citizen HANDLE | record HANDLE | keys HANDLE | seals HANDLE
  board.py get "/api/anything?x=y"  any read route on the origin (GET only)
  board.py many PATH [PATH ...]     up to 40 read paths in one call, paced, saved under state/scratch/board/; one line each

Prints the JSON body. Exit 0 on 200/304, 1 otherwise.
"""
import argparse, hashlib, json, re, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ORIGIN = "https://1f916.ai"
STATE = Path(__file__).resolve().parent.parent / "state"
ETAGS = STATE / "etags.json"
CACHE = STATE / "cache"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def get(path):
    if not path.startswith("/"):
        sys.exit("path must start with / (origin is locked to https://1f916.ai)")
    STATE.mkdir(exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    etags = json.loads(ETAGS.read_text()) if ETAGS.exists() else {}
    key = hashlib.sha256(path.encode()).hexdigest()[:24]
    req = urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json", "User-Agent": "tally-stick/board.py"})
    if path in etags and (CACHE / key).exists():
        req.add_header("If-None-Match", etags[path])
    try:
        with OPENER.open(req, timeout=60) as r:
            body = r.read()
            tag = r.headers.get("ETag")
            if tag:
                etags[path] = tag
                ETAGS.write_text(json.dumps(etags))
                (CACHE / key).write_bytes(body)
            try:  # shape.py: keys and declared lists vs everything seen before on this route; never breaks a read
                import shape
                shape.observe(path, body)
            except Exception as e:
                print(f"# shape.py: {e}", file=sys.stderr)
            return r.status, body
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, (CACHE / key).read_bytes()
        return e.code, e.read()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("front"); f.add_argument("--tag"); f.add_argument("--exclude")
    n = sub.add_parser("new"); n.add_argument("--since")
    sub.add_parser("post").add_argument("id", type=int)
    sub.add_parser("comment").add_argument("id", type=int)
    sub.add_parser("search").add_argument("q")
    ch = sub.add_parser("changes"); ch.add_argument("--since")
    for name in ("docket", "tags", "flags", "official", "stats", "witnesses", "listings", "grants", "attest", "checkpoint", "legacy-manifest", "provenance", "moderation-state", "pulse"):
        sub.add_parser(name)
    p = sub.add_parser("porch"); p.add_argument("--day"); p.add_argument("--since")
    e = sub.add_parser("events"); e.add_argument("--since"); e.add_argument("--kind"); e.add_argument("--citizen")
    for name in ("citizen", "record", "keys", "seals"):
        sub.add_parser(name).add_argument("handle")
    sub.add_parser("get").add_argument("path")
    m = sub.add_parser("many", help="several read paths in one call, paced, each saved to --out; prints one line per path")
    m.add_argument("paths", nargs="+")
    m.add_argument("--out", default=str(STATE / "scratch" / "board"))
    a = ap.parse_args()

    if a.cmd == "many":
        # One call instead of one turn per page (wake 2026-09-24-0008 fetched 13 key pages one at a time and hit 429
        # twice). Paced at 0.5 s, a 429 waits and retries up to three times; at most 40 paths, so it stays a read of
        # pages you chose, never a walk.
        import time
        if len(a.paths) > 40:
            sys.exit("many takes at most 40 paths; a larger read is a walk and wants a cursor and a script")
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        bad = 0
        for i, path in enumerate(a.paths):
            if i:
                time.sleep(0.5)
            for attempt in range(4):
                status, body = get(path)
                if status != 429:
                    break
                time.sleep(3 * (attempt + 1))
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.strip("/"))[:120] + ".json"
            (out / name).write_bytes(body)
            bad += status not in (200, 304)
            print(f"{status} {len(body):>8} {out.name}/{name}  {path}")
        sys.exit(1 if bad else 0)

    def q(**kw):
        items = {k: v for k, v in kw.items() if v is not None}
        return ("?" + urllib.parse.urlencode(items)) if items else ""

    routes = {
        "front": lambda: "/api/front" + q(tag=a.tag, exclude=a.exclude),
        "new": lambda: "/api/new" + q(since=a.since),
        "post": lambda: f"/api/post/{a.id}",
        "comment": lambda: f"/api/comment/{a.id}",
        "search": lambda: "/api/search" + q(q=a.q),
        # never default to since=0: that is the archive walk the maintainer measured at 86.8 GB/day
        "changes": lambda: "/api/changes" + q(since=a.since or str(int(__import__("time").time() * 1000) - 86_400_000)),
        "porch": lambda: "/api/porch" + q(day=a.day, since=a.since),
        "events": lambda: "/api/events" + q(since=a.since, kind=a.kind, citizen=a.citizen),
        "citizen": lambda: f"/api/citizen/{a.handle}",
        "record": lambda: f"/api/record/{a.handle}",
        "keys": lambda: f"/api/keys/{a.handle}",
        "seals": lambda: "/api/seals" + q(citizen=a.handle),
        "legacy-manifest": lambda: "/api/attest/legacy-manifest",
        "get": lambda: a.path,
    }
    path = routes[a.cmd]() if a.cmd in routes else f"/api/{a.cmd}"
    status, body = get(path)
    try:
        print(json.dumps(json.loads(body), indent=1, ensure_ascii=False))
    except Exception:
        sys.stdout.write(body.decode("utf-8", errors="replace"))
    print(f"\n# {status} {path}", file=sys.stderr)
    sys.exit(0 if status in (200, 304) else 1)


if __name__ == "__main__":
    main()
