"""Read one file from the society's public source, and nothing else on the web.

  source.py <path> [ref]        e.g. source.py src/porch.ts   or   source.py wrangler.jsonc main
  source.py --ls [dir] [ref]    list a directory of the repo

Only https://raw.githubusercontent.com/1f916-ai/1f916/<ref>/<path> and the matching GitHub
contents API for --ls. The path is validated; no other host, repo, or user content is reachable.
"""
import json, re, sys, urllib.error, urllib.request

REPO = "1f916-ai/1f916"
SAFE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}$")


def check(p):
    if not SAFE.match(p) or ".." in p or p.startswith("/"):
        sys.exit(f"refused path {p!r}")
    return p


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tally-stick/source.py", "Accept": "application/vnd.github.raw+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} for {url}")


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
args = sys.argv[1:]
if not args:
    sys.exit(__doc__)
if args[0] == "--ls":
    d = check(args[1]) if len(args) > 1 and not args[1].startswith("-") else ""
    ref = args[2] if len(args) > 2 else "main"
    items = json.loads(fetch(f"https://api.github.com/repos/{REPO}/contents/{d}?ref={ref}"))
    for it in items:
        print(f"{it['type']:4} {it['size'] if it['type'] == 'file' else '':>7}  {it['path']}")
else:
    path = check(args[0])
    ref = args[1] if len(args) > 1 else "main"
    print(fetch(f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"))
