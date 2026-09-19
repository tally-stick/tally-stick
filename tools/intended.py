"""Rebuild a thread the way its authors meant it, following a link the board serves and nothing follows.

The board caps comment depth at 6. A reply that would land deeper is reparented: the server moves
it up to an allowed ancestor (the served `parent_id`) and records where the author meant it to go
in `intended_parent_id` (null on every comment except the ones it moved). `GET /api/post/{id}`
serves both fields on every comment already; nothing on the board follows the link back to rebuild
the tree the author actually wrote. This does, from one ETag-cached GET of the thread (a few, if
the thread pages: `comments_note` gives a `since` cursor to follow while `has_more` is true).

  intended.py POST_ID            the thread as a tree, intended_parent_id preferred over parent_id
  intended.py POST_ID --moved    only the moved comments, as a table
  intended.py POST_ID --count    just the header line (cheapest use)
  intended.py POST_ID --json     {"post_id", "title", "comments_total", "moved", "unlinkable_note", "moved_rows"}

Comments before 2026-08-10T11:53Z (commit 285f258) predate reparenting: a too-deep reply was
refused outright rather than moved, so nothing that old carries intended_parent_id — see the
caveat line, printed once on every call. Orphans (an intended_parent_id naming a comment not in
this page of the thread) render at the top level with a note rather than being dropped; a loop in
the intended_parent_id chain is broken and reported the same way, never followed forever.
"""
import argparse, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board

sys.setrecursionlimit(5000)

CAVEAT = ("Note: replies before 2026-08-10T11:53Z (commit 285f258) could not be moved (a too-deep "
          "reply was refused outright), so nothing before then carries the link.")


def fetch(path):
    st, body = board.get(path)
    if st not in (200, 304):
        sys.exit(f"GET {path} -> {st}: {body[:200]!r}")
    return json.loads(body)


def load_thread(post_id):
    """All comment pages for a post, following comments_note's since cursor while has_more is true.
    Returns (post dict, flat comments list, comments_total)."""
    path = f"/api/post/{post_id}"
    d = fetch(path)
    post = d.get("post") or {}
    comments = list(d.get("comments") or [])
    while d.get("has_more"):
        since = d.get("next_since")
        if since is None:
            sys.exit(f"has_more is true but the response for {path} carries no next_since cursor — "
                      f"refusing to page by walking the archive from since=0")
        path = f"/api/post/{post_id}?since={since}"
        d = fetch(path)
        comments.extend(d.get("comments") or [])
    return post, comments, d.get("comments_total")


def date_only(created_at):
    if created_at is None:
        return "?"
    try:
        return datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def build(comments):
    """{parent_id_or_None: [child ids]} using intended_parent_id where set (and present in this
    thread) else parent_id, plus {id: missing intended_parent_id} for orphans and a list of broken
    cycles. Orphans and cycle members land under None (top level) so nothing served is dropped."""
    by_id = {c["id"]: c for c in comments}
    ids = set(by_id)
    eff_parent = {}
    orphans = {}
    for cid, c in by_id.items():
        ipid = c.get("intended_parent_id")
        if ipid is not None:
            if ipid in ids:
                eff_parent[cid] = ipid
            else:
                eff_parent[cid] = None
                orphans[cid] = ipid
        else:
            pid = c.get("parent_id")
            eff_parent[cid] = pid if (pid is None or pid in ids) else None

    cycles = []
    broken = set()
    for start in list(ids):
        if start in broken:
            continue
        chain, on_chain = [], set()
        cur = start
        while cur is not None:
            if cur in on_chain:
                cyc = chain[chain.index(cur):]
                cycles.append(cyc)
                broken.update(cyc)
                eff_parent[cur] = None  # sever the loop: render at top level instead of spinning
                break
            if cur in broken:
                break
            chain.append(cur)
            on_chain.add(cur)
            cur = eff_parent.get(cur)

    children = defaultdict(list)
    for cid in ids:
        children[eff_parent[cid]].append(cid)
    return children, by_id, orphans, cycles


def order_key(by_id):
    return lambda cid: (by_id[cid].get("created_at") or 0, cid)


def render_line(cid, by_id, orphans, depth):
    c = by_id[cid]
    body = (c.get("body") or "").replace("\n", " ").replace("\r", " ")[:70]
    line = f"{'  ' * depth}c{cid} · {c.get('author')} · {date_only(c.get('created_at'))} · {body}"
    ipid = c.get("intended_parent_id")
    if cid in orphans:
        line += f" [intended parent c{orphans[cid]} not in thread]"
    elif ipid is not None:
        line += f" [moved: landed under c{c.get('parent_id')}, meant for c{ipid}]"
    return line


def _walk(children, by_id, orphans, cid, depth, out_lines, depth_intended, visited):
    if cid in visited:
        return
    visited.add(cid)
    depth_intended[cid] = depth
    out_lines.append(render_line(cid, by_id, orphans, depth))
    for kid in sorted(children.get(cid, []), key=order_key(by_id)):
        _walk(children, by_id, orphans, kid, depth + 1, out_lines, depth_intended, visited)


def render_tree(children, by_id, orphans):
    out_lines, depth_intended, visited = [], {}, set()
    for cid in sorted(children.get(None, []), key=order_key(by_id)):
        _walk(children, by_id, orphans, cid, 0, out_lines, depth_intended, visited)
    return out_lines, depth_intended


def moved_rows(by_id, depth_intended):
    rows = []
    for cid, c in by_id.items():
        ipid = c.get("intended_parent_id")
        if ipid is None:
            continue
        rows.append({
            "id": cid,
            "landed_under": c.get("parent_id"),
            "meant_for": ipid,
            "depth_served": c.get("depth"),
            "depth_intended": depth_intended.get(cid),
        })
    rows.sort(key=lambda r: r["id"])
    return rows


def header_line(post_id, post, total, moved):
    return f"post {post_id} · {post.get('title')} · {total} comments · {moved} moved (intended_parent_id set)"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("post", type=int)
    ap.add_argument("--moved", action="store_true", help="only the moved comments, as a table")
    ap.add_argument("--count", action="store_true", help="only the header line")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    post, comments, total = load_thread(a.post)
    if total is None:
        total = len(comments)
    moved = sum(1 for c in comments if c.get("intended_parent_id") is not None)
    header = header_line(a.post, post, total, moved)

    children, by_id, orphans, cycles = build(comments)
    tree_lines, depth_intended = render_tree(children, by_id, orphans)
    rows = moved_rows(by_id, depth_intended)

    if a.json:
        print(json.dumps({
            "post_id": a.post, "title": post.get("title"), "comments_total": total, "moved": moved,
            "unlinkable_note": CAVEAT, "moved_rows": rows,
        }, indent=1, ensure_ascii=False))
        return

    if a.count:
        print(header)
        print(CAVEAT)
        return

    if a.moved:
        print(header)
        print("id | landed_under | meant_for | depth_served | depth_intended")
        for r in rows:
            print(f"{r['id']} | {r['landed_under']} | {r['meant_for']} | {r['depth_served']} | {r['depth_intended']}")
        print(CAVEAT)
        return

    print(header)
    for line in tree_lines:
        print(line)
    for cyc in cycles:
        print("Cycle broken in intended_parent_id chain: " + " -> ".join(f"c{i}" for i in cyc))
    print(CAVEAT)


if __name__ == "__main__":
    main()
