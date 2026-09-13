"""Everything that must hold before a branch of the society's repo leaves this machine.

Chosen by what changed, each gate a way a merge could flatten a service:

  workflow changed (.github/workflows/*.yml)
    yaml        the file parses, else the job never runs
    bash -n     every run: step parses, else the step fails on every run (PR 232's apostrophe)
    shellcheck  quoting, unbound variables under set -u, non-portable constructs
    actionlint  cron syntax, runner labels, permissions, ${{ }} expressions
    dryrun      the witness step executed for real against cached live answers (scripts/dryrun.py)
  witness/bin/witness.mjs or its .sha256 changed
    sha256      the pair agrees, else the step's own checksum fails
  worker changed (src/, wrangler.jsonc, package.json, tsconfig.json)
    tsc         typecheck, as their CI does
    wrangler    `deploy --dry-run` builds the bundle without deploying: catches what tests and tsc miss
                (a Node-only API in a Worker, a bad import)

  gates.py lint BRANCH      the static gates only
  gates.py all BRANCH       every gate that applies (pr.py test and pr.py push call this)

Each run logs a `check` row per gate (tool: pr-<gate>) so record.py checks scores them.
"""
import hashlib, os, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FORK = ROOT.parent / "1f916-fork"
TOOLS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
sys.path.insert(0, str(HERE))
NODE = shutil.which("node") or "node"
BASH = shutil.which("bash") or "C:/Program Files/Git/usr/bin/bash.exe"


def tool(name):
    return shutil.which(name) or shutil.which(name, path=str(TOOLS))


def sh(*args, cwd=FORK, timeout=600):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def changed_files(branch):
    r = sh("git", "checkout", "-q", branch)
    if r.returncode:
        sys.exit(r.stderr)
    return sorted(set(sh("git", "diff", "--name-only", "upstream/main...HEAD").stdout.split()
                      + sh("git", "diff", "--cached", "--name-only").stdout.split()))


def record_check(tool_name, target, ok, result, expected):
    import record
    c = record.connect()
    seq, _ = record.add(c, "check", "agent", {"tool": tool_name, "target": target, "pass": ok, "result": result, "expected": expected})
    return seq


def report(results):
    for r in results:
        print(("ok  " if r["ok"] else "FAIL") + f" [{r['gate']}] {r['where']} {r.get('error') or ''}")


def lint(branch):
    import yaml
    files = changed_files(branch)
    results = []
    for f in [f for f in files if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml"))]:
        try:
            doc = yaml.safe_load((FORK / f).read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            results.append({"gate": "yaml", "where": f, "ok": False, "error": str(e)[:300]})
            continue
        results.append({"gate": "yaml", "where": f, "ok": True})
        for job, jd in (doc.get("jobs") or {}).items():
            for i, step in enumerate(jd.get("steps") or []):
                if "run" not in step:
                    continue
                where = f"{f} {job}/{step.get('name') or i}"
                sf = FORK / ".tally-stick.step.sh"
                sf.write_text("#!/usr/bin/env bash\n" + step["run"], encoding="utf-8", newline="\n")
                # Git's bash by PATH order: a bare "bash" from Python lands on System32's WSL stub, which
                # exits 1 saying nothing. Relative script name, since that bash misreads a C: path.
                r = sh(BASH, "-n", sf.name)
                results.append({"gate": "bash -n", "where": where, "ok": r.returncode == 0, "error": r.stderr.strip()[:300] or None})
                if tool("shellcheck"):
                    r = sh(tool("shellcheck"), "-s", "bash", "-S", "warning", sf.name)
                    results.append({"gate": "shellcheck", "where": where, "ok": r.returncode == 0, "error": (r.stdout.strip() or r.stderr.strip())[:600] or None})
                else:
                    results.append({"gate": "shellcheck", "where": where, "ok": False, "error": "not installed: winget install koalaman.shellcheck"})
                sf.unlink(missing_ok=True)
        if tool("actionlint"):
            r = sh(tool("actionlint"), "-no-color", f)
            results.append({"gate": "actionlint", "where": f, "ok": r.returncode == 0, "error": (r.stdout.strip() or r.stderr.strip())[:600] or None})
        else:
            results.append({"gate": "actionlint", "where": f, "ok": False, "error": "not installed: winget install rhysd.actionlint"})
    if "witness/bin/witness.mjs" in files or "witness/bin/witness.mjs.sha256" in files:
        want = (FORK / "witness/bin/witness.mjs.sha256").read_text(encoding="utf-8").split()[0]
        have = hashlib.sha256((FORK / "witness/bin/witness.mjs").read_bytes()).hexdigest()
        results.append({"gate": "sha256", "where": "witness/bin/witness.mjs", "ok": want == have,
                        "error": None if want == have else f".sha256 says {want[:12]}, file hashes to {have[:12]}: the step's sha256sum -c would fail"})
    report(results)
    if not results:
        print("no workflow or witness files changed; nothing to lint")
    ok = all(r["ok"] for r in results)
    record_check("pr-lint", branch, ok, results, "changed workflows: yaml, bash -n, shellcheck, actionlint pass; witness.mjs matches its .sha256")
    return ok


def build(branch):
    """Worker code: tsc, then a wrangler build without deploying."""
    results = []
    r = sh(NODE, str(FORK / "node_modules/typescript/bin/tsc"), "--noEmit")
    results.append({"gate": "tsc", "where": "src/", "ok": r.returncode == 0, "error": (r.stdout.strip().splitlines() or [None])[0]})
    r = sh(NODE, str(FORK / "node_modules/wrangler/bin/wrangler.js"), "deploy", "--dry-run", "--outdir", ".tally-stick-build")
    shutil.rmtree(FORK / ".tally-stick-build", ignore_errors=True)
    results.append({"gate": "wrangler --dry-run", "where": "worker", "ok": r.returncode == 0,
                    "error": None if r.returncode == 0 else ((r.stderr or r.stdout).strip().splitlines() or [""])[-1][:300]})
    report(results)
    ok = all(r["ok"] for r in results)
    record_check("pr-build", branch, ok, results, "tsc --noEmit clean; wrangler deploy --dry-run builds the worker")
    return ok


def table_shape(c):
    return {t: sorted(r[1] for r in c.execute(f"PRAGMA table_info('{t}')"))
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def migrations(branch):
    """A migration is the one irreversible change: it runs against the live database on deploy. Their
    migrations cannot be replayed from empty (0001 assumes tables older than the migration system), so:
    start from upstream main's schema.sql (their mirror of live), apply only the migrations this branch
    adds, in order, on a database with one row in every table that has defaults enough to insert one;
    then the shape must equal the branch's own schema.sql (their fresh-install test checks tables, this
    checks columns too). Numbering must continue upstream's sequence."""
    import sqlite3
    files = changed_files(branch)
    new = sorted(f for f in files if f.startswith("migrations/") and f.endswith(".sql")
                 and sh("git", "cat-file", "-e", f"upstream/main:{f}").returncode != 0)
    results = []
    if not new:
        return True
    base_sql = sh("git", "show", "upstream/main:schema.sql").stdout
    have = sorted(sh("git", "ls-tree", "--name-only", "upstream/main", "migrations/").stdout.split())
    last = int(have[-1].split("/")[1][:4]) if have else 0
    for i, f in enumerate(new, start=1):
        want = f"{last + i:04d}_"
        results.append({"gate": "migration numbering", "where": f, "ok": f.split("/")[1].startswith(want),
                        "error": None if f.split("/")[1].startswith(want) else f"expected prefix {want} after upstream's {have[-1] if have else 'none'}"})
    c = sqlite3.connect(":memory:")
    c.executescript(base_sql)
    seeded = 0
    for t in list(table_shape(c)):
        try:
            c.execute(f"INSERT INTO '{t}' DEFAULT VALUES")
            seeded += 1
        except sqlite3.Error:
            pass
    for f in new:
        sql = (FORK / f).read_text(encoding="utf-8")
        try:
            c.executescript(sql)
            results.append({"gate": "migration applies", "where": f, "ok": True, "error": None})
        except sqlite3.Error as e:
            results.append({"gate": "migration applies", "where": f, "ok": False, "error": f"{e} (on upstream schema.sql with {seeded} seeded tables)"})
            break
    branch_c = sqlite3.connect(":memory:")
    try:
        branch_c.executescript((FORK / "schema.sql").read_text(encoding="utf-8"))
        a, b = table_shape(c), table_shape(branch_c)
        diffs = sorted(set(a) ^ set(b)) + [t for t in a if t in b and a[t] != b[t]]
        results.append({"gate": "schema.sql mirrors migrations", "where": "schema.sql", "ok": not diffs,
                        "error": None if not diffs else f"tables/columns that differ between migrated base and schema.sql: {diffs[:8]}"})
    except sqlite3.Error as e:
        results.append({"gate": "schema.sql parses", "where": "schema.sql", "ok": False, "error": str(e)[:300]})
    report(results)
    ok = all(r["ok"] for r in results)
    record_check("pr-migrations", branch, ok, results, "new migrations numbered in sequence, apply on upstream schema.sql with seeded rows, and leave the shape schema.sql declares")
    return ok


def dryrun(branch):
    r = subprocess.run([sys.executable, str(HERE / "dryrun.py"), branch, "--record"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    print(r.stdout.strip() or r.stderr.strip()[-600:])
    return r.returncode == 0


def all_gates(branch):
    files = changed_files(branch)
    ok = lint(branch)
    if any(f == ".github/workflows/witness.yml" for f in files):
        ok = dryrun(branch) and ok
    if any(f.startswith("src/") or f in ("wrangler.jsonc", "package.json", "tsconfig.json", "schema.sql") or f.startswith("migrations/") for f in files):
        ok = build(branch) and ok
    if any(f.startswith("migrations/") or f == "schema.sql" for f in files):
        ok = migrations(branch) and ok
    return ok


def summary_table(branch):
    """The latest check row per gate for BRANCH, as a Markdown table for the PR body."""
    import json, record
    c = record.connect()
    rows = c.execute("""SELECT tool, pass, result, ts FROM checks WHERE tool LIKE 'pr-%' AND (target = ? OR target LIKE ?)
                        ORDER BY seq DESC""", (branch, branch + ":%")).fetchall()
    latest = {}
    for tool_name, ok, result, ts in rows:
        latest.setdefault(tool_name, (ok, result, ts))
    tests = c.execute("""SELECT json_extract(payload,'$.summary'), ts FROM events WHERE kind='act'
                         AND json_extract(payload,'$.what')='pr:test' AND json_extract(payload,'$.branch')=? ORDER BY seq DESC LIMIT 1""", (branch,)).fetchone()
    lines = ["| gate | result | when (UTC) |", "|---|---|---|"]
    names = {"pr-lint": "yaml · bash -n · shellcheck · actionlint · sha256 pair", "pr-dryrun": "witness step executed (anchored + cold)",
             "pr-build": "tsc · wrangler deploy --dry-run", "pr-migrations": "migrations: numbering · apply · schema.sql mirror"}
    for tool_name in ("pr-lint", "pr-dryrun", "pr-build", "pr-migrations"):
        if tool_name in latest:
            ok, result, ts = latest[tool_name]
            lines.append(f"| {names[tool_name]} | {'pass' if ok else 'FAIL'} | {ts[:16]} |")
    if tests and tests[0]:
        s = json.loads(tests[0])
        lines.append(f"| npm test | {s.get('pass', '?')}/{s.get('tests', '?')} pass, {s.get('fail', '?')} fail | {tests[1][:16]} |")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("lint", "all"):
        sys.exit(__doc__)
    sys.exit(0 if (lint if sys.argv[1] == "lint" else all_gates)(sys.argv[2]) else 1)
