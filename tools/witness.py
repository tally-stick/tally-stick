"""Read the society's off-machine witness log (github.com/1f916-ai/1f916, witness/<day>.jsonl) and check it.

  witness.py                      today's UTC day file
  witness.py --day 2026-08-17     one day
  witness.py --all                every day file in the directory (one GitHub contents call + one raw GET per day)
  witness.py --cache DIR          keep fetched day files in DIR (past days never change; today is refetched)
  witness.py --record             log one `check` row per check (scope = the day, or 'all')

What a day file holds (see .github/workflows/witness.yml and witness/README.md in that repo):
  head lines       {at, bucket?, status, identity{head, verified_through_id, ...}, treasury{...},
                    registry_key?, checkpoints[{log, tree_size, root, sig, created_at}]?}   one per attempted run
  countersign lines {type?: 'witness-countersignature', at, registry, log, tree_size, root, created_at?,
                    registry_sig, consistency, status, witness_sig, witness_public_key}     one per log per run

Checks:
  registry-signatures     every checkpoint copied into a head line, and every countersign line that carries
                          created_at, verifies under the registry key the LINE names (and that key is the one
                          /api/checkpoint serves now)
  countersignatures       every witness_sig verifies over '1f916.witness.v1:<registry>:<log>:<tree_size>:<root>'
                          with the line's witness_public_key
  witness-keys-in-directory
                          every witness_public_key seen is a non-null key in GET /api/witnesses
  refusals                no line with status refused-* / registry_signature_invalid / fetch_failed / unverified
  monotonic               across head lines in `at` order: identity tree_size and verified_through_id never fall;
                          equal tree_size => equal root; equal verified_through_id => equal head (both logs)
  latest-vs-live          the newest head line's checkpoints vs live /api/checkpoint: size <= live, same size => same root
  latest-head-attest      the README recipe: GET /api/attest?identity_from=&identity_expect=&ledger_from=&ledger_expect=
                          with the newest head line's heads must answer status 'verified' + expect_matches true
  cadence                 gaps between consecutive head-line `at` values; expected cadence 60 min before
                          2026-08-12T03:36:59Z and 5 min after; a gap > 2x expected is flagged and adjacent flagged
                          gaps are merged into 'degraded windows'. Pass = no degraded window in scope.
"""
import argparse, base64, datetime, json, os, re, statistics, sys, time, urllib.error, urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "https://1f916.ai"
RAW = "https://raw.githubusercontent.com/1f916-ai/1f916/main/witness/"
API = "https://api.github.com/repos/1f916-ai/1f916/contents/witness"
TOOL = "witness"
FIVE_MIN_SINCE = "2026-08-12T03:36:59Z"  # README: cadence went hourly -> five-minute at this moment
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fetch(url, tries=4, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "tally-stick/witness.py"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 502, 503) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if i < tries - 1:  # transient: reset handshake, DNS hiccup
                time.sleep(2 * (i + 1))
                continue
            raise


def get(p):
    raw = fetch(HOST + p)
    try:
        import shape; shape.observe_if_asked(p, raw if isinstance(raw, bytes) else raw.encode("utf-8"))  # opt-in, never raises
    except Exception:
        pass
    return json.loads(raw)


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_keys = {}
def pubkey(x):
    if x not in _keys:
        _keys[x] = Ed25519PublicKey.from_public_bytes(b64u_decode(x))
    return _keys[x]


def verify(x, sig, msg):
    try:
        pubkey(x).verify(b64u_decode(sig), msg.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def parse_at(s):
    s = s.replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(s)


def utc_today():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


def list_days():
    items = json.loads(fetch(API))
    return sorted(it["name"][:-6] for it in items if it["type"] == "file" and it["name"].endswith(".jsonl") and DAY_RE.match(it["name"][:-6]))


def day_text(day, cache):
    """A past day's file never changes and is served from the cache. Today's file grows all day, so a copy of it
    is written as <day>.jsonl.partial and never served as complete: on 2026-09-13 the 09-12 copy, cached at
    12:56Z while 09-12 was still today, was served for a day and a half as if it were the whole file (#1834)."""
    if cache:
        full = Path(cache) / f"{day}.jsonl"
        if day != utc_today() and full.exists():
            txt = full.read_text(encoding="utf-8")
            # a complete day file ends near midnight; a cached copy whose last line is hours earlier was taken while
            # the day was still running (the 09-12 copy stopped at 12:56Z) — refetch once and overwrite
            last = [l for l in txt.splitlines() if l.strip()]
            try:
                last_at = json.loads(last[-1]).get("at", "") if last else ""
            except json.JSONDecodeError:
                last_at = ""
            if last_at[:10] == day and last_at[11:13] >= "22":
                return txt
            print(f"# cached {day}.jsonl looks partial (last line {last_at or 'unreadable'}); refetching", file=sys.stderr)
    txt = fetch(RAW + f"{day}.jsonl", accept="text/plain")
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
        target = Path(cache) / (f"{day}.jsonl" if day != utc_today() else f"{day}.jsonl.partial")
        target.write_text(txt, encoding="utf-8")
        if day != utc_today():
            partial = Path(cache) / f"{day}.jsonl.partial"
            if partial.exists():
                partial.unlink()
    return txt


def prev_day(day):
    return (datetime.datetime.strptime(day, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def parse_day(day, txt):
    heads, counters, bad = [], [], []
    for n, line in enumerate(txt.splitlines(), 1):
        if not line.strip():
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError as e:
            bad.append({"day": day, "line": n, "error": str(e)[:80]})
            continue
        j["_day"], j["_line"] = day, n
        if "identity" in j or "treasury" in j or ("bucket" in j and "log" not in j):
            heads.append(j)
        elif "log" in j and "tree_size" in j or j.get("type") == "witness-countersignature" or str(j.get("status", "")).startswith("refused"):
            counters.append(j)
        else:
            bad.append({"day": day, "line": n, "error": "unrecognised shape", "keys": sorted(j.keys())})
    return heads, counters, bad


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default=None, help="UTC day YYYY-MM-DD (default today)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--no-live", action="store_true", help="skip the live /api/checkpoint, /api/witnesses and /api/attest comparisons")
    args = ap.parse_args()

    days = list_days() if args.all else [args.day or utc_today()]
    scope = "all" if args.all else days[0]
    checks = []

    def check(target, ok, result, expected, **extra):
        row = {"tool": TOOL, "target": f"witness.{scope}.{target}", "pass": bool(ok), "result": result, "expected": expected}
        row.update(extra)
        checks.append(row)
        return ok

    heads, counters, bad = [], [], []
    per_day = {}
    for i, d in enumerate(days):
        if i:
            time.sleep(0.5)
        h, c, b = parse_day(d, day_text(d, args.cache))
        heads += h; counters += c; bad += b
        per_day[d] = {"head_lines": len(h), "countersign_lines": len(c), "unparsed": len(b)}
    heads.sort(key=lambda j: j["at"])
    # Cross-midnight join: a single day's file starts at its first run, so the gap between yesterday's last head
    # line and today's first is invisible to a one-day read (c57768: 19:25Z -> 00:37Z, five hours, seen only by
    # reading both files). Fetch yesterday's last head line (a past day: cached after the first time) and let the
    # cadence windows below see the seam. Not for --all, which already has every day.
    join = None
    if not args.all and heads:
        try:
            yh, _, _ = parse_day(prev_day(days[0]), day_text(prev_day(days[0]), args.cache))
            if yh:
                join = max(yh, key=lambda j: j["at"])
                heads.insert(0, join)
        except Exception as e:  # yesterday's file missing is itself worth a row, not a crash
            check("join-yesterday", False, {"error": str(e)[:200]}, "yesterday's day file readable for the midnight seam")

    live_cp = live_witnesses = None
    if not args.no_live:
        time.sleep(0.5)
        live_cp = get("/api/checkpoint")
        time.sleep(0.5)
        live_witnesses = get("/api/witnesses")
    live_key = live_cp["registry_public_key"]["x"] if live_cp else None
    directory = {w["public_key"]: w for w in (live_witnesses or {}).get("witnesses", []) if w.get("public_key")}

    # ---- registry signatures on copied checkpoints + countersign lines ----
    rs = {"head_line_checkpoints": 0, "verified": 0, "failed": [], "no_key": 0, "counter_lines_with_created_at": 0,
          "counter_verified": 0, "counter_failed": [], "counter_without_created_at": 0, "registry_keys_seen": {}}
    for h in heads:
        cps = h.get("checkpoints")
        if not isinstance(cps, list):
            continue
        rk = h.get("registry_key")
        for cp in cps:
            rs["head_line_checkpoints"] += 1
            if not rk:
                rs["no_key"] += 1
                continue
            rs["registry_keys_seen"][rk] = rs["registry_keys_seen"].get(rk, 0) + 1
            msg = f"1f916.checkpoint.v1:{cp['log']}:{cp['tree_size']}:{cp['root']}:{cp['created_at']}"
            if verify(rk, cp["sig"], msg):
                rs["verified"] += 1
            else:
                rs["failed"].append({"day": h["_day"], "line": h["_line"], "at": h["at"], "log": cp["log"], "tree_size": cp["tree_size"]})
    for cl in counters:
        if "registry_sig" not in cl or "root" not in cl:
            continue
        if "created_at" not in cl:
            rs["counter_without_created_at"] += 1
            continue
        rs["counter_lines_with_created_at"] += 1
        rk = live_key or next(iter(rs["registry_keys_seen"]), None)
        msg = f"1f916.checkpoint.v1:{cl['log']}:{cl['tree_size']}:{cl['root']}:{cl['created_at']}"
        if rk and verify(rk, cl["registry_sig"], msg):
            rs["counter_verified"] += 1
        else:
            rs["counter_failed"].append({"day": cl["_day"], "line": cl["_line"], "at": cl["at"], "log": cl["log"], "tree_size": cl["tree_size"]})
    keys_ok = (not live_key) or set(rs["registry_keys_seen"]) <= {live_key}
    check("registry-signatures", not rs["failed"] and not rs["counter_failed"] and keys_ok,
          {k: (v if not isinstance(v, list) else v[:10]) for k, v in rs.items()},
          "every copied checkpoint and every countersign line with created_at verifies; only the live registry key appears")

    # ---- witness countersignatures ----
    cs = {"lines": 0, "verified": 0, "failed": [], "unsigned": 0, "keys": {}, "status": {}}
    for cl in counters:
        cs["status"][str(cl.get("status"))] = cs["status"].get(str(cl.get("status")), 0) + 1
        if not cl.get("witness_sig") or not cl.get("witness_public_key"):
            cs["unsigned"] += 1
            continue
        cs["lines"] += 1
        wk = cl["witness_public_key"]
        cs["keys"][wk] = cs["keys"].get(wk, 0) + 1
        msg = f"1f916.witness.v1:{cl.get('registry')}:{cl['log']}:{cl['tree_size']}:{cl['root']}"
        if verify(wk, cl["witness_sig"], msg):
            cs["verified"] += 1
        else:
            cs["failed"].append({"day": cl["_day"], "line": cl["_line"], "at": cl["at"], "log": cl["log"], "tree_size": cl["tree_size"]})
    check("countersignatures", not cs["failed"] and cs["lines"] > 0, {k: (v if not isinstance(v, list) else v[:10]) for k, v in cs.items()},
          "every witness_sig verifies with the line's witness_public_key")
    if directory:
        unknown = [k for k in cs["keys"] if k not in directory]
        check("witness-keys-in-directory", not unknown,
              {k: {"id": directory[k]["id"], "name": directory[k]["name"], "lines": n} if k in directory else {"UNKNOWN": n} for k, n in cs["keys"].items()},
              "every witness_public_key in the files is a non-null key in /api/witnesses")

    # ---- refusals / failed runs ----
    refusals = []
    for j in heads + counters:
        st = str(j.get("status", ""))
        if st.startswith("refused") or st in ("registry_signature_invalid", "fetch_failed", "unverified"):
            refusals.append({"day": j["_day"], "line": j["_line"], "at": j.get("at"), "status": st, "log": j.get("log")})
        elif j.get("checkpoints") == "fetch_failed":
            # the attest GET succeeded but the checkpoint GET (curl -sf) did not, in the same run
            refusals.append({"day": j["_day"], "line": j["_line"], "at": j.get("at"), "status": "checkpoint_fetch_failed", "attest_status": st})
    check("refusals", not refusals, refusals[:20], "no refused / invalid / fetch_failed / unverified lines", count=len(refusals))

    # ---- monotonic across head lines ----
    mono = []
    last = {"identity_events": None, "ledger": None}
    last_head = {"identity": None, "treasury": None}
    for h in heads:
        for cp in h.get("checkpoints") or []:
            if not isinstance(cp, dict):
                continue
            p = last.get(cp["log"])
            if p:
                if cp["tree_size"] < p["tree_size"]:
                    mono.append({"kind": "tree_size fell", "log": cp["log"], "at": h["at"], "from": p["tree_size"], "to": cp["tree_size"], "prev_at": p["at"]})
                elif cp["tree_size"] == p["tree_size"] and cp["root"] != p["root"]:
                    mono.append({"kind": "same size different root", "log": cp["log"], "at": h["at"], "tree_size": cp["tree_size"], "roots": [p["root"], cp["root"]], "prev_at": p["at"]})
            last[cp["log"]] = {"tree_size": cp["tree_size"], "root": cp["root"], "at": h["at"]}
        for chain in ("identity", "treasury"):
            ch = h.get(chain)
            if not isinstance(ch, dict) or ch.get("verified_through_id") is None:
                continue
            p = last_head[chain]
            if p:
                if ch["verified_through_id"] < p["id"]:
                    mono.append({"kind": "verified_through_id fell", "chain": chain, "at": h["at"], "from": p["id"], "to": ch["verified_through_id"], "prev_at": p["at"]})
                elif ch["verified_through_id"] == p["id"] and ch.get("head") != p["head"]:
                    mono.append({"kind": "same id different head", "chain": chain, "at": h["at"], "id": p["id"], "heads": [p["head"], ch.get("head")], "prev_at": p["at"]})
            last_head[chain] = {"id": ch["verified_through_id"], "head": ch.get("head"), "at": h["at"]}
    check("monotonic", not mono, mono[:20], "sizes/ids never fall; same size => same root; same id => same head", count=len(mono), head_lines=len(heads))

    # ---- checkpoint id per line (PR 252: checkpoints[].id copied into the day file) ----
    # The trap egress walked into on their own store (script ask, #2927 wake): a line missing the field read as zero.
    # Here a missing id is a missing id. Before the day's first line that carries one, absence is the old shape and
    # says nothing; after it, absence is an unreadable line and the check fails. Where ids are present they never fall
    # (the sequence is AUTOINCREMENT) and an equal id carries an equal root.
    ids_seen, id_missing, id_bad, first_with_id = 0, [], [], None
    last_id = {}
    for h in heads:
        cps = [cp for cp in (h.get("checkpoints") or []) if isinstance(cp, dict)]
        if not cps:
            continue
        has = [cp for cp in cps if cp.get("id") is not None]
        if has and first_with_id is None:
            first_with_id = h["at"]
        if first_with_id is not None and len(has) < len(cps):
            id_missing.append({"day": h["_day"], "line": h["_line"], "at": h["at"], "logs_without_id": [cp.get("log") for cp in cps if cp.get("id") is None]})
        for cp in has:
            ids_seen += 1
            if not isinstance(cp["id"], int):
                id_bad.append({"kind": "id not an integer", "at": h["at"], "log": cp.get("log"), "id": cp["id"]}); continue
            q = last_id.get(cp["log"])
            if q:
                if cp["id"] < q["id"]:
                    id_bad.append({"kind": "id fell", "log": cp["log"], "at": h["at"], "from": q["id"], "to": cp["id"], "prev_at": q["at"]})
                elif cp["id"] == q["id"] and cp.get("root") != q["root"]:
                    id_bad.append({"kind": "same id different root", "log": cp["log"], "at": h["at"], "id": cp["id"], "roots": [q["root"], cp.get("root")]})
            last_id[cp["log"]] = {"id": cp["id"], "root": cp.get("root"), "at": h["at"]}
    if first_with_id is None:
        check("checkpoint-id", True, {"note": "no line carries checkpoints[].id yet (PR 252 unmerged); nothing to read, nothing read as zero"},
              "ids absent on every line: old shape, not a finding", ids=0)
    else:
        check("checkpoint-id", not id_missing and not id_bad,
              {"first_line_with_id": first_with_id, "ids_seen": ids_seen, "unreadable": id_missing[:20], "bad": id_bad[:20]},
              "after the first line with checkpoints[].id, every line carries one per log; ids never fall; same id => same root",
              ids=ids_seen, unreadable=len(id_missing), bad=len(id_bad))

    # ---- latest recorded head vs live ----
    newest = next((h for h in reversed(heads) if isinstance(h.get("checkpoints"), list)), None)
    if live_cp and newest:
        live_by = {c["log"]: c for c in live_cp["checkpoints"]}
        res, ok = {}, True
        for cp in newest["checkpoints"]:
            lc = live_by.get(cp["log"])
            if not lc:
                res[cp["log"]] = "no live checkpoint"; ok = False; continue
            good = cp["tree_size"] <= lc["tree_size"] and (cp["tree_size"] != lc["tree_size"] or cp["root"] == lc["root"])
            ok &= good
            res[cp["log"]] = {"witnessed": cp["tree_size"], "live": lc["tree_size"], "same_size": cp["tree_size"] == lc["tree_size"],
                              "root_agrees": cp["root"] == lc["root"] if cp["tree_size"] == lc["tree_size"] else None, "ok": good}
        check("latest-vs-live", ok, {"witnessed_at": newest["at"], "live_now": live_cp["now_utc"], "logs": res},
              "witnessed size <= live; equal size => equal root")
    newest_h = next((h for h in reversed(heads) if isinstance(h.get("identity"), dict) and h["identity"].get("verified_through_id")), None)
    if not args.no_live and newest_h:
        idn, tr = newest_h["identity"], newest_h["treasury"]
        time.sleep(0.5)
        a = get(f"/api/attest?identity_from={idn['verified_through_id']}&identity_expect={idn['head']}&ledger_from={tr['verified_through_id']}&ledger_expect={tr['head']}")
        res = {}
        ok = True
        for chain, want in (("identity_log", idn), ("treasury", tr)):
            ch = a.get(chain, {})
            good = ch.get("status") == "verified" and ch.get("expect_matches") is True and ch.get("anchor_resolved_as_requested") is True
            ok &= good
            res[chain] = {"status": ch.get("status"), "expect_matches": ch.get("expect_matches"), "anchor_resolved_as_requested": ch.get("anchor_resolved_as_requested"),
                          "verified_through_id": ch.get("verified_through_id"), "witnessed_id": want["verified_through_id"], "witnessed_head": want["head"]}
        check("latest-head-attest", ok, {"witnessed_at": newest_h["at"], "attest": res}, "status verified + expect_matches true on both chains (README recipe)")

    # ---- cadence ----
    switch = parse_at(FIVE_MIN_SINCE)
    gaps, flagged = [], []
    for p, n in zip(heads, heads[1:]):
        t0, t1 = parse_at(p["at"]), parse_at(n["at"])
        g = (t1 - t0).total_seconds()
        expected = 300 if t0 >= switch else 3600
        gaps.append(g)
        if g > 2 * expected:
            flagged.append({"from": p["at"], "to": n["at"], "gap_s": int(g), "expected_s": expected})
    windows = []
    for f in flagged:
        if windows and windows[-1]["to"] == f["from"]:
            w = windows[-1]
            w["to"], w["runs_inside"], w["worst_gap_s"] = f["to"], w["runs_inside"] + 1, max(w["worst_gap_s"], f["gap_s"])
        else:
            windows.append({"from": f["from"], "to": f["to"], "runs_inside": 1, "worst_gap_s": f["gap_s"], "expected_s": f["expected_s"]})
    for w in windows:
        w["duration_h"] = round((parse_at(w["to"]) - parse_at(w["from"])).total_seconds() / 3600, 2)
        # a missed slot or two is the dispatch leg hiccupping; an hour or more means the five-minute leg was down
        w["severity"] = "outage" if w["duration_h"] >= 1 else "missed-slot"
    five = [g for (p, g) in zip(heads, gaps) if parse_at(p["at"]) >= switch]
    stats = {"head_lines": len(heads), "gaps": len(gaps),
             "median_gap_s": statistics.median(gaps) if gaps else None, "max_gap_s": max(gaps) if gaps else None,
             "p90_gap_s": sorted(gaps)[int(0.9 * (len(gaps) - 1))] if gaps else None,
             "five_minute_era": {"gaps": len(five), "within_6min": sum(1 for g in five if g <= 360), "over_10min": sum(1 for g in five if g > 600),
                                 "median_s": statistics.median(five) if five else None},
             "flagged_gaps": len(flagged), "degraded_windows": windows}
    if join:
        stats["midnight_join"] = {"yesterday_last": join["at"], "today_first": heads[1]["at"] if len(heads) > 1 else None}
    check("cadence", not windows, stats, "no gap > 2x expected cadence (expected 5 min after 2026-08-12T03:36:59Z, 60 min before)")
    # Age of the newest head line: the witness is a five-minute job, so a newest line older than three slots means the
    # job is not running NOW, whatever the day's history looks like. Today only (a past day's newest line is old by
    # definition). Clock caution (egress c59105): 'at' is the runner's clock; ours is compared loosely (15 min).
    if not args.all and days[0] == utc_today() and heads:
        newest_at = parse_at(heads[-1]["at"])
        age_s = (datetime.datetime.now(datetime.UTC) - newest_at).total_seconds()
        check("newest-line-age", age_s <= 15 * 60, {"newest_at": heads[-1]["at"], "age_s": int(age_s)},
              "newest head line within 15 min (three five-minute slots) of now")
    outages = [w for w in windows if w["severity"] == "outage"]
    check("outage", not outages, outages, "no degraded window of 1 h or longer", missed_slot_windows=len(windows) - len(outages))

    all_ok = all(r["pass"] for r in checks)
    print(json.dumps({"scope": scope, "days": len(days), "per_day": per_day, "unparsed": bad[:10], "live_registry_key": live_key,
                      "checks": checks, "all_pass": all_ok}, indent=1, ensure_ascii=False))
    if args.record:
        import record
        c = record.connect()
        for row in checks:
            seq, _ = record.add(c, "check", "agent", row)
            print(f"recorded check #{seq} {row['target']} pass={row['pass']}", file=sys.stderr)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
