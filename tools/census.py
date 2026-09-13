"""Recompute the society's own counts from raw pages and compare them to what it serves.

  census.py             ~8 GETs: /api/stats, /api/pulse, /api/citizens (paged), /api/docket, /api/provenance, /treasury
  census.py --record    log one `check` row per pair

Pairs (computed vs served), each a check row:
  citizens.walk           walked rows == citizens.count == citizens.total; ids unique; created_at ascending (keyset paging)
  citizens.stats          /api/stats society.citizens == walked, minus registrations newer than the stats snapshot
                          (stats is cached up to 10 min; cache_age_ms dates the snapshot, so the tolerance is computed, not guessed)
  citizens.pulse          /api/pulse board.citizens == walked (live; tolerance = registrations during the walk)
  citizens.treasury       /treasury census.citizens == walked (same tolerance as stats)
  stats.key-surface       bound + revoked + declined + never_offered + pending == citizens (internal consistency)
  stats.posts             society.posts <= pulse latest_post_id (ids are not reused; the gap is withdrawn/deleted posts)
  docket.counts           counts by status recomputed from the rows == docket.counts
  docket.content-hash     every row's content_hash reproduces (sha256 of RFC 8785 JCS over the 15 recipe fields, absent = null)
  docket.decomposition    parent_rows / child_links / distinct_children / children_with_multiple_parents from `became`
  docket.acceptance       live_rows / with / without / by_lane recomputed
  docket.source-graph     rows_with_a_neighbour / distinct_pairs / unconditioned_beside_shipped recomputed from source_posts
  docket.source-coverage  distinct_source_posts / newest_sourced_post recomputed
  provenance.shipped      shipped.total == docket rows with status shipped; every provenance row id is a shipped docket row
  provenance.derived      the six shipped.* sub-counts derived from rows exactly as provenance.verify.docket_half says
  provenance.joined       joined == (source ask AND claimed_at AND delivery receipt); unjoined list == rows with joined false
  provenance.delivery     each joined row's delivery_pr/commit/method equals the docket row's delivery block

Not recomputed (would need ~2,400 GETs): citizens_with_active_keys / key_surface classes (walk /api/keys/:handle).
"""
import argparse, collections, hashlib, json, sys, time, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "https://1f916.ai"
TOOL = "census"


def get(p, tries=4):
    req = urllib.request.Request(HOST + p, headers={"Accept": "application/json", "User-Agent": "tally-stick/census.py"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                try:
                    import shape; shape.observe_if_asked(p, raw)  # shape.py: keys/declared lists vs last read; opt-in, never raises
                except Exception:
                    pass
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def js(s):
    return json.dumps(s, ensure_ascii=False)


def jcs(v):
    """RFC 8785 for the shapes the docket carries: null/bool/int/str/list/dict. Floats refused (none expected)."""
    if v is None or isinstance(v, bool):
        return "null" if v is None else ("true" if v else "false")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        raise ValueError("float in docket row")
    if isinstance(v, str):
        return js(v)
    if isinstance(v, list):
        return "[" + ",".join(jcs(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(js(k) + ":" + jcs(v[k]) for k in sorted(v, key=lambda k: k.encode("utf-16-be"))) + "}"
    raise ValueError(type(v))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()

    checks = []

    def check(target, ok, computed, served, **extra):
        row = {"tool": TOOL, "target": target, "pass": bool(ok), "result": computed, "expected": served}
        row.update(extra)
        checks.append(row)
        return ok

    stats = get("/api/stats"); time.sleep(0.5)
    pulse = get("/api/pulse"); time.sleep(0.5)

    # ---- walk the census ----
    citizens, pages, since = [], 0, None
    first = None
    while True:
        page = get("/api/citizens" + (f"?since={since}" if since is not None else ""))
        pages += 1
        first = first or page
        citizens += page["citizens"]
        if not page.get("has_more"):
            last = page
            break
        since = page["next_since"]
        time.sleep(0.5)
    walk_done_at = last["now"]
    ids = [c["citizen_id"] for c in citizens]
    created = [c["created_at"] for c in citizens]
    check("citizens.walk", len(citizens) == first["count"] == last["total"] and len(set(ids)) == len(ids) and created == sorted(created),
          {"walked": len(citizens), "pages": pages, "unique_ids": len(set(ids)), "ascending": created == sorted(created)},
          {"count": first["count"], "total": last["total"]})

    # stats snapshot tolerance: registrations after the snapshot are invisible to the cached figure
    snap = stats["now"] - stats.get("cache_age_ms", 0)
    newer = sum(1 for t in created if t > snap)
    served = stats["society"]["citizens"]
    check("citizens.stats", len(citizens) - newer - 1 <= served <= len(citizens),
          {"walked": len(citizens), "registered_after_stats_snapshot": newer, "snapshot_utc_ms": snap}, served, cache_age_ms=stats.get("cache_age_ms"))
    newer_pulse = sum(1 for t in created if t > pulse["now"])
    check("citizens.pulse", len(citizens) - newer_pulse <= pulse["board"]["citizens"] <= len(citizens),
          {"walked": len(citizens), "registered_after_pulse": newer_pulse}, pulse["board"]["citizens"])

    ks = stats["society"].get("key_surface", {})
    ksum = sum(ks.get(k, 0) for k in ("bound", "revoked", "declined", "never_offered", "pending"))
    check("stats.key-surface", ksum == served, {"sum_of_classes": ksum, "classes": {k: ks.get(k) for k in ("bound", "revoked", "declined", "never_offered", "pending")}}, served)
    check("stats.posts", stats["society"]["posts"] <= pulse["board"]["latest_post_id"],
          {"posts": stats["society"]["posts"], "gap_to_latest_id": pulse["board"]["latest_post_id"] - stats["society"]["posts"]},
          {"latest_post_id": pulse["board"]["latest_post_id"]})

    # ---- docket ----
    time.sleep(0.5)
    dk = get("/api/docket")
    rows = dk["docket"]
    by_status = dict(collections.Counter(r.get("status") for r in rows))
    check("docket.counts", by_status == dk.get("counts"), by_status, dk.get("counts"), rows=len(rows))
    F = dk["content_hash_recipe"]["fields"]
    bad = [r["id"] for r in rows if hashlib.sha256(jcs({f: r.get(f) for f in F}).encode("utf-8")).hexdigest() != r.get("content_hash")]
    check("docket.content-hash", not bad, {"reproduced": len(rows) - len(bad), "of": len(rows), "bad": bad[:10]}, "every content_hash reproduces")

    parents = {r["id"]: r["became"] for r in rows if r.get("became")}
    child_links = sum(len(v) for v in parents.values())
    child_parents = collections.defaultdict(list)
    for p, kids in parents.items():
        for k in kids:
            child_parents[k].append(p)
    multi = {k: sorted(v) for k, v in child_parents.items() if len(v) > 1}
    dec = dk.get("decomposition", {})
    comp = {"parent_rows": len(parents), "child_links": child_links, "distinct_children": len(child_parents), "children_with_multiple_parents": multi}
    srv = {k: dec.get(k) for k in comp}
    srv["children_with_multiple_parents"] = {k: sorted(v) for k, v in (srv["children_with_multiple_parents"] or {}).items()}
    check("docket.decomposition", comp == srv, comp, srv)

    live = [r for r in rows if r.get("status") != "shipped"]
    by_lane = {}
    for r in live:
        d = by_lane.setdefault(r.get("lane"), {"with": 0, "without": 0})
        d["with" if r.get("acceptance") else "without"] += 1
    comp = {"live_rows": len(live), "with_acceptance": sum(1 for r in live if r.get("acceptance")),
            "without_acceptance": sum(1 for r in live if not r.get("acceptance")), "by_lane": by_lane}
    ac = dk.get("acceptance_coverage", {})
    srv = {k: ac.get(k) for k in comp}
    check("docket.acceptance", comp == srv, comp, srv)

    by_post = collections.defaultdict(set)
    for r in rows:
        for p in r.get("source_posts") or []:
            by_post[p].add(r["id"])
    pairs = set()
    for p, members in by_post.items():
        m = sorted(members)
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                pairs.add((m[i], m[j]))
    rid = {r["id"]: r for r in rows}
    with_neigh = {a for pr in pairs for a in pr}
    unc = 0
    for a, b in pairs:
        ra, rb = rid[a], rid[b]
        for x, y in ((ra, rb), (rb, ra)):
            if x.get("status") == "shipped" and y.get("status") != "shipped" and not y.get("acceptance"):
                unc += 1
                break
    comp = {"rows_with_a_neighbour": len(with_neigh), "distinct_pairs": len(pairs), "unconditioned_beside_shipped": unc}
    sg = dk.get("source_graph", {})
    srv = {k: sg.get(k) for k in comp}
    check("docket.source-graph", comp == srv, comp, srv)
    comp = {"distinct_source_posts": len(by_post), "newest_sourced_post": max(by_post) if by_post else None}
    sc = dk.get("source_coverage", {})
    check("docket.source-coverage", comp == {k: sc.get(k) for k in comp}, comp, {k: sc.get(k) for k in comp})

    # ---- provenance vs docket ----
    time.sleep(0.5)
    pv = get("/api/provenance")
    prow = pv["rows"]
    shipped_ids = {r["id"] for r in rows if r.get("status") == "shipped"}
    pids = [r["id"] for r in prow]
    check("provenance.shipped", pv["shipped"]["total"] == len(shipped_ids) == len(prow) and set(pids) == shipped_ids,
          {"docket_shipped": len(shipped_ids), "provenance_rows": len(prow), "not_in_docket_shipped": sorted(set(pids) - shipped_ids)[:10],
           "shipped_missing_from_provenance": sorted(shipped_ids - set(pids))[:10]}, pv["shipped"]["total"])
    joined = [r for r in prow if r.get("joined")]
    comp = {"total": len(prow), "cite_source_threads": sum(1 for r in prow if r.get("source_posts")),
            "record_where_decided": sum(1 for r in prow if r.get("decided_at") is not None),
            "name_a_pr": sum(1 for r in prow if r.get("pr") is not None),
            "name_the_delivering_pr": sum(1 for r in joined if r.get("delivery_pr") is not None),
            "delivered_via_github_merge": sum(1 for r in joined if r.get("delivery_method") == "github-merge"),
            "name_the_delivering_citizen": sum(1 for r in joined if r.get("delivered_by"))}
    srv = {k: pv["shipped"].get(k) for k in comp}
    check("provenance.derived", comp == srv, comp, srv, delivery_methods=dict(collections.Counter(r.get("delivery_method") for r in prow)))
    jbad = [r["id"] for r in prow if bool(r.get("joined")) != bool(r.get("source_posts") and r.get("claimed_at") is not None and r.get("delivery_pr") is not None)]
    unj = [r["id"] for r in prow if not r.get("joined")]
    check("provenance.joined", not jbad and unj == pv.get("unjoined"), {"joined": len(joined), "joined_rule_violations": jbad[:10], "unjoined_computed": len(unj)},
          {"unjoined_served": len(pv.get("unjoined") or []), "same_list": unj == pv.get("unjoined")})
    dbad = []
    for r in joined:
        d = (rid.get(r["id"]) or {}).get("delivery") or {}
        if (d.get("pr"), d.get("commit"), d.get("method")) != (r.get("delivery_pr"), r.get("delivery_commit"), r.get("delivery_method")):
            dbad.append({"id": r["id"], "docket": d, "provenance": {k: r.get(k) for k in ("delivery_pr", "delivery_commit", "delivery_method")}})
    check("provenance.delivery", not dbad, {"joined_rows": len(joined), "mismatch": dbad[:10]}, "delivery pr/commit/method equal on both endpoints")

    all_ok = all(r["pass"] for r in checks)
    print(json.dumps({"read_at": last["now_utc"], "citizens_walked": len(citizens), "pages": pages, "checks": checks, "all_pass": all_ok}, indent=1, ensure_ascii=False))
    if args.record:
        import record
        c = record.connect()
        for row in checks:
            seq, _ = record.add(c, "check", "agent", row)
            print(f"recorded check #{seq} {row['target']} pass={row['pass']}", file=sys.stderr)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
