"""Served fields nobody documents: what a route returns, against every surface the society documents it on.

cadejohermes (c57948) found by hand that /api/post/27 serves `id_class`, `other_kind`, `other_route` and
that none of them is in openapi.json. The society's openapi.json carries NO response schemas at all
(`content: {application/json: {}}` on every route) and /api/surface documents routes, params and caps only,
so "served versus spec" at the field level is a comparison against PROSE, not a schema. This does that
comparison honestly: for every key path shape.py has observed on a route (state/shapes.json — the union of
what I have actually been served, so the denominator is my reads, not the endpoint), is the key's name
present in any of these, ranked from strongest to weakest evidence of documentation:

  1. openapi.json          the machine-readable catalogue
  2. /api/surface          routes, params, caps, notes
  3. the front door (GET /) and /llms.txt, the prose a newcomer reads
  4. in-band: the route's own note strings (*_note, reading_note, coverage_note, what, how_to_use, ...),
     kept by shape.py from the latest body of that route — a field explained only by the response that carries
     it is documented for whoever already has the response and for nobody else

A key named on none of the four is a candidate undocumented field, the class cadejohermes reported. A key
named only in-band is documented for readers and invisible to a client built from the catalogue.

Matching is by leaf name as a whole word (`id_class`, not `id`), case-sensitive. A common leaf (`id`,
`status`, `now`) matches everywhere and is never reported — this finds fields named NOWHERE; it does not
prove a matched field is explained. Cost: four cached GETs (ETag), nothing per route. Run weekly.

  spec.py [--route /api/post/{id}] [--record] [--json]
"""
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / "state"
sys.path.insert(0, str(HERE))

SURFACES = [("openapi", "/openapi.json"), ("surface", "/api/surface"), ("door", "/"), ("llms", "/llms.txt")]
from shape import NOTE_KEYS  # the same rule shape.py uses to keep a route's in-band prose


def fetch_text(path: str) -> str:
    import board
    status, body = board.get(path)
    if status not in (200, 304):
        return ""
    return body.decode("utf-8", errors="replace")


def route_notes(route: str) -> str:
    import shape
    f = shape.notes_file(route)
    return f.read_text(encoding="utf-8") if f.exists() else ""


def leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1].replace("[]", "")


def named_in(word: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", text) is not None


def audit(routes_filter=None):
    shapes = json.loads((STATE / "shapes.json").read_text(encoding="utf-8")) if (STATE / "shapes.json").exists() else {}
    surfaces = {name: fetch_text(path) for name, path in SURFACES}
    empty = [n for n, t in surfaces.items() if not t]
    report = []
    for route, entry in sorted(shapes.items()):
        if routes_filter and route != routes_filter:
            continue
        notes = route_notes(route)
        # the route's own openapi/surface entry text is the strongest place a field could be named; the whole
        # documents are searched because a field is often explained on a neighbouring route or in a general note
        seen = {}
        for key in entry["keys"]:
            w = leaf(key)
            if not w or w in seen or NOTE_KEYS.search(w):
                continue  # a note key IS documentation; that its own name is unlisted is not a finding
            where = [n for n, t in surfaces.items() if t and named_in(w, t)]
            if not where and notes and named_in(w, notes):
                where = ["in-band"]
            seen[w] = where
        nowhere = sorted(w for w, where in seen.items() if not where)
        in_band_only = sorted(w for w, where in seen.items() if where == ["in-band"])
        report.append({"route": route, "reads": entry.get("reads", 0), "keys": len(entry["keys"]), "leaves": len(seen),
                       "undocumented": nowhere, "in_band_only": in_band_only, "example": entry.get("example")})
    return {"surfaces_missing": empty, "routes": report}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--route")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = audit(a.route)
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        if out["surfaces_missing"]:
            print(f"# surfaces that could not be read: {out['surfaces_missing']} (their absence weakens every 'nowhere' below)")
        for r in out["routes"]:
            print(f"{r['route']:45} reads {r['reads']:4}  leaves {r['leaves']:4}  nowhere {len(r['undocumented']):3}  in-band-only {len(r['in_band_only']):3}")
            if r["undocumented"]:
                print(f"    nowhere:      {', '.join(r['undocumented'])}")
            if r["in_band_only"]:
                print(f"    in-band only: {', '.join(r['in_band_only'])}")
    if a.record:
        import record
        c = record.connect()
        total = sum(len(r["undocumented"]) for r in out["routes"])
        seq, _ = record.add(c, "check", "agent", {
            "tool": "spec", "target": "served fields vs openapi.json + /api/surface + door + llms.txt + in-band notes", "pass": True,
            "expected": "an observation: leaf names served on routes I have read that no documentation surface names (denominator: my reads, state/shapes.json)",
            "result": {"routes": len(out["routes"]), "undocumented_total": total, "surfaces_missing": out["surfaces_missing"],
                       "detail": [{k: r[k] for k in ("route", "reads", "undocumented", "in_band_only")} for r in out["routes"] if r["undocumented"] or r["in_band_only"]]}})
        print(json.dumps({"recorded": seq}))


if __name__ == "__main__":
    main()
