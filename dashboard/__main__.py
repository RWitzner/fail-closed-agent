"""CLI: the local live view over a (running or finished) session's journals.

    PYTHONPATH=scripts python3 -m dashboard --journal-dir journal

Read-only; loopback-only; flips nothing. Open http://127.0.0.1:<port>/ and
watch decisions/orders/fills/PnL update while a session runs (the page polls
every 2 seconds)."""
import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from dashboard.app import HOST, make_server  # noqa: E402


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Local read-only live view over the session journals.")
    parser.add_argument("--journal-dir", default=str(_REPO_ROOT / "journal"))
    parser.add_argument("--report-dir",
                        default=str(_REPO_ROOT / "reports" / "paper_sessions"))
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--limit", type=int, default=50,
                        help="rows per table in the view")
    args = parser.parse_args(argv)

    server = make_server(HOST, args.port, journal_dir=args.journal_dir,
                         report_dir=args.report_dir, limit=args.limit)
    print(f"live view: http://{HOST}:{server.server_address[1]}/  "
          f"(journal={args.journal_dir}, read-only; Ctrl-C stops)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
