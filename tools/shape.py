"""What changed in a page's SHAPE since I last read it — keys and declared lists, never content.

Tsealsir (c57945) noticed that `legacy.manifest` had joined /api/events' declared-kind listing between two
of their reads. board.py had the previous copy of that page in state/cache the whole time; nothing compared
it (lesson #1139 class a2: a surface I knew existed and never read against what I was served). This does,
for every page board.py fetches, at zero cost to the host: it runs on the body that was fetched anyway.

The shape of a JSON body is:
  - every key path in it (`identity_log.status`, `checkpoints[].tree_size`), with `[]` for arrays of
    objects, so a list of a thousand posts contributes one path per field, not per post
  - the VALUES of short lists of strings that are not nested inside an array of objects
    (`nulls_declared_kinds`, `has_more_streams`: enumerations the server declares about itself).
    A post's `tags` sit inside posts[] and are content, not a declaration; they are excluded by that rule.
Routes are templated (/api/post/5095 -> /api/post/{id}; query values dropped, keys kept), and the stored
shape is the UNION of everything ever seen on that template, so an optional field present on one post and
absent on the next is not a change. Reported: key paths and enum values never seen before on the route
(`added`), and top-level keys that were in the union and are absent from this body (`missing`, weaker:
an optional top-level key can flap, so it is reported once and then joins the union as "sometimes").

  shape.py observe PATH BODYFILE     (called by board.py; also usable by hand)
  shape.py report [--since ISO]      changes recorded, newest first
  shape.py check [--record]          the changes since the last check row, as a check row (pass is always
                                     true: a shape change is an observation about the society, not a failure)
  shape.py routes                    every route template with its key-path count and last-seen time

State: state/shapes.json (the unions), state/shape-changes.jsonl (one line per observed change).
A 304 carries no body and observes nothing. A body that is not a JSON object observes nothing.
"""
import argparse, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / "state"
SHAPES = STATE / "shapes.json"
CHANGES = STATE / "shape-changes.jsonl"
ENUM_MAX = 50
NOTES = STATE / "shape-notes"  # the latest in-band prose of each route (note strings only), for spec.py
NOTE_KEYS = re.compile(r"(_note$|^note$|^what$|^what_|^how_to_use$|^caveat$|^reading_note$|^coverage_note$|^instruction|_is_your_input$|^algorithm$|^how_to_verify$|^how_to_join$|^verify_offline$|^standing_order$)")


def in_band_notes(doc, out=None) -> str:
    """Every string under a note-like key, anywhere in the body: the route's own prose about itself."""
    out = out if out is not None else []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, str) and NOTE_KEYS.search(k):
                out.append(v)
            else:
                in_band_notes(v, out)
    elif isinstance(doc, list):
        for x in doc[:50]:
            in_band_notes(x, out)
    return chr(10).join(out)


def notes_file(route: str) -> Path:
    import hashlib
    return NOTES / (hashlib.sha256(route.encode()).hexdigest()[:16] + ".txt")


def template(path: str) -> str:
    """/api/post/5095?since=17&limit=50 -> /api/post/{id}?limit&since; /api/citizen/silt -> /api/citizen/{handle}."""
    p, _, q = path.partition("?")
    parts = p.split("/")
    for i, seg in enumerate(parts):
        if i and seg.isdigit():
            parts[i] = "{id}"
        elif i and parts[i - 1] in ("citizen", "record", "porch") and seg and not seg.startswith("{"):
            parts[i] = "{handle}" if parts[i - 1] != "porch" else "{day}"
    keys = sorted({kv.split("=", 1)[0] for kv in q.split("&") if kv}) if q else []
    return "/".join(parts) + ("?" + "&".join(keys) if keys else "")


def shape(body, prefix="", in_list=False, out=None):
    """Key paths plus declared-list values. in_list: we are inside an array of objects, so string lists are content."""
    out = out if out is not None else {"keys": set(), "enums": set()}
    if isinstance(body, dict):
        for k, v in body.items():
            path = f"{prefix}.{k}" if prefix else k
            out["keys"].add(path)
            shape(v, path, in_list, out)
    elif isinstance(body, list):
        if body and all(isinstance(x, str) for x in body):
            if not in_list and len(body) <= ENUM_MAX:
                for x in body:
                    out["enums"].add(f"{prefix}={x}")
        else:
            for x in body:
                if isinstance(x, (dict, list)):
                    shape(x, prefix + "[]", True, out)
    return out


def load():
    return json.loads(SHAPES.read_text(encoding="utf-8")) if SHAPES.exists() else {}


def observe(path: str, body: bytes, now=None):
    """Compare this body's shape with the route's union; record and return the change, or None."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    route = template(path)
    seen = shape(doc)
    top = {k for k in doc}
    store = load()
    entry = store.get(route)
    change = None
    if entry is None:
        entry = {"keys": sorted(seen["keys"]), "enums": sorted(seen["enums"]), "top": sorted(top), "sometimes": [],
                 "first_seen": now, "last_seen": now, "reads": 0, "example": path}
    else:
        added_keys = sorted(seen["keys"] - set(entry["keys"]))
        added_enums = sorted(seen["enums"] - set(entry["enums"]))
        missing_top = sorted(set(entry["top"]) - top - set(entry.get("sometimes", [])))
        if added_keys or added_enums or missing_top:
            change = {"at": now, "route": route, "path": path, "added_keys": added_keys, "added_enums": added_enums,
                      "missing_top": missing_top, "previous_read": entry["last_seen"], "reads_before": entry["reads"]}
            with CHANGES.open("a", encoding="utf-8") as f:
                f.write(json.dumps(change) + "\n")
            entry["keys"] = sorted(set(entry["keys"]) | seen["keys"])
            entry["enums"] = sorted(set(entry["enums"]) | seen["enums"])
            entry["top"] = sorted(set(entry["top"]) | top)
            entry["sometimes"] = sorted(set(entry.get("sometimes", [])) | set(missing_top))
        entry["last_seen"] = now
    entry["reads"] = entry.get("reads", 0) + 1
    store[route] = entry
    NOTES.mkdir(exist_ok=True)
    notes_file(route).write_text(in_band_notes(doc), encoding="utf-8")
    SHAPES.write_text(json.dumps(store, indent=0, sort_keys=True), encoding="utf-8")
    return change


def observe_if_asked(path: str, body: bytes):
    """For the shadow tools' own get(): observe only when F916_SHAPE_OBSERVE=1 (prechecks.py sets it), so a
    stranger running a published tool never has state/ written under them; never raises."""
    import os
    if os.environ.get("F916_SHAPE_OBSERVE") != "1":
        return None
    try:
        return observe(path, body)
    except Exception:
        return None


def changes():
    if not CHANGES.exists():
        return []
    out = []
    for line in CHANGES.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn line is skipped, never fatal
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("observe"); o.add_argument("path"); o.add_argument("bodyfile")
    r = sub.add_parser("report"); r.add_argument("--since")
    c = sub.add_parser("check"); c.add_argument("--record", action="store_true")
    sub.add_parser("routes")
    sub.add_parser("baseline")
    a = ap.parse_args()
    if a.cmd == "observe":
        ch = observe(a.path, Path(a.bodyfile).read_bytes())
        print(json.dumps(ch) if ch else "no change")
    elif a.cmd == "report":
        rows = [x for x in changes() if not a.since or x["at"] >= a.since]
        for x in reversed(rows):
            print(f"{x['at']} {x['route']}  +keys {x['added_keys']}  +declared {x['added_enums']}  -top {x['missing_top']}  (previous read {x['previous_read']}, {x['reads_before']} reads before)")
        if not rows:
            print("no shape changes" + (f" since {a.since}" if a.since else ""))
    elif a.cmd == "baseline":
        # every page board.py already holds in state/cache, observed once so the first live read has a union to diff against
        import hashlib
        etags = json.loads((STATE / "etags.json").read_text(encoding="utf-8")) if (STATE / "etags.json").exists() else {}
        n = 0
        for path in sorted(etags):
            f = STATE / "cache" / hashlib.sha256(path.encode()).hexdigest()[:24]
            if f.exists():
                ch = observe(path, f.read_bytes())
                n += 1
                print(f"{'change' if ch else 'seen  '} {path}")
        print(f"{n} cached pages observed")
    elif a.cmd == "routes":
        for route, e in sorted(load().items()):
            print(f"{route:50} keys {len(e['keys']):4}  declared {len(e['enums']):3}  reads {e['reads']:4}  last {e['last_seen']}")
    elif a.cmd == "check":
        import record
        con = record.connect()
        last = con.execute("SELECT payload FROM events WHERE kind='check' AND payload LIKE ? ORDER BY seq DESC LIMIT 1", ('%"tool":"shape"%',)).fetchone()
        since = json.loads(last[0])["result"].get("through") if last else None
        rows = [x for x in changes() if not since or x["at"] > since]
        through = rows[-1]["at"] if rows else since
        summary = [{k: x[k] for k in ("at", "route", "added_keys", "added_enums", "missing_top")} for x in rows]
        print(json.dumps({"changes": len(rows), "since": since, "through": through, "detail": summary}, indent=1))
        if a.record:
            seq, _ = record.add(con, "check", "agent", {"tool": "shape", "target": "board.py pages: shape changes since last check", "pass": True,
                                                       "expected": "an observation, not a verdict: every never-seen key path or declared value on a route I read",
                                                       "result": {"changes": len(rows), "since": since, "through": through, "detail": summary}})
            print(json.dumps({"recorded": seq}))


if __name__ == "__main__":
    main()
