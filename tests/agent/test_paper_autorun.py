"""agent.paper_autorun — bounded truncation-retry + append-only evidence.

Pins: clean day = one attempt, one log row, exit 0, no ATTENTION; exit-1 with
a truncated report retries ONCE with a suffixed record path; drift/crash
exit-1 never retries; operator-attended codes (2/3/4/5) never retry; every
unclean final outcome writes ATTENTION-<date>.txt and propagates the code."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.journal import replay as journal_replay
from agent.paper_autorun import run_autorun


def _write_report(report_dir: Path, date: str, *, truncated=False,
                  incomplete=False, suffix=None):
    report_dir.mkdir(parents=True, exist_ok=True)
    name = f"{date}.json" if suffix is None else f"{date}.{suffix}.json"
    (report_dir / name).write_text(json.dumps({
        "session_date_et": date,
        "session": {"feed_truncated": truncated},
        "session_incomplete": incomplete,
    }), encoding="utf-8")


class TestRunAutorun(unittest.TestCase):
    _DATE = "2026-07-06"

    def _run(self, tmp, exit_codes, *, reports_between=None,
             max_retries=1, record=True):
        """Drive run_autorun with scripted session exit codes."""
        report_dir = Path(tmp) / "reports"
        record_dir = Path(tmp) / "recorded" if record else None
        calls = []
        codes = iter(exit_codes)

        def runner(argv):
            attempt = len(calls)
            calls.append(list(argv))
            if reports_between:
                reports_between(report_dir, attempt)
            return next(codes)

        rc = run_autorun(
            session_argv=["--journal-dir", "j", "--live"],
            session_date=self._DATE, report_dir=report_dir,
            record_dir=record_dir, max_retries=max_retries,
            run_session=runner,
            utc_now_iso_fn=lambda: "2026-07-06T21:00:00.000000Z")
        return rc, calls, report_dir

    def _log_rows(self, report_dir: Path):
        return journal_replay(report_dir / "autorun_log.jsonl")

    def test_clean_day_single_attempt_no_attention(self):
        with TemporaryDirectory() as tmp:
            rc, calls, report_dir = self._run(
                tmp, [0],
                reports_between=lambda d, a: _write_report(d, self._DATE))
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            rows = self._log_rows(report_dir)
            self.assertEqual([r["exit_code"] for r in rows], [0])
            self.assertFalse(
                (report_dir / f"ATTENTION-{self._DATE}.txt").exists())

    def test_truncated_exit_1_retries_once_with_suffixed_recording(self):
        with TemporaryDirectory() as tmp:
            def reports(d, attempt):
                _write_report(d, self._DATE, truncated=(attempt == 0),
                              suffix=None if attempt == 0 else attempt)

            rc, calls, report_dir = self._run(
                tmp, [1, 0], reports_between=reports)
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 2)
            self.assertIn(f"{self._DATE}.events.jsonl",
                          calls[0][-1])
            self.assertIn(f"{self._DATE}.events.1.jsonl",
                          calls[1][-1])
            rows = self._log_rows(report_dir)
            self.assertEqual([r["will_retry"] for r in rows], [True, False])
            self.assertFalse(
                (report_dir / f"ATTENTION-{self._DATE}.txt").exists())

    def test_truncated_retry_is_bounded(self):
        with TemporaryDirectory() as tmp:
            rc, calls, report_dir = self._run(
                tmp, [1, 1],
                reports_between=lambda d, a: _write_report(
                    d, self._DATE, truncated=True,
                    suffix=None if a == 0 else a))
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 2)   # 1 retry, then escalate
            self.assertTrue(
                (report_dir / f"ATTENTION-{self._DATE}.txt").exists())

    def test_drift_exit_1_never_retries(self):
        with TemporaryDirectory() as tmp:
            rc, calls, report_dir = self._run(
                tmp, [1],
                reports_between=lambda d, a: _write_report(
                    d, self._DATE, truncated=False))
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 1)
            self.assertTrue(
                (report_dir / f"ATTENTION-{self._DATE}.txt").exists())

    def test_incomplete_crash_exit_1_never_retries(self):
        with TemporaryDirectory() as tmp:
            rc, calls, _ = self._run(
                tmp, [1],
                reports_between=lambda d, a: _write_report(
                    d, self._DATE, truncated=True, incomplete=True))
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 1)

    def test_operator_codes_never_retry(self):
        for code in (2, 3, 4, 5):
            with self.subTest(code=code), TemporaryDirectory() as tmp:
                rc, calls, report_dir = self._run(
                    tmp, [code],
                    reports_between=lambda d, a: _write_report(
                        d, self._DATE, truncated=True))
                self.assertEqual(rc, code)
                self.assertEqual(len(calls), 1)
                self.assertTrue(
                    (report_dir / f"ATTENTION-{self._DATE}.txt").exists())

    def test_no_record_dir_means_no_record_flag(self):
        with TemporaryDirectory() as tmp:
            rc, calls, _ = self._run(
                tmp, [0], record=False,
                reports_between=lambda d, a: _write_report(d, self._DATE))
            self.assertEqual(rc, 0)
            self.assertNotIn("--record-events", calls[0])


if __name__ == "__main__":
    unittest.main()
