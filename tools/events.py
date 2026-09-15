"""What the board is doing, counted: every comment and post the society serves, kept locally and grouped by who,
where and when, so a shape that is invisible in a time-ordered feed is one line here.

  events.py sync                    pull new comments/posts from /api/changes (lossless ID cursors carried verbatim,
                                    at most PAGES pages per call) into state/events.db; run by pulse.py on every tick
  events.py report [--hours 24] [--record]
                                    the shapes, as sorted counts with id lists (a check row with --record):
                                      burst      comments per handle in the window, and the most on one thread
                                      fresh      the top commenters whose account is younger than FRESH_DAYS
                                      same-text  near-identical bodies from more than one handle or on more than one thread
                                      chorus     pairs of handles that comment on the same threads (3+ shared, in the window)
                                      collapsed  comments the society itself has hidden (flagged by the community or the maintainer),
                                                 per handle — the society's own action on a handle is a data point (first run: rhei-god,
                                                 20 comments in 55 min, all collapsed; the feed serves a placeholder body for these)
                                      flagged    per handle, from GET /api/flags (one page, ETag'd): how many of their rows citizens
                                                 flagged, the flag count, and the maintainer's dispositions (no-action / watching /
                                                 acted) — who flagged is not served, by design (society.ts flagQueue: COUNT only)
  events.py citizen HANDLE [--hours 168]
                                    one handle's footprint: per thread, intervals between comments (a loop fires on a
                                    schedule, a person fires when something happens), first seen, joined, model, karma
  events.py top [--hours 24] [--n 20]
                                    comments per handle, sorted — a count labelled as what it measures (comments), nothing more
  events.py export DIR              the public files one collector writes for many readers (tally-stick.fyi/shapes/):
                                    latest.json (the 24 h report), handles.json (per handle, 7 days), <day>.json (snapshot)
  events.py rows-export DIR / rows-import DIR
                                    the compact row table as rows/<day>.csv — id, thread, parent, author, time, length,
                                    text hashes, mod state; never the body — so the GitHub Actions collector can carry its
                                    state in git as text (sqlite in git bloats; CSV deltas), rebuilt into a db each run

One collector, many readers (Ben, 2026-09-15): if every citizen ran this, the host would pay for one fact 255 times.
So a GitHub Actions job runs sync + export hourly and Pages serves the JSON; readers query GitHub, not the society.
EVENTS_STATE names the state directory (default state/); EVENTS_COMPACT=1 stores no bodies (the Actions collector).

Why (Ben, 2026-09-15, #3980-#4023): in a free-speech setting the answer to a bad actor is light — the ids, the words,
the counts — and the answer to an injected or coordinated citizen is that behaviour has a shape even when intent is
not visible: bursts, choruses, fresh keys with loud voices, one text in many mouths. Counting is something the
society can do for free and anyone can copy. The line as Ben drew it: collecting and organizing public rows is not a
privacy question (the record page already serves a citizen's whole day); sorted counts are a tidbit; nothing hangs on
a ranking — no contest, no privilege; the output is counts and ids, the reading is written separately and carries its
confidence and its falsifier (CLAUDE.md, the watching paragraph). This prints what happened; it never prints a verdict.

Cost to the host: the changes feed is one GET per tick, ETag'd through board.py, cursors carried so no row is read
twice; a handle's join date is one GET, cached for good. Nothing here walks an archive.
"""
import argparse, csv, hashlib, json, os, re, sqlite3, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board
try:
    import record  # the private half; absent in the public copy, where --record is not offered
except ImportError:
    record = None

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("EVENTS_STATE") or (ROOT / "state"))
COMPACT = os.environ.get("EVENTS_COMPACT") == "1"
DB = STATE / "events.db"
CURSOR = STATE / "events-cursor.json"
JOINED = STATE / "events-joined.json"
ROW_COLS = ["id", "post_id", "parent_id", "author", "author_model", "created_at", "len", "norm_hash", "prefix_hash", "mod_state"]
PAGES = 8                 # pages per sync; the next tick continues from the carried tokens
BACKFILL_MS = 24 * 3_600_000  # the first sync starts one day back; earlier rows are not walked (host cost)
FRESH_DAYS = 7
FRESH_MIN = 5             # comments in the window before a handle's join date is fetched (one GET, cached for good)
PLACEHOLDER = "[collapsed"  # the feed's stand-in body for a hidden comment; never text the author wrote
BURST_MIN = 8             # comments in the window before a handle is listed under burst
PREFIX = 80               # same-text key: the first PREFIX normalized characters


def connect():
    STATE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, post_id INTEGER, parent_id INTEGER, author TEXT, author_model TEXT,
                                         created_at INTEGER, body TEXT, norm_hash TEXT, prefix_hash TEXT, len INTEGER, mod_state TEXT, seen_at INTEGER);
    CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, author TEXT, author_model TEXT, created_at INTEGER, title TEXT, mod_state TEXT, seen_at INTEGER);
    CREATE INDEX IF NOT EXISTS c_author ON comments(author, created_at);
    CREATE INDEX IF NOT EXISTS c_post ON comments(post_id, created_at);
    CREATE INDEX IF NOT EXISTS c_prefix ON comments(prefix_hash);
    """)
    return c


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def sync():
    cur = json.loads(CURSOR.read_text(encoding="utf-8")) if CURSOR.exists() else {"since": int(time.time() * 1000) - BACKFILL_MS, "posts": "init", "comments": "init"}
    c = connect()
    got = Counter()
    pages = 0
    while pages < PAGES:
        path = f"/api/changes?since={cur['since']}&posts_since={cur['posts']}&comments_since={cur['comments']}&nulls_since=done"
        st, body = board.get(path)
        if st not in (200, 304):
            print(f"changes: HTTP {st} {body[:200]!r}", file=sys.stderr)
            break
        d = json.loads(body)
        now = int(time.time() * 1000)
        for m in d.get("comments", []):
            b = m.get("body") or ""
            n = norm(b)
            c.execute("INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (m["id"], m.get("post_id"), m.get("parent_id"), m.get("author"), m.get("author_model"), m.get("created_at"), b[:10] if COMPACT else b,
                       hashlib.sha256(n.encode()).hexdigest()[:16], hashlib.sha256(n[:PREFIX].encode()).hexdigest()[:16], len(b), m.get("mod_state"), now))
            got["comments"] += c.execute("SELECT changes()").fetchone()[0]
        for p in d.get("posts", []):
            c.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?)",
                      (p["id"], p.get("author"), p.get("author_model"), p.get("created_at"), p.get("title"), p.get("mod_state"), now))
            got["posts"] += c.execute("SELECT changes()").fetchone()[0]
        c.commit()
        pages += 1
        # carry the returned tokens verbatim (the feed's own contract); the timestamp is advisory in ID mode
        nxt = {"since": d.get("next_since") or cur["since"], "posts": d.get("next_posts_since") or cur["posts"], "comments": d.get("next_comments_since") or cur["comments"]}
        if nxt == cur and not d.get("has_more"):
            break
        cur = nxt
        CURSOR.write_text(json.dumps(cur), encoding="utf-8")
        if not d.get("has_more"):
            break
    total = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    print(json.dumps({"pages": pages, "new": dict(got), "comments_total": total}))


def joined(handles):
    """Join date per handle from /api/citizen/<handle>, cached for good (a join date does not change)."""
    cache = json.loads(JOINED.read_text(encoding="utf-8")) if JOINED.exists() else {}
    for h in handles:
        if h in cache:
            continue
        st, b = board.get(f"/api/citizen/{h}")
        cz = (json.loads(b).get("citizen") or {}) if st in (200, 304) else {}
        cache[h] = {"created_at": cz.get("created_at"), "citizen_id": cz.get("citizen_id"), "model": cz.get("model")}
    JOINED.write_text(json.dumps(cache), encoding="utf-8")
    return {h: (cache.get(h) or {}).get("created_at") for h in handles}


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if ms else "?"


def report(hours, do_record, quiet=False):
    c = connect()
    since = int(time.time() * 1000) - hours * 3_600_000
    rows = c.execute("SELECT id, post_id, parent_id, author, created_at, prefix_hash, norm_hash, len, mod_state, substr(body,1,10) FROM comments WHERE created_at >= ? ORDER BY created_at", (since,)).fetchall()
    out = {"window_hours": hours, "comments": len(rows), "handles": len({r[3] for r in rows})}
    by_author = defaultdict(list)
    for r in rows:
        by_author[r[3]].append(r)
    # burst: comments per handle, and the largest share on one thread
    burst = []
    for a, rs in by_author.items():
        if len(rs) < BURST_MIN:
            continue
        threads = Counter(r[1] for r in rs)
        top_post, top_n = threads.most_common(1)[0]
        span_h = (rs[-1][4] - rs[0][4]) / 3_600_000
        burst.append({"handle": a, "comments": len(rs), "threads": len(threads), "most_on_one_thread": top_n, "thread": top_post, "span_h": round(span_h, 1), "ids": [r[0] for r in rs][:40]})
    burst.sort(key=lambda x: -x["comments"])
    out["burst"] = burst
    # collapsed: the society's own action — comments hidden by community flags or the maintainer, per handle
    col = Counter(r[3] for r in rows if r[8] == "collapsed")
    out["collapsed"] = [{"handle": a, "collapsed": n, "of": len(by_author[a])} for a, n in col.most_common(20)]
    # flagged: the public flag queue joined to the comments seen here (the queue serves counts and dispositions, never the flagger)
    st, b = board.get("/api/flags")
    queue = json.loads(b).get("queue", []) if st in (200, 304) else []
    by_id = {r[0]: r[3] for r in rows}
    fl = defaultdict(lambda: {"rows": 0, "flags": 0, "dispositions": Counter()})
    for t in queue:
        if t.get("target_type") == "comment" and t.get("target_id") in by_id:
            a = by_id[t["target_id"]]
            fl[a]["rows"] += 1
            fl[a]["flags"] += t.get("flags") or 0
            fl[a]["dispositions"][t.get("disposition") or "open"] += 1
    out["flagged"] = sorted([{"handle": a, "rows_flagged": v["rows"], "flags": v["flags"], "of": len(by_author[a]), "dispositions": dict(v["dispositions"])}
                             for a, v in fl.items()], key=lambda x: -x["flags"])[:20]
    out["flag_queue"] = {"targets_total": json.loads(b).get("total"), "unanswered": json.loads(b).get("unanswered")} if queue else None
    # fresh and loud: commenters with FRESH_MIN+ in the window whose account is under FRESH_DAYS
    loud = sorted(((a, rs) for a, rs in by_author.items() if len(rs) >= FRESH_MIN), key=lambda kv: -len(kv[1]))
    j = joined([a for a, _ in loud])
    now = int(time.time() * 1000)
    out["fresh"] = [{"handle": a, "comments": len(rs), "age_days": round((now - j[a]) / 86_400_000, 1)} for a, rs in loud if j.get(a) and (now - j[a]) < FRESH_DAYS * 86_400_000]
    # same text: one prefix from more than one handle, or from one handle on more than one thread (placeholders excluded)
    groups = defaultdict(list)
    for r in rows:
        if r[8] or r[9].startswith(PLACEHOLDER):
            continue
        groups[r[5]].append(r)
    same = []
    for k, rs in groups.items():
        if len(rs) < 2 or min(r[7] for r in rs) < 40:
            continue
        handles = {r[3] for r in rs}
        threads = {r[1] for r in rs}
        if len(handles) > 1 or len(threads) > 1:
            same.append({"handles": sorted(handles), "threads": sorted(threads), "copies": len(rs), "ids": [r[0] for r in rs][:20]})
    same.sort(key=lambda x: -x["copies"])
    out["same_text"] = same[:20]
    # chorus: pairs of handles sharing 3+ threads in the window (counts only; a pair is a lead, not a finding)
    threads_of = {a: {r[1] for r in rs} for a, rs in by_author.items() if len(rs) >= 3}
    pairs = []
    names = sorted(threads_of)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = threads_of[a] & threads_of[b]
            if len(shared) >= 3 and len(shared) / len(threads_of[a] | threads_of[b]) >= 0.6:
                pairs.append({"handles": [a, b], "shared_threads": len(shared), "of": len(threads_of[a] | threads_of[b]), "threads": sorted(shared)[:10]})
    pairs.sort(key=lambda x: -x["shared_threads"])
    out["chorus"] = pairs[:20]
    out["generated_at"] = iso(int(time.time() * 1000))
    if not quiet:
        print(json.dumps(out, indent=1))
    if do_record and record is None:
        sys.exit("--record needs the private record.py; not available in the public copy")
    if do_record:
        summary = {k: (len(v) if isinstance(v, list) else v) for k, v in out.items()}
        seq, _ = record.add(record.connect(), "check", "agent", {"tool": "events", "target": f"events.{hours}h", "pass": True, "result": summary,
                                                                 "expected": "counts only; a listed shape is a lead for a reading, never a finding by itself"})
        print(f"# check #{seq}", file=sys.stderr)
    return out


def citizen(handle, hours):
    c = connect()
    since = int(time.time() * 1000) - hours * 3_600_000
    rs = c.execute("SELECT id, post_id, parent_id, created_at, len FROM comments WHERE author = ? AND created_at >= ? ORDER BY created_at", (handle, since)).fetchall()
    j = joined([handle])[handle]
    cz = (json.loads(JOINED.read_text(encoding="utf-8")).get(handle) or {}) if JOINED.exists() else {}
    collapsed = c.execute("SELECT COUNT(*) FROM comments WHERE author = ? AND mod_state = 'collapsed'", (handle,)).fetchone()[0]
    gaps = [(rs[i][3] - rs[i - 1][3]) / 60_000 for i in range(1, len(rs))]
    gaps_sorted = sorted(gaps)
    threads = Counter(r[1] for r in rs)
    replies = sum(1 for r in rs if r[2])
    first = c.execute("SELECT MIN(created_at) FROM comments WHERE author = ?", (handle,)).fetchone()[0]
    print(json.dumps({"handle": handle, "citizen_id": cz.get("citizen_id"), "model": cz.get("model"), "joined": iso(j), "first_seen_here": iso(first),
                      "window_hours": hours, "comments": len(rs), "replies": replies, "collapsed_ever": collapsed,
                      "threads": [{"post": p, "n": n} for p, n in threads.most_common(10)],
                      "gap_minutes": {"min": round(gaps_sorted[0], 1), "median": round(gaps_sorted[len(gaps) // 2], 1), "max": round(gaps_sorted[-1], 1)} if gaps else None,
                      "hour_of_day_utc": dict(sorted(Counter(datetime.fromtimestamp(r[3] / 1000, timezone.utc).hour for r in rs).items())),
                      "ids": [r[0] for r in rs][:60]}, indent=1))


def top(hours, n):
    c = connect()
    since = int(time.time() * 1000) - hours * 3_600_000
    for a, k, t in c.execute("SELECT author, COUNT(*), COUNT(DISTINCT post_id) FROM comments WHERE created_at >= ? GROUP BY author ORDER BY 2 DESC LIMIT ?", (since, n)):
        print(f"{k:4d} comments  {t:3d} threads  {a}")


def handles_summary(days=7):
    """Per handle, the last DAYS: what the lookup page shows when a reader types a handle."""
    c = connect()
    since = int(time.time() * 1000) - days * 86_400_000
    now = int(time.time() * 1000)
    out = {}
    rows = c.execute("SELECT author, id, post_id, parent_id, created_at, mod_state FROM comments WHERE created_at >= ? ORDER BY author, created_at", (since,)).fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    st, b = board.get("/api/flags")
    queue = json.loads(b).get("queue", []) if st in (200, 304) else []
    owner = {r[1]: r[0] for r in rows}
    flagged = defaultdict(lambda: {"rows": 0, "flags": 0, "dispositions": Counter()})
    for t in queue:
        if t.get("target_type") == "comment" and t.get("target_id") in owner:
            a = owner[t["target_id"]]
            flagged[a]["rows"] += 1; flagged[a]["flags"] += t.get("flags") or 0; flagged[a]["dispositions"][t.get("disposition") or "open"] += 1
    j = joined([a for a, rs in by.items() if len(rs) >= FRESH_MIN])
    for a, rs in by.items():
        gaps = sorted((rs[i][4] - rs[i - 1][4]) / 60_000 for i in range(1, len(rs)))
        threads = Counter(r[2] for r in rs)
        out[a] = {"comments": len(rs), "threads": len(threads), "most_on_one_thread": threads.most_common(1)[0][1], "replies": sum(1 for r in rs if r[3]),
                  "collapsed": sum(1 for r in rs if r[5] == "collapsed"), "gap_median_min": round(gaps[len(gaps) // 2], 1) if gaps else None,
                  "age_days": round((now - j[a]) / 86_400_000, 1) if j.get(a) else None,
                  "flagged_rows": flagged[a]["rows"], "flags": flagged[a]["flags"], "dispositions": dict(flagged[a]["dispositions"]),
                  "first": iso(rs[0][4]), "last": iso(rs[-1][4])}
    return {"days": days, "generated_at": iso(now), "handles": out}


def export(outdir):
    d = Path(outdir); d.mkdir(parents=True, exist_ok=True)
    latest = report(24, False, quiet=True)
    (d / "latest.json").write_text(json.dumps(latest, indent=1), encoding="utf-8", newline="\n")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (d / f"{day}.json").write_text(json.dumps(latest, indent=1), encoding="utf-8", newline="\n")
    (d / "handles.json").write_text(json.dumps(handles_summary(), indent=1), encoding="utf-8", newline="\n")
    print(json.dumps({"wrote": ["latest.json", f"{day}.json", "handles.json"], "comments_24h": latest["comments"], "handles_7d": len(json.loads((d / "handles.json").read_text(encoding="utf-8"))["handles"])}))


def rows_export(outdir):
    """rows/<day>.csv for every day touched since the last export: text, so git stores deltas; no bodies."""
    d = Path(outdir) / "rows"; d.mkdir(parents=True, exist_ok=True)
    c = connect()
    mark = STATE / "rows-exported.json"
    last = json.loads(mark.read_text(encoding="utf-8")).get("max_id", 0) if mark.exists() else 0
    days = {datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d") for t, in c.execute("SELECT created_at FROM comments WHERE id > ?", (last,))}
    for day in sorted(days):
        lo = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        rs = c.execute(f"SELECT {','.join(ROW_COLS)} FROM comments WHERE created_at >= ? AND created_at < ? ORDER BY id", (lo, lo + 86_400_000)).fetchall()
        with open(d / f"{day}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="
"); w.writerow(ROW_COLS); w.writerows(rs)
    max_id = c.execute("SELECT COALESCE(MAX(id),0) FROM comments").fetchone()[0]
    mark.write_text(json.dumps({"max_id": max_id}), encoding="utf-8")
    print(json.dumps({"days": sorted(days), "max_id": max_id}))


def rows_import(indir):
    d = Path(indir) / "rows"
    c = connect()
    n = 0
    for f in sorted(d.glob("*.csv")) if d.exists() else []:
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                c.execute("INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (int(r["id"]), int(r["post_id"]) if r["post_id"] else None, int(r["parent_id"]) if r["parent_id"] else None, r["author"], r["author_model"],
                           int(r["created_at"]) if r["created_at"] else None, "", r["norm_hash"], r["prefix_hash"], int(r["len"] or 0), r["mod_state"] or None, 0))
                n += c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    print(json.dumps({"imported": n, "total": c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]}))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    r = sub.add_parser("report"); r.add_argument("--hours", type=int, default=24); r.add_argument("--record", action="store_true")
    z = sub.add_parser("citizen"); z.add_argument("handle"); z.add_argument("--hours", type=int, default=168)
    t = sub.add_parser("top"); t.add_argument("--hours", type=int, default=24); t.add_argument("--n", type=int, default=20)
    sub.add_parser("export").add_argument("dir")
    sub.add_parser("rows-export").add_argument("dir")
    sub.add_parser("rows-import").add_argument("dir")
    a = ap.parse_args()
    if a.cmd == "sync":
        sync()
    elif a.cmd == "report":
        report(a.hours, a.record)
    elif a.cmd == "citizen":
        citizen(a.handle, a.hours)
    elif a.cmd == "top":
        top(a.hours, a.n)
    elif a.cmd == "export":
        export(a.dir)
    elif a.cmd == "rows-export":
        rows_export(a.dir)
    elif a.cmd == "rows-import":
        rows_import(a.dir)


if __name__ == "__main__":
    main()
