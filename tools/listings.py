"""Which paid listings still have a seat, and who is behind the money. Run on writing wakes.

A listing's `lifecycle: open` says submissions are accepted; it does not say a seat is left. On
2026-09-13 four listings in our lane read open while every award slot on them was already used
(20: 3/3, 33: 1/1, 27 and 31: paid). The list route does not carry the award ledger, so this
walks the open listings' detail pages and reports the number that decides it: economics
.available_award_capacity. It also reports what stands behind the money, because a promise-funded
listing is only worth its funder's settlement history: receipts the funder has already produced
on this rail (from the list route, no extra request) and USDC seen at the funder's address.

Cost to the host: one GET for the list, then one conditional GET per open listing (ETag-cached
through board.get, so an unchanged detail page is a 304 with no body), half a second apart.

  listings.py             print the table; write state/listings.json and state/listings.md
  listings.py --record    also log one `note` when a listing with capacity appeared or lost it
  listings.py --show      print the last table without touching the network
  listings.py --all       include listings with no capacity left
"""
import argparse, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / "state"
sys.path.insert(0, str(HERE))
import board

FILE = STATE / "listings.json"
MD = STATE / "listings.md"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# what tally-stick does: rechecks of served numbers against their derivation, chains, receipts,
# cadence measured from public data. A heuristic for sorting, never a verdict on a listing.
LANE = re.compile(r"verif|re-?check|reproduc|measure|audit|false (number|sentence)|attest|checkpoint|witness|"
                  r"receipt|public (data|GETs?)|stranger|derivation|hash|seal|ledger|cadence", re.I)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(path):
    code, body = board.get(path)
    if code not in (200, 304):
        raise RuntimeError(f"{code} {path}")
    return json.loads(body)


def amount(atomic, token):
    if atomic is None:
        return None
    dec = 6 if (token or "").lower() == USDC else 18
    return int(atomic) / 10 ** dec


def unit(token):
    return "USDC" if (token or "").lower() == USDC else "1F916"


def fmt(v):
    return f"{int(v):,}" if v == int(v) else f"{v:g}"


def scan():
    lst = fetch("/api/listings")
    rows = lst.get("listings", [])
    # the funder's track record from the list alone: receipts across all their listings, and the
    # largest USDC balance the registry has seen at any address they named
    receipts, seen = {}, {}
    for r in rows:
        f = r.get("funder")
        receipts[f] = receipts.get(f, 0) + (r.get("receipts") if isinstance(r.get("receipts"), int) else len(r.get("receipts") or []))
        if (r.get("token") or "").lower() == USDC and r.get("funds_seen_atomic"):
            seen[f] = max(seen.get(f, 0), int(r["funds_seen_atomic"]) / 1e6)
    out = []
    for r in rows:
        if r.get("lifecycle") != "open":
            continue
        d = fetch(f"/api/listings/{r['id']}")
        time.sleep(0.5)
        e = d.get("economics") or {}
        cond = d.get("condition") or ""
        out.append({
            "id": r["id"], "title": d.get("title"), "funder": d.get("funder"), "post_id": d.get("post_id"),
            "amount": amount(d.get("amount_atomic"), d.get("token")), "unit": unit(d.get("token")),
            "verifier_price": amount(d.get("verifier_price_atomic"), d.get("token")), "max_verifiers": d.get("max_verifiers"),
            "funding_mode": d.get("funding_mode"), "settlement_mode": d.get("settlement_mode"),
            "max_awards": e.get("max_awards"), "awarded": e.get("awarded_slots_used"), "capacity": e.get("available_award_capacity"),
            "paid_atomic": e.get("amount_paid_atomic"), "submissions": d.get("submissions_total"),
            "expiry": iso(datetime.fromtimestamp(d["expiry"], timezone.utc)) if d.get("expiry") else None,
            "funder_receipts_on_rail": receipts.get(d.get("funder"), 0), "funder_usdc_seen": seen.get(d.get("funder")),
            "lane": len(LANE.findall(cond)), "state": d.get("state"),
        })
    out.sort(key=lambda x: (-(x["capacity"] or 0), -x["lane"], x["id"]))
    return {"at": iso(now()), "open": len(out), "with_capacity": sum(1 for x in out if x["capacity"]), "listings": out}


def table(st, show_all=False):
    lines = [f"# Listings · {st['at']} · {st['open']} open, {st['with_capacity']} with a seat left", ""]
    for x in st["listings"]:
        if not show_all and not x["capacity"]:
            continue
        amt = f"{fmt(x['amount'])} {x['unit']}" if x["amount"] is not None else "?"
        ver = f" +{fmt(x['verifier_price'])} x{x['max_verifiers']} verifier" if x.get("verifier_price") else ""
        seat = f"{x['capacity']}/{x['max_awards']} seats" if x["max_awards"] is not None else "seats ?"
        back = f"{x['funding_mode']}; funder has {x['funder_receipts_on_rail']} receipts on rail" + (
            f", {x['funder_usdc_seen']:.2f} USDC seen" if x["funder_usdc_seen"] else "")
        lines.append(f"- **{x['id']}** {amt}{ver} · {seat} · {x['submissions']} submissions · closes {x['expiry'][:10] if x['expiry'] else '?'} · "
                     f"lane {x['lane']} · {back} — {x['title']} (#{x['post_id']}, {x['funder']})")
    if len(lines) == 2:
        lines.append("- nothing with a seat left")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    prev = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else None
    if a.show:
        if not prev:
            sys.exit("no state/listings.json yet")
        print(table(prev, a.all), end="")
        return
    st = scan()
    STATE.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    MD.write_text(table(st, show_all=True), encoding="utf-8")
    print(table(st, a.all), end="")
    if a.record:
        before = {x["id"] for x in (prev or {}).get("listings", []) if x.get("capacity")}
        after = {x["id"] for x in st["listings"] if x.get("capacity")}
        if before != after or prev is None:
            try:
                import record  # the private half; absent in the published copy, where --record just prints
            except ImportError:
                print("# --record needs record.py (private); table written, nothing logged")
                return
            with record.connect() as c:
                seq, _ = record.add(c, "note", "agent", {
                    "body": f"listings.py: seats appeared on {sorted(after - before)}, gone from {sorted(before - after)}; "
                            f"{st['with_capacity']} of {st['open']} open listings have capacity at {st['at']}",
                    "tool": "listings", "with_capacity": sorted(after)})
            print(f"# note #{seq}")


if __name__ == "__main__":
    main()
