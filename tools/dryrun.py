"""Execute the witness workflow's shell step locally, for real, before it goes in a PR.

`pr.py lint` proves the step parses; this proves it RUNS: the actual `run:` block from the branch's
.github/workflows/witness.yml, in a throwaway checkout of that branch, with two shims on PATH:

  curl   answers from state/dryrun-cache/<sha256 of url>.json, filling the cache from the live server
         the first time a URL is seen (so a dry run costs the host at most what one real job run costs,
         and nothing on repeats); --offline refuses to fetch
  git    records the call and exits 0, so commit/pull/push touch nothing

WITNESS_KEY is unset, so the countersign block is skipped (it needs the society's key, which is not
ours to have). The bucket-dedup line for the current 5-minute window is removed from the scratch copy
of today's day file first, otherwise a live job that already ran this window makes the step skip.

Two LIVE scenarios, both run: `anchored` (day files present, the normal case) and `cold` (no day
files: the first line after a gap, which is the expensive unanchored read). For each the produced day
line must parse as JSON and carry the fields the day-file format promises; every key it carries must
be named in witness/README.md.

Then the SYNTHETIC scenarios (lesson #1139): the same step against chains the live server cannot
supply, answered by the branch's own src/chain.ts attest() through node:sqlite (dryrun_attest.mjs).
Live data is ~13k rows, so a step that pages past VERIFY_PAGE (20,000) is a code path no live dry
run ever executes; PR 236's loop shipped with pages=1 in every gate row and nobody noticed until a
citizen asked what it was tested against. Each synthetic scenario carries an EXPECTATION of the
line (status, identity status, pages, calls), so a new branch that never ran is red, not green:
  under         VERIFY_PAGE-1 rows, cold          verified, 1 page
  exact         VERIFY_PAGE rows, cold            verified, 1 page (the sentinel row decides, not rows.length)
  over          VERIFY_PAGE+431 rows, cold        verified through the tip, 2 pages
  anchored      over, yesterday's line at 13,047  verified, expect_matches true, 1 page
  far-anchor    over, anchored at row 31          verified, 2 pages
  two-over      2*VERIFY_PAGE+1000 rows, cold     verified, 3 pages
  tamper-p2     over, row VERIFY_PAGE+100 edited  unverified/broken, found on page 2
  tamper-below  tamper at 5,000, anchored 13,047  verified (the documented scope of an anchored line)
  wrong-head    over, anchored with a wrong hash  unverified/mismatch, expect_matches false
  cont-fails    over, continuation fetch dies     unverified/incomplete, pages 1, line still written
  cp-fails      over, checkpoint fetch dies       verified, checkpoints fetch_failed
  mutation      over, follow loop disabled        unverified/incomplete (the gate's own falsifier)
The .db seeds are cached under state/dryrun-cache/synthetic, keyed by the branch's chain.ts, so a
changed hash recipe rebuilds them. The checkpoint body is synthetic: the lag block on these lines is
not measured here. VERIFY_PAGE is read from the branch, never hardcoded.

  dryrun.py BRANCH [--offline] [--record] [--no-synthetic]

Exit 0 when every scenario passes. --record logs one `check` row per scenario (tool: pr-dryrun),
with the line's numbers in result so the row is data, not an exit code (lesson #736).
"""
import argparse, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FORK = ROOT.parent / "1f916-fork"
CACHE = ROOT / "state" / "dryrun-cache"
sys.path.insert(0, str(HERE))

SYNTH = CACHE / "synthetic"
ATTEST_MJS = HERE / "dryrun_attest.mjs"
NODE = shutil.which("node") or "node"
NODE_FLAGS = ["--experimental-strip-types", "--experimental-sqlite"]
CHECKPOINT_BODY = json.dumps({"registry_public_key": {"x": "synthetic"}, "checkpoints": [
    {"log": "identity_events", "tree_size": 1, "root": "r", "sig": "s", "created_at": 1},
    {"log": "ledger", "tree_size": 3, "root": "r", "sig": "s", "created_at": 1}]})
ANCHOR = 13047  # where the live log stood on 2026-09-13; any sealed id below the page works
REQUIRED = {"at", "bucket", "status", "identity", "treasury"}
REQUIRED_LOG = {"status", "head", "verified_through_id", "sealed_entries_total", "total_rows"}
BASH = shutil.which("bash") or "C:/Program Files/Git/usr/bin/bash.exe"
REAL_CURL = shutil.which("curl") or "curl"
# The step runs under the jq the GitHub runner has (ubuntu-24.04: 1.7.1), not the 1.8.2 on PATH; 1.8 accepts syntax 1.7
# rejects and that is how PR 236 broke production (#2260). Installed once outside PATH; pr.py tests programs under both.
RUNNER_JQ = Path(os.environ.get("F916_RUNNER_JQ") or (Path.home() / "tools" / "jq-1.7.1" / "jq.exe"))


def sh(*args, cwd=FORK, check=True, env=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and r.returncode:
        sys.exit(f"$ {' '.join(map(str, args))}\n{r.stdout[-800:]}{r.stderr[-800:]}")
    return r


def step_script(tree: Path):
    import yaml
    doc = yaml.safe_load((tree / ".github" / "workflows" / "witness.yml").read_text(encoding="utf-8"))
    steps = [s for j in doc["jobs"].values() for s in (j.get("steps") or []) if "run" in s]
    if len(steps) != 1:
        sys.exit(f"expected one run: step in witness.yml, found {len(steps)}")
    return steps[0]["run"]


def write_shims(shimdir: Path, offline: bool, synthetic=None):
    """synthetic = {"tree": worktree, "db": chain file} routes /api/attest to the branch's own attest() over that chain
    (dryrun_attest.mjs serve) and /api/checkpoint to a fixed body; DRYRUN_FAIL_URL in the env fails a matching URL."""
    shimdir.mkdir(parents=True, exist_ok=True)
    cache = CACHE.as_posix()
    if synthetic:
        curl = f"""#!/usr/bin/env bash
# dry-run curl, synthetic: /api/attest answered by the branch's src/chain.ts over a seeded chain; no socket
url="${{@: -1}}"
printf '%s\\n' "$url" >> "$DRYRUN_LOG.curl"
if [ -n "${{DRYRUN_FAIL_URL:-}}" ] && [[ "$url" == *"$DRYRUN_FAIL_URL"* ]]; then exit 22; fi
case "$url" in
  https://1f916.ai/api/checkpoint) printf '%s' '{CHECKPOINT_BODY}' ;;
  https://1f916.ai/api/attest|https://1f916.ai/api/attest\\?*) exec "{Path(NODE).as_posix()}" {' '.join(NODE_FLAGS)} "{ATTEST_MJS.as_posix()}" serve "{Path(synthetic['tree']).as_posix()}" "{Path(synthetic['db']).as_posix()}" "$url" 2>/dev/null ;;
  *) echo "dry-run: synthetic shim has no answer for $url" >&2; exit 22 ;;
esac
"""
    else:
        curl = f"""#!/usr/bin/env bash
# dry-run curl: last argument is the URL; serve from cache, else fetch once with the real curl
url="${{@: -1}}"
key=$(printf '%s' "$url" | sha256sum | cut -c1-64)
f="{cache}/$key.json"
printf '%s\\n' "$url" >> "$DRYRUN_LOG.curl"
if [ -f "$f" ]; then cat "$f"; exit 0; fi
if [ "${{DRYRUN_OFFLINE:-0}}" = "1" ]; then echo "dry-run: offline and no cached answer for $url" >&2; exit 22; fi
if "{REAL_CURL}" "$@" > "$f.tmp"; then mv "$f.tmp" "$f"; cat "$f"; exit 0; else rc=$?; rm -f "$f.tmp"; exit $rc; fi
"""
    git = """#!/usr/bin/env bash
# dry-run git: record the call, change nothing
printf '%s\\n' "git $*" >> "$DRYRUN_LOG.git"
exit 0
"""
    if not RUNNER_JQ.exists():
        raise SystemExit(f"runner jq not found at {RUNNER_JQ}: the dry run only counts under the runner's jq version (#2260)")
    jq = f"""#!/usr/bin/env bash
# dry-run jq: the runner's version, not the one on PATH
exec "{RUNNER_JQ.as_posix()}" "$@"
"""
    for name, body in (("curl", curl), ("git", git), ("jq", jq)):
        p = shimdir / name
        p.write_text(body, encoding="utf-8", newline="\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)


def upstream_line_keys():
    """Keys of the newest head line on upstream main: the day-file format as the society serves it today."""
    sh("git", "fetch", "-q", "upstream", "main", check=False)
    files = sorted(f for f in sh("git", "ls-tree", "--name-only", "upstream/main", "witness/").stdout.split() if f.endswith(".jsonl"))
    for f in reversed(files[-2:]):
        for l in reversed(sh("git", "show", f"upstream/main:{f}").stdout.splitlines()):
            try:
                d = json.loads(l)
            except json.JSONDecodeError:
                continue
            if isinstance(d.get("identity"), dict):
                return set(d) | {k for n in ("identity", "treasury") if isinstance(d.get(n), dict) for k in d[n]}
    return set()


def run_scenario(name, branch, script, offline, readme_text, base_keys, synthetic=None):
    """synthetic: {"db": chain file, "yesterday": a head line or None, "fail": url substring or None,
    "mutate": (old, new) applied to the step text, "expect": {...}} — see synthetic_scenarios below."""
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=f"f916-dryrun-{name.replace(chr(58), chr(45))}-"))  # scenario names carry a colon; paths cannot
    tree = tmp / "tree"
    sh("git", "worktree", "add", "-q", "--detach", str(tree), branch)
    try:
        today = datetime.now(timezone.utc)
        day = today.strftime("%Y-%m-%d")
        yday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        bucket = f"{today:%Y-%m-%dT%H}:{today.minute // 5 * 5:02d}"
        wdir = tree / "witness"
        if name == "cold" or synthetic:
            for f in wdir.glob("*.jsonl"):
                f.unlink()
            if synthetic and synthetic.get("yesterday"):
                (wdir / f"{yday}.jsonl").write_text(synthetic["yesterday"] + "\n", encoding="utf-8", newline="\n")
        else:
            f = wdir / f"{day}.jsonl"
            if f.exists():
                kept = [l for l in f.read_text(encoding="utf-8").splitlines() if f'"bucket":"{bucket}"' not in l]
                f.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8", newline="\n")
        before = (wdir / f"{day}.jsonl").read_text(encoding="utf-8").splitlines() if (wdir / f"{day}.jsonl").exists() else []
        shim = tmp / "shim"
        write_shims(shim, offline, {"tree": tree, "db": synthetic["db"]} if synthetic else None)
        if synthetic and synthetic.get("mutate"):
            old_text, new_text = synthetic["mutate"]
            if old_text not in script:
                return {"scenario": name, "pass": False, "exit": None, "seconds": 0.0, "curl": [], "git": [], "line": {},
                        "stdout_tail": [], "problems": [f"mutation target not in the step: {old_text!r} (a witness step with no follow loop is the defect of post 5095)"]}
            script = script.replace(old_text, new_text)
        (tree / ".dryrun-step.sh").write_text(script, encoding="utf-8", newline="\n")
        log = tmp / "log"
        env = {k: v for k, v in os.environ.items() if k not in ("WITNESS_KEY", "NODE_OPTIONS")}
        env.update({"PATH": shim.as_posix() + os.pathsep + env.get("PATH", ""), "DRYRUN_LOG": log.as_posix(),
                    "DRYRUN_OFFLINE": "1" if offline else "0", "RUNNER_TEMP": tmp.as_posix()})
        if synthetic and synthetic.get("fail"):
            env["DRYRUN_FAIL_URL"] = synthetic["fail"]
        r = subprocess.run([BASH, "-euo", "pipefail", ".dryrun-step.sh"], cwd=tree, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        curls = Path(str(log) + ".curl").read_text(encoding="utf-8").splitlines() if Path(str(log) + ".curl").exists() else []
        gits = Path(str(log) + ".git").read_text(encoding="utf-8").splitlines() if Path(str(log) + ".git").exists() else []
        after = (wdir / f"{day}.jsonl").read_text(encoding="utf-8").splitlines() if (wdir / f"{day}.jsonl").exists() else []
        new_lines = after[len(before):]
        problems = []
        if r.returncode != 0:
            problems.append(f"step exited {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
        if len(new_lines) != 1:
            problems.append(f"expected exactly one new day line, got {len(new_lines)}"
                            + (" (a step that writes no line is a silent gap in the day file)" if synthetic else ""))
        line = {}
        if new_lines:
            try:
                line = json.loads(new_lines[-1])
            except json.JSONDecodeError as e:
                problems.append(f"new line is not JSON: {e}")
        if line and synthetic:
            # the expectation is the point: a line that reads verified with pages=1 where 2 is expected
            # is a new code path that never ran, and that is red here (lesson #1139)
            ident = line.get("identity") if isinstance(line.get("identity"), dict) else {}
            got = {"status": line.get("status"), "identity": ident.get("status"), "pages": ident.get("pages"),
                   "expect_matches": ident.get("expect_matches"), "calls": sum("/api/attest" in c for c in curls),
                   "verified_through_id": ident.get("verified_through_id"), "checkpoints": line.get("checkpoints")}
            for k, want in synthetic["expect"].items():
                if got.get(k) != want:
                    problems.append(f"{k}: expected {want!r}, line has {got.get(k)!r}")
            if line.get("bucket") != bucket:
                problems.append(f"bucket {line.get('bucket')!r} != {bucket!r}")
        elif line:
            missing = REQUIRED - set(line)
            if missing:
                problems.append(f"line lacks {sorted(missing)}")
            for logname in ("identity", "treasury"):
                if isinstance(line.get(logname), dict):
                    m = REQUIRED_LOG - set(line[logname])
                    if m:
                        problems.append(f"{logname} lacks {sorted(m)}")
                    if line[logname].get("status") != "verified":
                        problems.append(f"{logname}.status is {line[logname].get('status')!r}, not verified")
                    if "expect_matches" in line[logname] and line[logname]["expect_matches"] is False:
                        problems.append(f"{logname}.expect_matches is false")
            if line.get("status") != "verified":
                problems.append(f"status is {line.get('status')!r}")
            if line.get("bucket") != bucket:
                problems.append(f"bucket {line.get('bucket')!r} != {bucket!r}")
            keys = set(line) | {k for logname in ("identity", "treasury") if isinstance(line.get(logname), dict) for k in line[logname]}
            # only keys this branch ADDS must be documented; the format already carries undocumented ones
            new_keys = keys - base_keys
            undocumented = sorted(k for k in new_keys if not re.search(r"\b" + re.escape(k) + r"\b", readme_text))
            if undocumented:
                problems.append(f"new keys not named in witness/README.md: {undocumented}")
        if not any(c.startswith("git commit") for c in gits):
            problems.append("step never reached git commit")
        if not any("/api/attest" in c for c in curls):
            problems.append("step never called /api/attest")
        return {"scenario": name, "pass": not problems, "problems": problems, "exit": r.returncode, "seconds": round(time.time() - t0, 1),
                "curl": curls, "git": gits, "line": line, "stdout_tail": r.stdout.strip().splitlines()[-3:]}
    finally:
        sh("git", "worktree", "remove", "--force", str(tree), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def node(*args):
    r = subprocess.run([NODE, *NODE_FLAGS, str(ATTEST_MJS), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"})
    if r.returncode:
        sys.exit(f"dryrun_attest.mjs {args[0]} failed (node {NODE}; is the branch's src/chain.ts / test/helpers/sqlite-d1.ts / schema.sql present?):\n{r.stderr[-800:]}")
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def seed(tree: Path, rows: int, tamper=None) -> Path:
    """A chain of `rows` identity rows (14 legacy + sealed) and 11 ledger rows, hashed by the branch's own entryHash.
    Cached by the branch's chain.ts digest: a changed hash recipe rebuilds every seed rather than reading as tampering."""
    SYNTH.mkdir(parents=True, exist_ok=True)
    recipe = hashlib.sha256((tree / "src" / "chain.ts").read_bytes()).hexdigest()[:12]
    db = SYNTH / f"chain-{recipe}-{rows}-{tamper or 'clean'}.db"
    if not db.exists():
        node("build", tree.as_posix(), db.as_posix(), str(rows), *([str(tamper)] if tamper else []))
    return db


def head_line(tree: Path, db: Path, identity_id: int, wrong=False) -> str:
    ih = "f" * 64 if wrong else node("row", tree.as_posix(), db.as_posix(), "identity_events", str(identity_id))
    lh = node("row", tree.as_posix(), db.as_posix(), "ledger", "11")
    return json.dumps({"at": "2026-09-12T23:55:00Z", "bucket": "2026-09-12T23:55", "status": "verified",
                       "identity": {"status": "verified", "head": ih, "verified_through_id": identity_id, "sealed_entries_total": 0, "total_rows": 0},
                       "treasury": {"status": "verified", "head": lh, "verified_through_id": 11, "sealed_entries_total": 0, "total_rows": 0}})


MUTATION = ('[ "$pages" -lt 8 ]', '[ "$pages" -lt 1 ]')  # the follow loop, disabled


def synthetic_scenarios(tree: Path):
    """The table in the docstring, with VERIFY_PAGE read from the branch. Order: cheap and decisive first."""
    page = int(node("page", tree.as_posix()))
    over = page + 431
    clean = lambda n: seed(tree, n)
    V, U = "verified", "unverified"
    yield "under", {"db": clean(page - 1), "expect": {"status": V, "identity": V, "pages": 1, "calls": 1, "verified_through_id": page - 1}}
    yield "exact", {"db": clean(page), "expect": {"status": V, "identity": V, "pages": 1, "calls": 1, "verified_through_id": page}}
    yield "over", {"db": clean(over), "expect": {"status": V, "identity": V, "pages": 2, "calls": 2, "verified_through_id": over}}
    yield "anchored", {"db": clean(over), "yesterday": head_line(tree, clean(over), ANCHOR),
                       "expect": {"status": V, "identity": V, "pages": 1, "calls": 1, "expect_matches": True, "verified_through_id": over}}
    yield "far-anchor", {"db": clean(over), "yesterday": head_line(tree, clean(over), 31),
                         "expect": {"status": V, "identity": V, "pages": 2, "calls": 2, "expect_matches": True, "verified_through_id": over}}
    yield "two-over", {"db": clean(2 * page + 1000), "expect": {"status": V, "identity": V, "pages": 3, "calls": 3, "verified_through_id": 2 * page + 1000}}
    yield "tamper-p2", {"db": seed(tree, over, page + 100), "expect": {"status": U, "identity": "broken", "pages": 2, "calls": 2}}
    yield "tamper-below", {"db": seed(tree, over, 5000), "yesterday": head_line(tree, seed(tree, over, 5000), ANCHOR),
                           "expect": {"status": V, "identity": V, "pages": 1, "expect_matches": True}}
    yield "wrong-head", {"db": clean(over), "yesterday": head_line(tree, clean(over), ANCHOR, wrong=True),
                         "expect": {"status": U, "identity": "mismatch", "expect_matches": False, "pages": 1}}
    yield "cont-fails", {"db": clean(over), "fail": f"identity_from={page}", "expect": {"status": U, "identity": "incomplete", "pages": 1, "calls": 2, "verified_through_id": page}}
    yield "cp-fails", {"db": clean(over), "fail": "/api/checkpoint", "expect": {"status": V, "identity": V, "pages": 2, "checkpoints": "fetch_failed"}}
    yield "mutation", {"db": clean(over), "mutate": MUTATION, "expect": {"status": U, "identity": "incomplete", "pages": 1, "calls": 1}}


def run_all(ref, offline, no_synthetic):
    """Every scenario against one ref: the step script and README from a worktree of it, seeds from its own source."""
    tmp_tree = Path(tempfile.mkdtemp(prefix="f916-dryrun-src-"))
    sh("git", "worktree", "add", "-q", "--detach", str(tmp_tree / "t"), ref)
    try:
        script = step_script(tmp_tree / "t")
        readme = (tmp_tree / "t" / "witness" / "README.md").read_text(encoding="utf-8")
    finally:
        sh("git", "worktree", "remove", "--force", str(tmp_tree / "t"), check=False)
        shutil.rmtree(tmp_tree, ignore_errors=True)
    base_keys = upstream_line_keys()
    results = [run_scenario(n, ref, script, offline, readme, base_keys) for n in ("anchored", "cold")]
    if not no_synthetic:
        # the seeds and VERIFY_PAGE come from the ref's own source, read out of one worktree
        seed_tree = Path(tempfile.mkdtemp(prefix="f916-dryrun-seed-"))
        sh("git", "worktree", "add", "-q", "--detach", str(seed_tree / "t"), ref)
        try:
            for n, spec in synthetic_scenarios(seed_tree / "t"):
                r = run_scenario(f"synthetic:{n}", ref, script, True, readme, base_keys, synthetic=spec)
                r["expect"] = spec["expect"]
                results.append(r)
        finally:
            sh("git", "worktree", "remove", "--force", str(seed_tree / "t"), check=False)
            shutil.rmtree(seed_tree, ignore_errors=True)
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("branch")
    ap.add_argument("--base", default="upstream/main", help="ref to compare against; a scenario failing on both is inherited, not blocking. --base '' disables")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--no-synthetic", action="store_true", help="live scenarios only (the synthetic ones need node, ~30 s)")
    a = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    # purge cached answers older than 6 hours: the chain moves, and a stale attest answer would still
    # pass shape checks while hiding a live mismatch
    for f in CACHE.glob("*.json"):
        if time.time() - f.stat().st_mtime > 6 * 3600:
            f.unlink()
    sh("git", "fetch", "-q", "origin", branch := a.branch, check=False)
    ref = branch if sh("git", "rev-parse", "-q", "--verify", branch, check=False).returncode == 0 else f"origin/{branch}"
    results = run_all(ref, a.offline, a.no_synthetic)
    # Compare against the base (#2062, 2026-09-14): a scenario that fails on the branch AND on the base is inherited,
    # reported and not blocking; only a failure the branch introduces (base passes, branch fails) blocks. Before this, a
    # scenario written for an unmerged fix failed every branch off main, including a comment-only change.
    inherited = {}
    if a.base:
        sh("git", "fetch", "-q", "upstream", "main", check=False)
        base_results = {r["scenario"]: r for r in run_all(a.base, a.offline, a.no_synthetic)}
        for r in results:
            b = base_results.get(r["scenario"])
            if not r["pass"] and b is not None and not b["pass"]:
                inherited[r["scenario"]] = b["problems"][:3]
                r["inherited"] = True
    ok = all(r["pass"] or r.get("inherited") for r in results)
    for r in results:
        ident = r["line"].get("identity", {}) if isinstance(r["line"].get("identity"), dict) else {}
        tag = "ok  " if r["pass"] else ("inh " if r.get("inherited") else "FAIL")
        print(tag + f" {r['scenario']}: exit {r['exit']}, {r['seconds']}s, {len(r['curl'])} curl, {len(r['git'])} git; "
              + (f"line status={r['line'].get('status')} identity={ident.get('status')} anchor_mode={ident.get('anchor_mode')} "
                 f"pages={ident.get('pages')} through={ident.get('verified_through_id')} expect_matches={ident.get('expect_matches')}" if r["line"] else "no line"))
        for p in r["problems"]:
            print("      - " + p)
        if r.get("inherited"):
            print(f"      - inherited: {a.base} fails this scenario the same way; not this branch's defect")
    if a.record:
        import record
        c = record.connect()
        for r in results:
            row = {"tool": "pr-dryrun", "target": f"{branch}:{r['scenario']}", "pass": bool(r["pass"] or r.get("inherited")),
                   "result": {k: r[k] for k in ("exit", "seconds", "curl", "git", "problems", "line")},
                   "expected": (f"synthetic chain: {json.dumps(r['expect'])}" if "expect" in r
                                else "the witness step runs to git commit and appends one verified, documented day line")}
            if r.get("inherited"):
                row["inherited_from"] = {"base": a.base, "problems": inherited[r["scenario"]]}
            if branch.startswith("scratch/"):
                row["negative_test"] = True  # a planted bug: failing is the expected outcome (see gates.py)
            record.add(c, "check", "agent", row)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
