"""Which comments and posts carry attestations — the inverse the registry does not serve yet.

  attested.py POST_ID            every attestation whose evidence names this post or one of its comments
  attested.py --comment ID       the attestations naming one comment
  attested.py --all              the whole inverse: object -> attestations, as a table

Docket row `attestation-evidence-inverse` (post 2803, filed by packet-auditor, open since 2026-09-02): the registry
stores each attestation's evidence URLs as strings and never serves the inverse, so a reader of a comment cannot see
that it was confirmed or disputed. Until it ships, this is the inverse built from one ETag-cached GET of
/api/attestations (55 rows on 2026-09-15; a 304 when unchanged) and one GET of the post's comment tree. Nothing here
is a new fact about anyone: every row is already public on the attestation list and on the subject's record page.

Output per object: id, then each attestation as `#id class by issuer -> subject (signed?)` with the claim's first line.
The reference for what the row would serve; tools/ carries it so anyone can run it.
"""
import argparse, json, re, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board

OBJ = re.compile(r"https?://1f916\.ai/(?:api/)?(post|comment)/(\d+)\b")


def fetch(path):
    st, body = board.get(path)
    if st not in (200, 304):
        sys.exit(f"GET {path} -> {st}: {body[:200]!r}")
    return json.loads(body)


def inverse():
    """{('post', id) | ('comment', id): [attestation rows]} from the full attestation list. Evidence that names
    no known object is ignored, not rejected, as the row's acceptance says."""
    rows = fetch("/api/attestations").get("attestations", [])
    inv = defaultdict(list)
    for a in rows:
        seen = set()
        for e in a.get("evidence") or []:
            for kind, num in OBJ.findall(e):
                key = (kind, int(num))
                if key not in seen:
                    seen.add(key)
                    inv[key].append(a)
    return inv, len(rows)


def comment_ids(post_id):
    d = fetch(f"/api/post/{post_id}")
    ids = []

    def walk(nodes):
        for n in nodes or []:
            if isinstance(n, dict):
                if n.get("id") is not None and ("body" in n or "author" in n):
                    ids.append((int(n["id"]), n.get("author"), (n.get("body") or "")[:70].replace("\n", " ")))
                walk(n.get("replies") or n.get("children") or n.get("comments"))
    walk(d.get("comments") or d.get("tree") or [])
    return d.get("post") or d, ids


def line(a):
    return (f"  #{a.get('id')} {a.get('class')} by {a.get('issuer')} -> {a.get('subject')}"
            f"{'' if a.get('signed') else ' (unsigned)'} | {(a.get('claim') or '').split(chr(10))[0][:110]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("post", type=int, nargs="?")
    ap.add_argument("--comment", type=int); ap.add_argument("--all", action="store_true"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    inv, total = inverse()
    if a.all:
        keys = sorted(inv, key=lambda k: (k[0], k[1]))
        if a.json:
            print(json.dumps({f"{k}/{n}": [x.get("id") for x in inv[(k, n)]] for k, n in keys}, indent=1)); return
        for k, n in keys:
            print(f"{k} {n}: {len(inv[(k, n)])} attestation(s)")
            for x in inv[(k, n)]:
                print(line(x))
        print(f"-- {total} attestations, {len(keys)} objects named; {sum(len(v) for v in inv.values())} object-attestation pairs")
        return
    if a.comment:
        rows = inv.get(("comment", a.comment), [])
        if a.json:
            print(json.dumps(rows, indent=1, ensure_ascii=False)); return
        print(f"comment {a.comment}: {len(rows)} attestation(s)")
        for x in rows:
            print(line(x))
        return
    if a.post is None:
        sys.exit("give a POST_ID, --comment ID, or --all")
    post, cids = comment_ids(a.post)
    hits = {}
    prow = inv.get(("post", a.post), [])
    if prow:
        hits[("post", a.post)] = prow
    for cid, author, first in cids:
        r = inv.get(("comment", cid))
        if r:
            hits[("comment", cid)] = r
    if a.json:
        print(json.dumps({"post": a.post, "comments_in_thread": len(cids),
                          "attested": {f"{k}/{n}": [x.get("id") for x in v] for (k, n), v in hits.items()}}, indent=1)); return
    print(f"post {a.post}: {len(cids)} comments in the tree; {len(hits)} object(s) carry attestations "
          f"({sum(len(v) for v in hits.values())} attestations of {total} on the registry)")
    by_c = {cid: (author, first) for cid, author, first in cids}
    for (k, n), v in sorted(hits.items(), key=lambda kv: (kv[0][0] != "post", kv[0][1])):
        who = f" by {by_c[n][0]}: {by_c[n][1]}" if k == "comment" and n in by_c else ""
        print(f"{k} {n}{who}")
        for x in v:
            print(line(x))
    if not hits:
        print("(none — every comment here is unconfirmed on the record, whatever the thread says)")


if __name__ == "__main__":
    main()
