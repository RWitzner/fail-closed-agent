"""Daily autorun wrapper — one unattended paper/observe session with evidence.

Track C's supervisor seam: wraps ``agent.paper_session._main`` IN-PROCESS (no
subprocess, no shell), captures the exit code, appends one status row per
attempt to ``reports/paper_sessions/autorun_log.jsonl`` (hash-verified
append-only rows), retries the SAME day at most ``--max-retries`` times and
ONLY for the feed-truncation exit-1 case (the newest daily report must say
``session.feed_truncated`` — a reconcile-drift or incomplete-crash exit 1 is
operator territory, never auto-retried), and escalates LOUDLY on any unclean
final outcome: an ``ATTENTION-<date>.txt`` file plus the nonzero exit code.

Retry evidence never overwrites: the daily report path is restart-safe
(``<date>.json``, ``<date>.1.json`` — paper_report.next_report_path) and the
recorded live stream gets a per-attempt suffix the same way.

Safety posture unchanged: this wrapper composes and runs the session runner —
it never touches config, gates, caps, or secrets. Exit codes it propagates are
the runner's documented contract (0 clean · 1 unclean · 2 lock/usage · 3
journal corruption · 4 kill HALTED · 5 calendar coverage expired).
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from agent import paper_session
from agent.journal import JournalWriter

_REPO_ROOT = Path(__file__).resolve().parents[2]

# exit codes that must NEVER auto-retry (operator-attended by contract)
_NO_RETRY = frozenset({2, 3, 4, 5})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _newest_report(report_dir: Path, session_date: str) -> Optional[dict]:
    """The authoritative (highest-suffix) daily report for the date."""
    candidates = sorted(report_dir.glob(f"{session_date}*.json"),
                        key=lambda p: p.name)
    for path in reversed(candidates):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _feed_truncated(report: Optional[dict]) -> bool:
    if not isinstance(report, dict):
        return False
    if report.get("session_incomplete"):
        return False        # a crash is operator territory, not a retry
    session = report.get("session")
    return bool(isinstance(session, dict) and session.get("feed_truncated"))


def _record_path(record_dir: Path, session_date: str, attempt: int) -> Path:
    name = (f"{session_date}.events.jsonl" if attempt == 0
            else f"{session_date}.events.{attempt}.jsonl")
    return record_dir / name


def run_autorun(*, session_argv: Sequence[str], session_date: str,
                report_dir, log_dir=None, record_dir=None,
                max_retries: int = 1,
                run_session=None, utc_now_iso_fn=None) -> int:
    """Run one session day with bounded truncation-retry and loud escalation.

    ``session_argv`` is the paper_session arg list WITHOUT --record-events
    (added per attempt when ``record_dir`` is given). ``run_session`` is the
    injectable runner (default: ``paper_session._main``)."""
    report_dir = Path(report_dir)
    log_dir = Path(log_dir) if log_dir is not None else report_dir
    runner = run_session or paper_session._main
    now_iso = utc_now_iso_fn or _utc_now_iso
    log_dir.mkdir(parents=True, exist_ok=True)
    log = JournalWriter(log_dir / "autorun_log.jsonl",
                        run_id=f"autorun-{session_date}", clock=now_iso)

    attempt = 0
    exit_code: int = 1
    while True:
        argv = list(session_argv)
        if record_dir is not None:
            record_dir = Path(record_dir)
            record_dir.mkdir(parents=True, exist_ok=True)
            argv += ["--record-events",
                     str(_record_path(record_dir, session_date, attempt))]
        exit_code = int(runner(argv))
        truncated = (exit_code == 1
                     and _feed_truncated(_newest_report(report_dir,
                                                        session_date)))
        will_retry = (truncated and attempt < max_retries
                      and exit_code not in _NO_RETRY)
        log.append("autorun_attempt", {
            "session_date_et": session_date,
            "attempt": attempt,
            "exit_code": exit_code,
            "feed_truncated": truncated,
            "will_retry": will_retry,
        })
        if not will_retry:
            break
        attempt += 1

    if exit_code != 0:
        attention = log_dir / f"ATTENTION-{session_date}.txt"
        attention.write_text(
            f"paper autorun UNCLEAN for {session_date}: exit={exit_code} "
            f"after {attempt + 1} attempt(s) at {now_iso()}.\n"
            f"See {report_dir} daily report(s) + autorun_log.jsonl; the exit "
            "contract is 1 unclean / 2 lock / 3 journal corruption / 4 kill "
            "HALTED / 5 calendar expired. Do not re-arm or bulk-retry without "
            "reading the journal evidence first.\n",
            encoding="utf-8")
    return exit_code


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Unattended daily wrapper around agent.paper_session: "
                    "bounded truncation-retry, append-only attempt log, loud "
                    "ATTENTION escalation. Flips nothing.")
    parser.add_argument("--journal-dir", required=True)
    parser.add_argument("--report-dir", default=str(_REPO_ROOT / "reports"
                                                    / "paper_sessions"))
    parser.add_argument("--record-dir", default=str(_REPO_ROOT / "data"
                                                    / "live"))
    parser.add_argument("--session-date", default=None,
                        help="ET session date (default: today per the "
                             "runner's calendar)")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--strategy-id", default=None)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--replay", default=None,
                        help="rehearsal mode: replay this events.jsonl "
                             "instead of --live (no recording)")
    args = parser.parse_args(argv)

    session_date = args.session_date or datetime.now(
        timezone.utc).strftime("%Y-%m-%d")
    session_argv = ["--journal-dir", args.journal_dir,
                    "--report-dir", args.report_dir]
    if args.session_date:
        session_argv += ["--session-date", args.session_date]
    if args.symbols:
        session_argv += ["--symbols", args.symbols]
    if args.strategy_id:
        session_argv += ["--strategy-id", args.strategy_id]
    if args.replay:
        session_argv += ["--replay", args.replay]
        record_dir = None
    else:
        session_argv += ["--live"]
        record_dir = args.record_dir

    return run_autorun(
        session_argv=session_argv, session_date=session_date,
        report_dir=args.report_dir, record_dir=record_dir,
        max_retries=args.max_retries)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
