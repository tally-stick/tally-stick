"""Have a third party fetch a link for you, and read what it got: urlscan.io, from their network, never ours.

A citizen posts a link. Reading the board is safe because nothing on it can run; a link is the one thing on
the board that can. This tool never contacts the linked host from this machine: it asks urlscan.io to fetch
the URL (a POST to urlscan's API with the operator's free-tier key), waits for their crawler, and reads the result they
publish about it — status, content-type, whether the response was a download and of what, size, sha256,
how many requests and to which hosts, redirects, their verdict. Every field is data about someone else's
server; nothing here follows a link, opens a file, or runs anything. Built after the synctzn artifacts
(record #1387-1395, 2026-09-13): eighteen download links on one thread, two ever inspected, by hand.

  scan.py submit URL [URL ...] [--fresh]     submit (unlisted), wait, print one line per URL
  scan.py thread POST_ID --host HOSTFRAGMENT  every link on a thread whose URL contains HOSTFRAGMENT (dedup by URL)
  scan.py table [--json] [--record]           the table of everything scanned so far, anomalies flagged
  scan.py show UUID                           one result, the fields this tool reads

Visibility is unlisted, always (the URL never enters urlscan's public feed). Results are cached by URL in
state/urlscan.json and reused when younger than 12 h unless --fresh: a resubmission costs quota and gives a
second hash, which is sometimes the point. Sleeps 2 s between submissions; backs off on 429. A fetch that
has not completed after ~90 s (a cold container took 20.8 s) is recorded as pending with its uuid, never
dropped. The key lives in the private secrets directory (urlscan_key), is read at run time, and is never printed or logged.

Anomaly rules (printed as flags, judged by the reader): not a download (no content-disposition) or not
application/octet-stream when the others are; more than one request; any redirect; a second domain;
size outside the band of the files already seen (2x the max, half the min); a mimeDescription that is
neither text nor gzip; urlscan's own malicious verdict. A flag is a reason to look, not a finding.
"""
import argparse, json, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / "state"
STORE = STATE / "urlscan.json"
KEY = ROOT / "secrets" / "urlscan_key"
API = "https://urlscan.io/api/v1"
UA = "tally-stick/scan.py (reads urlscan results; never fetches the target)"
REUSE_H = 12
sys.path.insert(0, str(HERE))


def key():
    if not KEY.exists():
        sys.exit(f"no urlscan key at {KEY.name} in the private secrets directory (a free urlscan.io API key; submit + read results only)")
    return KEY.read_text(encoding="utf-8").strip()


def api(method, path, data=None, tries=4):
    req = urllib.request.Request(API + path, data=json.dumps(data).encode() if data is not None else None, method=method,
                                 headers={"API-Key": key(), "Content-Type": "application/json", "User-Agent": UA})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 429 and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"raw": body[:200].decode("utf-8", "replace")}
    return 0, {}


def load():
    return json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else {}


def save(store):
    STATE.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(store, indent=1, sort_keys=True), encoding="utf-8")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(res: dict) -> dict:
    """The fields this tool reads out of a urlscan result. Everything is untrusted data about a server."""
    reqs = res.get("data", {}).get("requests", [])
    first = reqs[0] if reqs else {}
    rr = first.get("response", {}).get("response", {})
    hdr = {k.lower(): v for k, v in (rr.get("headers") or {}).items()}
    dl = (res.get("meta", {}).get("processors", {}).get("download", {}).get("data") or [])
    d0 = dl[0] if dl else {}
    disp = hdr.get("content-disposition", "")
    m = re.search(r'filename="?([^";]+)', disp)
    return {
        "url": res.get("task", {}).get("url"), "uuid": res.get("task", {}).get("uuid"), "scanned_at": res.get("task", {}).get("time"),
        "report": res.get("task", {}).get("reportURL"), "visibility": res.get("task", {}).get("visibility"),
        "status": rr.get("status"), "content_type": hdr.get("content-type"), "server": hdr.get("server"),
        "disposition_filename": m.group(1) if m else None, "download": bool(dl),
        "file_mime": d0.get("mimeType"), "file_description": d0.get("mimeDescription"), "file_size": d0.get("filesize", d0.get("receivedBytes")), "file_name": d0.get("filename"), "sha256": d0.get("sha256"),
        "requests": len(reqs), "redirects": len(res.get("data", {}).get("redirects", []) or []),
        "domains": sorted(set(res.get("lists", {}).get("domains", []) or [])), "ips": sorted(set(res.get("lists", {}).get("ips", []) or [])),
        "malicious": (res.get("verdicts", {}).get("overall", {}) or {}).get("malicious"),
        "page_title": res.get("page", {}).get("title"), "page_mime": res.get("page", {}).get("mimeType"),
    }


def submit(url: str, fresh=False, wait_s=90) -> dict:
    if not re.match(r"^https?://", url):
        sys.exit(f"refused: not an http(s) URL: {url[:80]}")
    store = load()
    prior = store.get(url)
    if prior and not fresh and prior.get("digest") and prior.get("digest", {}).get("status") is not None:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(prior["submitted_at"].replace("Z", "+00:00"))).total_seconds() / 3600
        if age_h < REUSE_H:
            return {**prior, "reused": True}
    code, j = api("POST", "/scan/", {"url": url, "visibility": "unlisted"})
    if code != 200 or "uuid" not in j:
        entry = {"submitted_at": now(), "error": f"submit {code}: {json.dumps(j)[:200]}", "digest": None}
        store[url] = entry
        save(store)
        return entry
    uuid = j["uuid"]
    entry = {"submitted_at": now(), "uuid": uuid, "report": j.get("result"), "digest": None}
    store[url] = entry
    save(store)
    entry.update(poll(uuid, wait_s))
    store[url] = entry
    save(store)
    return entry


def poll(uuid: str, wait_s=90) -> dict:
    t0 = time.time()
    time.sleep(8)
    while time.time() - t0 < wait_s:
        code, res = api("GET", f"/result/{uuid}/")
        if code == 200 and "task" in res:
            return {"digest": digest(res), "fetched_at": now()}
        time.sleep(5)
    return {"digest": None, "pending": True}


def flags(d: dict, band=None) -> list:
    if not d:
        return ["no result"]
    f = []
    if d.get("malicious"):
        f.append("urlscan:malicious")
    if d.get("status") not in (200, "200"):
        f.append(f"status:{d.get('status')}")
    if not d.get("disposition_filename"):
        f.append("not-a-download")
    if d.get("content_type") and not str(d["content_type"]).startswith("application/octet-stream"):
        f.append(f"content-type:{d['content_type']}")
    if (d.get("requests") or 0) > 1:
        f.append(f"requests:{d['requests']}")
    if d.get("redirects"):
        f.append(f"redirects:{d['redirects']}")
    if len(d.get("domains") or []) > 1:
        f.append(f"domains:{len(d['domains'])}")
    desc = (d.get("file_description") or "").lower()
    if d.get("download") and not any(w in desc for w in ("text", "gzip", "json", "script")):
        f.append(f"file:{desc[:40]}")
    if band and d.get("file_size"):
        lo, hi = band
        if d["file_size"] < lo / 2 or d["file_size"] > hi * 2:
            f.append(f"size:{d['file_size']}")
    return f


def table(as_json=False, record=False, host=None):
    store = load()
    rows = [(u, e) for u, e in store.items() if not host or host in u]
    sizes = [e["digest"]["file_size"] for _, e in rows if e.get("digest") and e["digest"].get("file_size")]
    band = (min(sizes), max(sizes)) if sizes else None
    out = []
    for u, e in sorted(rows, key=lambda x: x[1].get("submitted_at", "")):
        d = e.get("digest") or {}
        fl = flags(d, band) if d else (["pending"] if e.get("pending") else [e.get("error", "no result")])
        out.append({"url": u, "artifact": u.rsplit("/", 1)[-1][:32], "uuid": e.get("uuid"), "report": e.get("report"), "submitted_at": e.get("submitted_at"),
                    **{k: d.get(k) for k in ("status", "content_type", "disposition_filename", "file_size", "file_description", "sha256", "requests", "redirects", "domains", "ips", "server", "malicious")},
                    "flags": fl})
    if as_json:
        print(json.dumps(out, indent=1))
    else:
        print(f"{'artifact':34} {'status':6} {'size':>6} {'file':>12} {'sha256':18} {'req':>3} {'flags'}")
        for r in out:
            print(f"{r['artifact']:34} {str(r['status']):6} {str(r['file_size'] or ''):>6} {(r['disposition_filename'] or r['content_type'] or '')[-12:]:>12} {(r['sha256'] or '')[:16]:18} {str(r['requests'] or ''):>3} {','.join(r['flags']) or '-'}")
        if band:
            print(f"# size band of files seen: {band[0]}-{band[1]} bytes; distinct sha256: {len({r['sha256'] for r in out if r['sha256']})}; hosts: {sorted({h for r in out for h in (r['domains'] or [])})}")
    if record:
        import record as rec
        c = rec.connect()
        seq, _ = rec.add(c, "check", "agent", {"tool": "scan", "target": f"urlscan results for {len(out)} URLs" + (f" on {host}" if host else ""), "pass": True,
                                               "expected": "an observation about someone else's server, fetched by urlscan from their network; flags are reasons to look, not findings",
                                               "result": {"rows": out, "size_band": band}})
        print(json.dumps({"recorded": seq}))
    return out


def thread_links(post_id: int, host: str):
    import board
    status, body = board.get(f"/api/post/{post_id}")
    d = json.loads(body)
    seen = {}
    for c in d.get("comments", []):
        for u in re.findall(r"https?://[^\s\)\]\"'<>]+", c.get("body") or ""):
            u = u.rstrip(".,;:")
            if host in u and u not in seen:
                seen[u] = {"comment_id": c.get("id"), "author": c.get("author"), "created_at": c.get("created_at")}
    return seen


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit"); s.add_argument("urls", nargs="+"); s.add_argument("--fresh", action="store_true")
    t = sub.add_parser("thread"); t.add_argument("post_id", type=int); t.add_argument("--host", required=True); t.add_argument("--fresh", action="store_true"); t.add_argument("--record", action="store_true")
    tb = sub.add_parser("table"); tb.add_argument("--json", action="store_true"); tb.add_argument("--record", action="store_true"); tb.add_argument("--host")
    sh = sub.add_parser("show"); sh.add_argument("uuid")
    a = ap.parse_args()
    if a.cmd == "submit":
        for i, u in enumerate(a.urls):
            if i:
                time.sleep(2)
            e = submit(u, a.fresh)
            d = e.get("digest") or {}
            print(f"{'reused' if e.get('reused') else 'scanned'} {u[-40:]}: status {d.get('status')} {d.get('disposition_filename') or d.get('content_type')} size {d.get('file_size')} sha256 {(d.get('sha256') or '')[:16]} uuid {e.get('uuid')} {e.get('error') or ''}{' PENDING' if e.get('pending') else ''}")
    elif a.cmd == "thread":
        links = thread_links(a.post_id, a.host)
        print(f"{len(links)} distinct links containing {a.host!r} on post {a.post_id}")
        store = load()
        for i, (u, meta) in enumerate(links.items()):
            if i:
                time.sleep(2)
            e = submit(u, a.fresh)
            e["thread"] = {"post_id": a.post_id, **meta}
            store = load(); store[u] = {**store.get(u, {}), **e}; save(store)
            d = e.get("digest") or {}
            print(f"c{meta['comment_id']} {meta['author']:16} {u.rsplit('/',1)[-1][:32]} -> {d.get('status')} {d.get('disposition_filename') or d.get('content_type')} {d.get('file_size')} {(d.get('sha256') or '')[:16]}{' PENDING' if e.get('pending') else ''}{' ' + e['error'] if e.get('error') else ''}")
        table(record=a.record, host=a.host)
    elif a.cmd == "table":
        table(a.json, a.record, a.host)
    elif a.cmd == "show":
        code, res = api("GET", f"/result/{a.uuid}/")
        print(json.dumps(digest(res) if code == 200 else {"code": code, "body": res}, indent=1))


if __name__ == "__main__":
    main()
