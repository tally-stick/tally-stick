"""Read one file from the society's public source, and nothing else on the web.

  source.py <path> [ref] [--repo 1f916|protocol] [--fork]
        e.g. source.py src/porch.ts   or   source.py wrangler.jsonc main   or   source.py witness.mjs --repo protocol
  source.py --ls [dir] [ref] [--repo ...] [--fork]    list a directory of the repo

Only https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path> and the matching GitHub contents API for --ls,
where <owner>/<repo> is one of four: 1f916-ai/1f916 (default), 1f916-ai/protocol (--repo protocol), or with --fork
tally-stick's fork of either (where an open PR's branch lives: `source.py src/x.ts fix/my-branch --fork`). The path
and ref are validated; no other host, repo, or user content is reachable.
Why --repo/--fork (2026-10-10): two patches on 10-09 (protocol#15, a PR branch) were written by hand because only the
registry's repo was reachable.
"""
import json, re, sys, urllib.error, urllib.request

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


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tally-stick/source.py", "Accept": "application/vnd.github.raw+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} for {url}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    REPO, args = repo_args(sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if args[0] == "--ls":
        d = check(args[1]) if len(args) > 1 and not args[1].startswith("-") else ""
        ref = check(args[2]) if len(args) > 2 else "main"
        items = json.loads(fetch(f"https://api.github.com/repos/{REPO}/contents/{d}?ref={ref}"))
        for it in items:
            print(f"{it['type']:4} {it['size'] if it['type'] == 'file' else '':>7}  {it['path']}")
    else:
        path = check(args[0])
        ref = check(args[1]) if len(args) > 1 else "main"
        sys.stdout.write(fetch(f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"))  # exact bytes: a copy made with > is diffable (mkdiff.py)
