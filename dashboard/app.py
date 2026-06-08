"""Local stdlib monitor (spec dashboard tier). M0: server skeleton + path sandbox.

Stdlib only, binds 127.0.0.1 exclusively. The only security-relevant surface is
the file viewer, so `safe_workspace_path` is the load-bearing guard: it
canonicalizes (resolving symlinks) and contains every requested path inside the
workspace root, rejecting traversal, absolute escapes, and symlink escapes. The
domain renderers are added in later milestones.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"  # loopback only — never change
PORT = 8788
MAX_FILE_BYTES = 5_000_000


class PathOutsideWorkspace(Exception):
    """A requested path resolved outside the workspace root."""


def safe_workspace_path(root, requested) -> Path:
    root = Path(root).resolve()
    candidate = (root / requested).resolve()  # absolute `requested` drops root; resolve follows symlinks
    if not candidate.is_relative_to(root):
        raise PathOutsideWorkspace(f"path escapes workspace: {requested!r}")
    return candidate


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"stocks-agent dashboard (M0 stub)")

    def log_message(self, *args):  # quiet in tests
        pass


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


if __name__ == "__main__":
    server = make_server()
    print(f"serving on http://{HOST}:{server.server_address[1]}")
    server.serve_forever()
