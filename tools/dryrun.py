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

Two scenarios, both run: `anchored` (day files present, the normal case) and `cold` (no day files:
the first line after a gap, which is the expensive unanchored read). For each the produced day line
must parse as JSON and carry the fields the day-file format promises; every key it carries must be
named in witness/README.md.

  dryrun.py BRANCH [--offline] [--record]

Exit 0 when both scenarios pass. --record logs one `check` row per scenario (tool: pr-dryrun).
"""
import argparse, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FORK = ROOT.parent / "1f916-fork"
CACHE = ROOT / "state" / "dryrun-cache"
sys.path.insert(0, str(HERE))

REQUIRED = {"at", "bucket", "status", "identity", "treasury"}
REQUIRED_LOG = {"status", "head", "verified_through_id", "sealed_entries_total", "total_rows"}
BASH = shutil.which("bash") or "C:/Program Files/Git/usr/bin/bash.exe"
REAL_CURL = shutil.which("curl") or "curl"


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


def write_shims(shimdir: Path, offline: bool):
    shimdir.mkdir(parents=True, exist_ok=True)
    cache = CACHE.as_posix()
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
    for name, body in (("curl", curl), ("git", git)):
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


def run_scenario(name, branch, script, offline, readme_text, base_keys):
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=f"f916-dryrun-{name}-"))
    tree = tmp / "tree"
    sh("git", "worktree", "add", "-q", "--detach", str(tree), branch)
    try:
        today = datetime.now(timezone.utc)
        day = today.strftime("%Y-%m-%d")
        yday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        bucket = f"{today:%Y-%m-%dT%H}:{today.minute // 5 * 5:02d}"
        wdir = tree / "witness"
        if name == "cold":
            for f in wdir.glob("*.jsonl"):
                f.unlink()
        else:
            f = wdir / f"{day}.jsonl"
            if f.exists():
                kept = [l for l in f.read_text(encoding="utf-8").splitlines() if f'"bucket":"{bucket}"' not in l]
                f.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8", newline="\n")
        before = (wdir / f"{day}.jsonl").read_text(encoding="utf-8").splitlines() if (wdir / f"{day}.jsonl").exists() else []
        shim = tmp / "shim"
        write_shims(shim, offline)
        (tree / ".dryrun-step.sh").write_text(script, encoding="utf-8", newline="\n")
        log = tmp / "log"
        env = {k: v for k, v in os.environ.items() if k != "WITNESS_KEY"}
        env.update({"PATH": shim.as_posix() + os.pathsep + env.get("PATH", ""), "DRYRUN_LOG": log.as_posix(),
                    "DRYRUN_OFFLINE": "1" if offline else "0", "RUNNER_TEMP": tmp.as_posix()})
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
            problems.append(f"expected exactly one new day line, got {len(new_lines)}")
        line = {}
        if new_lines:
            try:
                line = json.loads(new_lines[-1])
            except json.JSONDecodeError as e:
                problems.append(f"new line is not JSON: {e}")
        if line:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("branch")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    # purge cached answers older than 6 hours: the chain moves, and a stale attest answer would still
    # pass shape checks while hiding a live mismatch
    for f in CACHE.glob("*.json"):
        if time.time() - f.stat().st_mtime > 6 * 3600:
            f.unlink()
    sh("git", "fetch", "-q", "origin", branch := a.branch, check=False)
    ref = branch if sh("git", "rev-parse", "-q", "--verify", branch, check=False).returncode == 0 else f"origin/{branch}"
    tmp_tree = Path(tempfile.mkdtemp(prefix="f916-dryrun-src-"))
    sh("git", "worktree", "add", "-q", "--detach", str(tmp_tree / "t"), ref)
    try:
        script = step_script(tmp_tree / "t")
        readme = (tmp_tree / "t" / "witness" / "README.md").read_text(encoding="utf-8")
    finally:
        sh("git", "worktree", "remove", "--force", str(tmp_tree / "t"), check=False)
        shutil.rmtree(tmp_tree, ignore_errors=True)
    base_keys = upstream_line_keys()
    results = [run_scenario(n, ref, script, a.offline, readme, base_keys) for n in ("anchored", "cold")]
    ok = all(r["pass"] for r in results)
    for r in results:
        print(("ok  " if r["pass"] else "FAIL") + f" {r['scenario']}: exit {r['exit']}, {r['seconds']}s, {len(r['curl'])} curl, {len(r['git'])} git; "
              + (f"line status={r['line'].get('status')} identity={r['line'].get('identity', {}).get('status')} "
                 f"anchor_mode={r['line'].get('identity', {}).get('anchor_mode')} pages={r['line'].get('identity', {}).get('pages')}" if r["line"] else "no line"))
        for p in r["problems"]:
            print("      - " + p)
    if a.record:
        import record
        c = record.connect()
        for r in results:
            record.add(c, "check", "agent", {"tool": "pr-dryrun", "target": f"{branch}:{r['scenario']}", "pass": r["pass"],
                                             "result": {k: r[k] for k in ("exit", "seconds", "curl", "git", "problems", "line")},
                                             "expected": "the witness step runs to git commit and appends one verified, documented day line"})
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
