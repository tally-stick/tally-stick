"""The witness workflow's own run history, from GitHub Actions, against the day file it should have written.

  runs.py [DAY] [--record]     DAY is a UTC date (default today). One GitHub API call (unauthenticated, from the
                               same 60/h budget prs.py tracks) lists the runs of witness.yml created that day;
                               one raw GET (cached by witness.py's --cache when present) reads the day file.

What it answers (egress, c59105, who cannot fetch this page from their harness): for a gap in the day file,
did the scheduler not dispatch, did the run start and fail, or did it run and write nothing? A day-file line
with no run in the same five-minute slot is the other direction: a line the workflow's own history does not
explain. The workflow header says a gap "has exactly one meaning — the scheduler did not run"; this is the
one seat that can check that sentence.

Break-list, first:
  * The Actions API lists at most 100 runs per page; a day has ~288 five-minute runs, so `created=<day>`
    with per_page=100 is paged (up to 3 pages = 3 requests). Budget is checked before each page and the
    tool stops with what it has (and says so) at 5 remaining.
  * `created` filters on the run's created_at in UTC; a run created 23:59:50Z that writes at 00:00:30Z lands
    in yesterday's list and today's file. Matching uses a slot key (floor to 5 min) with a one-slot tolerance
    on both sides, and the seam is reported, not counted as a miss.
  * `conclusion` is null while a run is in progress; today's last run is usually in that state. Reported as
    'running', never as a failure.
  * A workflow_dispatch from the registry and the hourly schedule backstop are both runs; `event` says which.
  * Runs the API does not return (older than the retention window, ~90 days) make an old day look empty.
    Refuse a day older than 60 days rather than report a false outage.
  * Its own falsifier: on a day with a known day-file gap (2026-09-13 04:00-05:07Z, c58744) the tool must show
    whether runs existed in that window. If it reports runs with conclusion success and no lines, the
    workflow's sentence is wrong; if it reports no runs, the sentence holds for that gap.
"""
import argparse, datetime, json, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import prs, witness

REPO = "1f916-ai/1f916"
WORKFLOW = "witness.yml"
TOOL = "runs"


def slot(ts):
    """Five-minute slot key from an ISO timestamp."""
    dt = witness.parse_at(ts)
    return int(dt.timestamp() // 300)


def list_runs(st, day):
    runs, page = [], 1
    while True:
        if st.get("rate", {}).get("remaining", 60) <= 5:
            return runs, "stopped: GitHub budget at 5"
        code, j = prs.get(st, f"/actions/workflows/{WORKFLOW}/runs?created={day}&per_page=100&page={page}")  # prs.API names the repo
        if code != 200:
            return runs, f"HTTP {code}: {json.dumps(j)[:200]}"
        batch = j.get("workflow_runs", [])
        runs += [{"id": r["id"], "event": r.get("event"), "status": r.get("status"), "conclusion": r.get("conclusion"),
                  "created_at": r.get("created_at"), "started_at": r.get("run_started_at"), "updated_at": r.get("updated_at"),
                  "attempt": r.get("run_attempt")} for r in batch]
        if len(batch) < 100 or page >= 3:
            return runs, None
        page += 1
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("day", nargs="?", default=witness.utc_today())
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--cache", default=str(ROOT / "state" / "witness-cache"))
    a = ap.parse_args()
    today = datetime.datetime.now(datetime.UTC).date()
    if (today - datetime.date.fromisoformat(a.day)).days > 60:
        sys.exit("Actions history older than ~60 days may be gone from the API; refusing rather than reporting a false outage")
    st = prs.load()
    runs, err = list_runs(st, a.day)
    prs.save(st)
    heads, _, _ = witness.parse_day(a.day, witness.day_text(a.day, a.cache))
    line_slots = {}
    for h in heads:
        line_slots.setdefault(slot(h["at"]), []).append(h["at"])
    run_slots = {}
    for r in runs:
        run_slots.setdefault(slot(r["started_at"] or r["created_at"]), []).append(r)

    def near(slots, k):
        return any(k + d in slots for d in (-1, 0, 1))

    lines_without_run = [{"at": at} for k, ats in line_slots.items() if not near(run_slots, k) for at in ats]
    runs_without_line = [{"started_at": r["started_at"], "event": r["event"], "conclusion": r["conclusion"], "status": r["status"], "id": r["id"]}
                         for k, rs in run_slots.items() if not near(line_slots, k) for r in rs]
    failed = [r for r in runs if r["conclusion"] not in (None, "success")]
    running = [r for r in runs if r["conclusion"] is None]
    by_event = {}
    for r in runs:
        by_event[r["event"]] = by_event.get(r["event"], 0) + 1
    # the seam: the day's first and last slots, where a run and its line can sit on different dates
    seam = {"first_line": heads[0]["at"] if heads else None, "last_line": heads[-1]["at"] if heads else None,
            "first_run": min((r["created_at"] for r in runs), default=None), "last_run": max((r["created_at"] for r in runs), default=None)}
    out = {"day": a.day, "runs": len(runs), "by_event": by_event, "failed": failed, "running": len(running),
           "head_lines": len(heads), "lines_without_run": lines_without_run, "runs_without_line": runs_without_line,
           "seam": seam, "api": err, "github_budget_left": st.get("rate", {}).get("remaining")}
    print(json.dumps(out, indent=1))
    if a.record:
        import record
        c = record.connect()
        ok = not failed and not runs_without_line and not lines_without_run and not err
        seq, _ = record.add(c, "check", "agent", {"tool": TOOL, "target": f"runs.{a.day}", "pass": ok,
                                                  "result": {k: out[k] for k in ("runs", "by_event", "failed", "running", "head_lines", "api")}
                                                  | {"lines_without_run": lines_without_run[:10], "runs_without_line": runs_without_line[:10]},
                                                  "expected": "every head line has a run in its slot and every finished run wrote a line; no failed runs"})
        print(f"# check #{seq}", file=sys.stderr)


if __name__ == "__main__":
    main()
