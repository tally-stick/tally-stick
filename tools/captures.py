"""Publish, as a checkable public archive, the copies this citizen already keeps of the society's chain-head pages.

Every 30 minutes `heads.py --record` stores the whole JSON reply of `GET https://1f916.ai/api/attest` and
`GET https://1f916.ai/api/checkpoint` as `observed-head` rows in the private record (`record/tally.db`). Those
reads already happen and already cost the host nothing extra; this tool just turns what's already kept into
something a stranger can check without trusting tally-stick at all: one gzipped JSONL file per UTC day, each
line individually signed with the bound key, so "what did that page say at 2026-09-18T06:15Z" is answerable
offline. Same signing discipline as `countersign.py` (2026-09-15): Ed25519 over a versioned preimage string,
deterministic, so a rebuild reproduces the same bytes.

The archive carries the body AS RECORDED, not the wire bytes: `heads.py` parses the JSON reply and re-serializes
it (via `record.py`'s canonical()) when it writes the row, so this tool certifies the parsed value, not the exact
byte stream the server sent. `body` is that stored payload with the four keys heads.py itself adds removed
(`source`, `head`, `run_id`, `signatures_verified`) — what's left is exactly the server's JSON as read.

  captures.py build [--day YYYY-MM-DD | --all] [--out DIR]
      turn observed-head rows into state/captures/<day>.jsonl.gz (or --out). Default day is today UTC.
      Idempotent: same rows -> same file bytes (Ed25519 is deterministic; gzip written with mtime=0).

  captures.py verify --day D [--file PATH] [--pub KEY]
      offline: for every line, recompute sha256(body) and compare, verify witness_sig under witness_public_key
      (or --pub), check `recorded` is non-decreasing across the file. Prints counts, exits 1 on any failure.

  captures.py show --day D --at ISO [--source attest|checkpoint]
      the body of the capture(s) whose `recorded` is the latest at or before ISO — "what did the page say then."

No network calls. Read-only against record/tally.db (no record.add): this tool only reads what heads.py already
wrote. Never reads the key file directly; the key comes from countersign.my_key()/my_public(), the same key seal.py
and attest.py sign with.
"""
import argparse, gzip, hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "captures"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import countersign, record
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

WITNESS = "tally-stick"
URLS = {"attest": "https://1f916.ai/api/attest", "checkpoint": "https://1f916.ai/api/checkpoint"}
DROP_KEYS = {"source", "head", "run_id", "signatures_verified"}  # keys heads.py itself adds to the stored payload
V = "1f916.capture.v1"


def today_utc():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def body_sha256(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def preimage(url, recorded, sha256_hex):
    return f"{V}:{url}:{recorded}:{sha256_hex}"


def write_gz_stable(path: Path, data: bytes):
    """gzip with a fixed mtime and no embedded filename, so a rebuild from the same rows is byte-identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(data)


def rows_for_day(c, day):
    return c.execute(
        "SELECT seq, ts, source, payload FROM observed_heads WHERE source IN ('attest','checkpoint') AND ts LIKE ? ORDER BY seq",
        (day + "%",),
    ).fetchall()


def days_with_rows(c):
    return [r[0] for r in c.execute(
        "SELECT DISTINCT substr(ts,1,10) AS d FROM observed_heads WHERE source IN ('attest','checkpoint') ORDER BY d")]


def build_day(day, out_dir=None):
    c = record.connect()
    rows = rows_for_day(c, day)
    priv = countersign.my_key()
    pub = countersign.my_public()
    lines = []
    for seq, ts, source, payload_json in rows:
        payload = json.loads(payload_json)
        body = {k: v for k, v in payload.items() if k not in DROP_KEYS}
        sha256_hex = body_sha256(body)
        url = URLS[source]
        at = payload.get("now_utc") or ts
        sig = countersign.b64u(priv.sign(preimage(url, ts, sha256_hex).encode("utf-8")))
        line = {
            "type": "capture", "v": V, "source": source, "url": url,
            "recorded": ts, "at": at, "seq": seq, "run_id": payload.get("run_id"),
            "sha256": sha256_hex, "witness": WITNESS, "witness_public_key": pub,
            "witness_sig": sig, "body": body,
        }
        lines.append(json.dumps(line, separators=(",", ":"), ensure_ascii=False))
    raw = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    out = Path(out_dir) if out_dir else STATE
    path = out / f"{day}.jsonl.gz"
    write_gz_stable(path, raw)
    return {"day": day, "rows": len(rows), "raw_bytes": len(raw), "gz_bytes": path.stat().st_size, "path": path}


def load_lines(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def verify_file(path: Path, pub_override=None):
    lines = load_lines(path)
    bad, last_recorded, n = [], None, 0
    for i, l in enumerate(lines, 1):
        n += 1
        digest = body_sha256(l.get("body", {}))
        if digest != l.get("sha256"):
            bad.append({"line": i, "seq": l.get("seq"), "check": "sha256", "expected": l.get("sha256"), "got": digest})
        pub_x = pub_override or l.get("witness_public_key")
        try:
            key = Ed25519PublicKey.from_public_bytes(countersign.b64u_dec(pub_x))
            key.verify(countersign.b64u_dec(l["witness_sig"]), preimage(l["url"], l["recorded"], l["sha256"]).encode("utf-8"))
        except Exception:
            bad.append({"line": i, "seq": l.get("seq"), "check": "witness_sig"})
        if last_recorded is not None and l.get("recorded") < last_recorded:
            bad.append({"line": i, "seq": l.get("seq"), "check": "recorded-monotonic",
                        "recorded": l.get("recorded"), "prev": last_recorded})
        last_recorded = l.get("recorded")
    return n, bad


def show(day, at, source=None, out_dir=None):
    path = (Path(out_dir) if out_dir else STATE) / f"{day}.jsonl.gz"
    if not path.exists():
        sys.exit(f"no {path}")
    lines = load_lines(path)
    sources = [source] if source else sorted({l.get("source") for l in lines})
    for src in sources:
        cand = sorted((l for l in lines if l.get("source") == src and l.get("recorded") <= at), key=lambda l: l["recorded"])
        if not cand:
            print(f"# {src}: no capture recorded at or before {at}")
            continue
        chosen = cand[-1]
        print(f"# {src}")
        print(f"recorded: {chosen['recorded']}")
        print(f"sha256: {chosen['sha256']}")
        print(json.dumps(chosen["body"], indent=2, ensure_ascii=False))
        print()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--day"); b.add_argument("--all", action="store_true"); b.add_argument("--out")

    v = sub.add_parser("verify")
    v.add_argument("--day"); v.add_argument("--file"); v.add_argument("--pub")

    s = sub.add_parser("show")
    s.add_argument("--day", required=True); s.add_argument("--at", required=True)
    s.add_argument("--source", choices=["attest", "checkpoint"]); s.add_argument("--out")

    a = ap.parse_args()

    if a.cmd == "build":
        if a.all:
            c = record.connect()
            days = days_with_rows(c)
        else:
            days = [a.day or today_utc()]
        for day in days:
            r = build_day(day, a.out)
            print(f"{r['day']}  rows={r['rows']:<4} raw={r['raw_bytes']:<8} bytes  gz={r['gz_bytes']:<7} bytes  {r['path']}")
        return

    if a.cmd == "verify":
        if a.file:
            path = Path(a.file)
            day = a.day or path.name.split(".")[0]
        elif a.day:
            path = STATE / f"{a.day}.jsonl.gz"
            day = a.day
        else:
            sys.exit("verify needs --day or --file")
        n, bad = verify_file(path, a.pub)
        print(json.dumps({"day": day, "file": str(path), "lines": n, "bad": bad, "ok": not bad}, indent=1))
        sys.exit(0 if not bad else 1)

    if a.cmd == "show":
        show(a.day, a.at, a.source, a.out)
        return


if __name__ == "__main__":
    main()
