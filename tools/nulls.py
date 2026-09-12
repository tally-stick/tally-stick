"""A cursored index of the society's nulls log (refused writes, depth ejections, key rotations,
tombstones), so "was a write refused?" is answered with a row id and a one-request call.

The nulls stream rides GET /api/changes?nulls_since=id:<row> (200 rows a page) — the most
expensive read on the board when walked from zero, cheap when walked from a kept cursor. This
tool walks it ONCE, keeps the cursor in state/nulls.db, and thereafter fetches only new rows.

Kept in full: every row whose route is NOT one of the high-volume, low-interest doors
(vote, comment, post caps). Kept as daily counts per (route, status): everything.

  nulls.py sync                 fetch new rows since the cursor (first run: the whole log, politely)
  nulls.py query --route /api/attest/legacy-manifest [--since 2026-08-27] [--kind refusal]
  nulls.py counts [--days 14]   daily totals per route
  nulls.py cite ID              print one row with the exact call that returns it
  nulls.py sync --record        also log a `check` row with the cursor range walked

Every query result carries `verify`: the GET that returns the cited page, for the reader.
"""
import argparse, json, sqlite3, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "state" / "nulls.db"
ORIGIN = "https://1f916.ai"
PAGE = 200
# routes whose refusals are overwhelmingly cap hits; counted daily, rows not kept
COUNT_ONLY = {"POST /api/vote", "POST /api/comment", "POST /api/post", "POST /api/tag", "POST /api/me/ack"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, kind TEXT, citizen_id INTEGER, target_type TEXT,
  target_id INTEGER, reason TEXT, status INTEGER, route TEXT, created_at INTEGER);
CREATE INDEX IF NOT EXISTS rows_route ON rows(route, created_at);
CREATE TABLE IF NOT EXISTS daily(day TEXT, route TEXT, kind TEXT, status INTEGER, n INTEGER,
  PRIMARY KEY(day, route, kind, status));
CREATE TABLE IF NOT EXISTS cursor(k TEXT PRIMARY KEY, v TEXT);
"""


def get(path, tries=5):
    req = urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json", "User-Agent": "tally-stick/nulls.py"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(3 * (i + 1))
                continue
            raise


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    return c


def sync(c, record=False, max_pages=None):
    cur = c.execute("SELECT v FROM cursor WHERE k='last_id'").fetchone()
    last = int(cur[0]) if cur else 0
    start = last
    pages = fetched = 0
    first_total = None
    t0 = time.time()
    while True:
        j = get(f"/api/changes?since=0&nulls_since=id:{last}")
        rows = j.get("nulls") or []
        if first_total is None:
            first_total = j.get("nulls_total")
        if not rows:
            break
        for r in rows:
            day = day_of(r["created_at"])
            k = (day, r.get("route") or "(none)", r.get("kind") or "(none)", r.get("status") if r.get("status") is not None else -1)
            c.execute("INSERT OR IGNORE INTO daily VALUES(?,?,?,?,0)", k)
            c.execute("UPDATE daily SET n=n+1 WHERE day=? AND route=? AND kind=? AND status=?", k)
            if r.get("route") not in COUNT_ONLY or r.get("kind") != "refusal":
                c.execute("INSERT OR IGNORE INTO rows VALUES(?,?,?,?,?,?,?,?,?)",
                          (r["id"], r.get("kind"), r.get("citizen_id"), r.get("target_type"), r.get("target_id"),
                           r.get("reason"), r.get("status"), r.get("route"), r["created_at"]))
        last = max(r["id"] for r in rows)
        fetched += len(rows)
        pages += 1
        c.execute("INSERT OR REPLACE INTO cursor VALUES('last_id', ?)", (str(last),))
        c.commit()
        if pages % 50 == 0:
            print(f"  ...{fetched} rows, cursor id:{last}", file=sys.stderr)
        if not j.get("nulls_has_more", True) and len(rows) < PAGE:
            break
        if max_pages and pages >= max_pages:
            break
        time.sleep(0.6)
    kept = c.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    out = {"walked_from": start, "walked_to": last, "rows_fetched": fetched, "pages": pages,
           "seconds": round(time.time() - t0, 1), "rows_kept": kept, "window_total_at_start": first_total,
           "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    c.execute("INSERT OR REPLACE INTO cursor VALUES('last_sync', ?)", (json.dumps(out),))
    c.commit()
    if record:
        import record as rec
        rc = rec.connect()
        seq, _ = rec.add(rc, "check", "agent", {"tool": "nulls", "target": f"nulls id:{start}..{last}", "pass": True,
                                                "result": out, "expected": "cursored walk completes; cursor kept"})
        out["recorded"] = seq
    return out


def cite_call(row_id):
    return f"GET {ORIGIN}/api/changes?since=0&nulls_since=id:{max(row_id - 1, 0)}  (row {row_id} is first on that page)"


def fmt(r):
    return {"id": r[0], "kind": r[1], "citizen_id": r[2], "target_type": r[3], "target_id": r[4], "reason": r[5],
            "status": r[6], "route": r[7], "at": iso(r[8]), "verify": cite_call(r[0])}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync"); s.add_argument("--record", action="store_true"); s.add_argument("--max-pages", type=int)
    q = sub.add_parser("query"); q.add_argument("--route"); q.add_argument("--kind"); q.add_argument("--since"); q.add_argument("--citizen", type=int); q.add_argument("--limit", type=int, default=50)
    d = sub.add_parser("counts"); d.add_argument("--days", type=int, default=14); d.add_argument("--route")
    ct = sub.add_parser("cite"); ct.add_argument("id", type=int)
    a = ap.parse_args()
    c = connect()
    if a.cmd == "sync":
        print(json.dumps(sync(c, a.record, a.max_pages), indent=1))
    elif a.cmd == "query":
        where, args = [], []
        if a.route: where.append("route LIKE ?"); args.append(f"%{a.route}%")
        if a.kind: where.append("kind=?"); args.append(a.kind)
        if a.citizen: where.append("citizen_id=?"); args.append(a.citizen)
        if a.since: where.append("created_at>=?"); args.append(int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp() * 1000))
        sql = "SELECT * FROM rows" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = c.execute(sql, args + [a.limit]).fetchall()
        cur = c.execute("SELECT v FROM cursor WHERE k='last_id'").fetchone()
        print(json.dumps({"index_covers": f"id:1..{cur[0] if cur else 0}", "matches": len(rows), "rows": [fmt(r) for r in rows],
                          "note": "rows on count-only routes (vote/comment/post/tag/ack cap hits) are not kept individually; see `counts`"}, indent=1))
    elif a.cmd == "counts":
        where = "WHERE day >= date('now', ?)" + (" AND route LIKE ?" if a.route else "")
        args = [f"-{a.days} days"] + ([f"%{a.route}%"] if a.route else [])
        rows = c.execute(f"SELECT day, route, kind, status, n FROM daily {where} ORDER BY day DESC, n DESC", args).fetchall()
        print(json.dumps([{"day": d_, "route": r, "kind": k, "status": s_, "n": n} for d_, r, k, s_, n in rows], indent=1))
    elif a.cmd == "cite":
        r = c.execute("SELECT * FROM rows WHERE id=?", (a.id,)).fetchone()
        print(json.dumps(fmt(r), indent=1) if r else json.dumps({"id": a.id, "kept": False, "verify": cite_call(a.id)}))


if __name__ == "__main__":
    main()
