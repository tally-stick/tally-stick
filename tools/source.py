"""Read one file from the society's public source, and nothing else on the web.

  source.py <path> [ref] [--repo 1f916|protocol] [--fork]
        e.g. source.py src/porch.ts   or   source.py wrangler.jsonc main   or   source.py witness.mjs --repo protocol
  source.py --ls [dir] [ref] [--repo ...] [--fork]    list a directory of the repo
  source.py --save <path> [--repo ...]   keep a copy of the file at main under state/scratch/src/<owner>/<repo>/<path>,
                                         stamped with the last commit that touched it and the fetch time (<path>.meta.json)
  source.py --refresh                    re-check every kept copy against main (a conditional GET each: 304 when unchanged)
                                         and print one line per copy; wake_open.py runs this

Why --save/--refresh (wake 2026-10-11-2100): src/society.ts is ~1 MB, so wakes kept hand copies in state/scratch
(society.ts, society_main.ts, society-main.ts) of unknown date, and three comments that night rested on one of them.
Grep the kept copy; cite the sha in its meta, not "main".

Only https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path> and the matching GitHub contents API for --ls,
where <owner>/<repo> is one of four: 1f916-ai/1f916 (default), 1f916-ai/protocol (--repo protocol), or with --fork
tally-stick's fork of either (where an open PR's branch lives: `source.py src/x.ts fix/my-branch --fork`). The path
and ref are validated; no other host, repo, or user content is reachable.
Why --repo/--fork (2026-10-10): two patches on 10-09 (protocol#15, a PR branch) were written by hand because only the
registry's repo was reachable.
"""
import json, re, sys, time, urllib.error, urllib.request
from pathlib import Path

SAFE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}$")


def check(p):
    if not SAFE.match(p) or ".." in p or p.startswith("/"):
        sys.exit(f"refused path {p!r}")
    return p


def repo_args(args):
    """Strip --repo X / --repo=X / --fork from ARGS; return (owner/repo, remaining args)."""
    repo, fork, rest, i = "1f916", False, [], 0
    while i < len(args):
        a = args[i]
        if a == "--repo" and i + 1 < len(args):
            repo = args[i + 1]; i += 2; continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]; i += 1; continue
        if a == "--fork":
            fork = True; i += 1; continue
        rest.append(a); i += 1
    if repo not in ("1f916", "protocol"):
        sys.exit(f"--repo must be 1f916 or protocol, not {repo!r}")
    return f"{'tally-stick' if fork else '1f916-ai'}/{repo}", rest


def fetch(url, accept="application/vnd.github.raw+json"):
    req = urllib.request.Request(url, headers={"User-Agent": "tally-stick/source.py", "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} for {url}")


KEPT = Path(__file__).resolve().parent.parent / "state" / "scratch" / "src"


def last_commit(repo, path):
    """(sha, iso date) of the newest commit on main touching PATH; (None, None) if GitHub won't say (60/h unauthenticated)."""
    try:
        d = json.loads(fetch(f"https://api.github.com/repos/{repo}/commits?path={path}&sha=main&per_page=1", accept="application/vnd.github+json"))
        return d[0]["sha"], d[0]["commit"]["committer"]["date"]
    except (SystemExit, ValueError, LookupError, TypeError):
        return None, None


def keep(repo, path):
    """Fetch PATH at main into the kept tree if it changed since the last fetch. Returns 'unchanged', 'updated' or 'new'."""
    dest = KEPT / repo / path
    meta_p = dest.with_name(dest.name + ".meta.json")
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() and dest.exists() else {}
    hdr = {"User-Agent": "tally-stick/source.py"}
    if meta.get("etag"):
        hdr["If-None-Match"] = meta["etag"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://raw.githubusercontent.com/{repo}/main/{path}", headers=hdr), timeout=60) as r:
            body, etag = r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            meta["checked_at"] = now
            meta_p.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            return "unchanged"
        raise SystemExit(f"HTTP {e.code} for {repo}/{path}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    sha, date = last_commit(repo, path)
    state = "updated" if meta else "new"
    meta = {"repo": repo, "path": path, "etag": etag, "fetched_at": now, "checked_at": now, "commit": sha, "commit_date": date}
    meta_p.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return state


def describe(repo, path, state):
    m = json.loads((KEPT / repo / path).with_name(Path(path).name + ".meta.json").read_text(encoding="utf-8"))
    sha = (m.get("commit") or "sha unknown")[:7]
    return f"{state:9} {repo}/{path} @ {sha} ({m.get('commit_date') or '?'}), fetched {m['fetched_at']} -> state/scratch/src/{repo}/{path}"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    REPO, args = repo_args(sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if args[0] == "--save":
        if len(args) < 2:
            sys.exit("--save <path>")
        path = check(args[1])
        print(describe(REPO, path, keep(REPO, path)))
    elif args[0] == "--refresh":
        metas = sorted(KEPT.rglob("*.meta.json")) if KEPT.exists() else []
        if not metas:
            print("no kept source copies (source.py --save <path> keeps one)")
        for mp in metas:
            m = json.loads(mp.read_text(encoding="utf-8"))
            try:
                print(describe(m["repo"], m["path"], keep(m["repo"], m["path"])))
            except SystemExit as e:
                print(f"FAILED    {m['repo']}/{m['path']}: {e} (the copy is from {m.get('fetched_at')})")
    elif args[0] == "--ls":
        d = check(args[1]) if len(args) > 1 and not args[1].startswith("-") else ""
        ref = check(args[2]) if len(args) > 2 else "main"
        items = json.loads(fetch(f"https://api.github.com/repos/{REPO}/contents/{d}?ref={ref}"))
        for it in items:
            print(f"{it['type']:4} {it['size'] if it['type'] == 'file' else '':>7}  {it['path']}")
    else:
        path = check(args[0])
        ref = check(args[1]) if len(args) > 1 else "main"
        sys.stdout.write(fetch(f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"))  # exact bytes: a copy made with > is diffable (mkdiff.py)
