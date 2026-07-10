"""Local stdlib live view — follow a paper/observe session while it runs.

Stdlib only, binds 127.0.0.1 exclusively, strictly READ-ONLY over the journal
tree (hash-verified incremental reads; see dashboard/state.py). The browser
polls ``/api/state`` and renders decisions, orders, fills, positions, the
Broker-vs-Modeled PnL split (never conflated — S5), kill state, status-plane
transitions, reconcile counts, and the daily reports. The dashboard can never
affect a session: no broker, no gates, no writes.

``safe_workspace_path`` (the M0 path sandbox) remains the guard for any future
file-viewer surface and stays load-bearing-tested; the live view itself never
serves files by request path.
"""
import json
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


_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>stocks-agent — live paper view</title>
<style>
:root { --bg:#101418; --card:#1a2027; --text:#dbe3ea; --dim:#8a97a3;
        --green:#4cc38a; --red:#e5534b; --amber:#d9a13c; --line:#2a323b; }
@media (prefers-color-scheme: light) {
  :root { --bg:#f4f6f8; --card:#ffffff; --text:#1c2733; --dim:#5c6b7a;
          --line:#dde3e9; }
}
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--text);
       font:14px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
       padding:16px; }
h1 { font-size:16px; font-weight:600; }
h2 { font-size:12px; font-weight:600; color:var(--dim);
     text-transform:uppercase; letter-spacing:.06em; margin-bottom:8px; }
header { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
         margin-bottom:14px; }
.badge { padding:2px 10px; border-radius:999px; font-size:12px;
         font-weight:600; background:var(--line); }
.badge.ok { background:rgba(76,195,138,.15); color:var(--green); }
.badge.warn { background:rgba(217,161,60,.18); color:var(--amber); }
.badge.bad { background:rgba(229,83,75,.18); color:var(--red); }
.meta { color:var(--dim); font-size:12px; }
.grid { display:grid; gap:12px;
        grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }
.cards { display:grid; gap:12px; margin-bottom:12px;
         grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
.card, .panel { background:var(--card); border:1px solid var(--line);
                border-radius:10px; padding:12px; }
.card .v { font-size:20px; font-weight:650; margin-top:2px; }
.card .k { color:var(--dim); font-size:12px; }
.pos .v { color:var(--green); } .neg .v { color:var(--red); }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th { text-align:left; color:var(--dim); font-weight:600; padding:4px 8px;
     border-bottom:1px solid var(--line); white-space:nowrap; }
td { padding:4px 8px; border-bottom:1px solid var(--line);
     white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
     max-width:260px; }
.panel { overflow-x:auto; }
.empty { color:var(--dim); font-style:italic; padding:6px 8px; }
footer { color:var(--dim); font-size:11px; margin-top:14px; }
</style></head><body>
<header>
  <h1>stocks-agent — live paper view</h1>
  <span id="kill" class="badge">…</span>
  <span id="incomplete" class="badge" style="display:none">incomplete day</span>
  <span class="meta" id="meta">loading…</span>
</header>
<div class="cards">
  <div class="card" id="c-broker"><div class="k">Broker PnL (ledger truth)</div><div class="v">—</div></div>
  <div class="card" id="c-modeled"><div class="k">Modeled PnL (label)</div><div class="v">—</div></div>
  <div class="card" id="c-fees"><div class="k">Fees</div><div class="v">—</div></div>
  <div class="card" id="c-pos"><div class="k">Open positions</div><div class="v">—</div></div>
  <div class="card" id="c-trades"><div class="k">Opens / closes</div><div class="v">—</div></div>
  <div class="card" id="c-fills"><div class="k">Fills (broker/modeled)</div><div class="v">—</div></div>
</div>
<div class="grid">
  <div class="panel"><h2>Live positions</h2><div id="positions"></div></div>
  <div class="panel"><h2>Decisions (latest)</h2><div id="decisions"></div></div>
  <div class="panel"><h2>Orders (latest)</h2><div id="orders"></div></div>
  <div class="panel"><h2>Fills (latest)</h2><div id="fills"></div></div>
  <div class="panel"><h2>Status plane (halt / LULD / SSR)</h2><div id="status"></div></div>
  <div class="panel"><h2>Kill transitions</h2><div id="killrows"></div></div>
  <div class="panel"><h2>Reject reasons</h2><div id="rejects"></div></div>
  <div class="panel"><h2>Daily reports</h2><div id="reports"></div></div>
</div>
<footer id="foot"></footer>
<script>
const $ = id => document.getElementById(id);
function table(rows, cols) {
  if (!rows || !rows.length) return '<div class="empty">nothing yet</div>';
  const head = '<tr>' + cols.map(c => `<th>${c}</th>`).join('') + '</tr>';
  const body = rows.slice().reverse().map(r =>
    '<tr>' + cols.map(c => `<td>${r[c] ?? ''}</td>`).join('') + '</tr>'
  ).join('');
  return `<table>${head}${body}</table>`;
}
function money(el, v) {
  const n = parseFloat(v);
  el.classList.remove('pos','neg');
  if (!isNaN(n) && n > 0) el.classList.add('pos');
  if (!isNaN(n) && n < 0) el.classList.add('neg');
  el.querySelector('.v').textContent = isNaN(n) ? (v ?? '—') : n.toFixed(2);
}
async function tick() {
  let s;
  try { s = await (await fetch('/api/state')).json(); }
  catch (e) { $('meta').textContent = 'poll failed: ' + e; return; }
  const kill = $('kill');
  kill.textContent = 'kill: ' + s.kill.state;
  kill.className = 'badge ' + (s.kill.state === 'monitoring' ? 'ok' :
                               s.kill.state === 'halted' ? 'bad' : 'warn');
  $('meta').textContent = `journal=${s.journal_dir}  runs=${s.run_ids.length}` +
    (s.run_ids.length ? ` (latest ${s.run_ids[s.run_ids.length-1]})` : '');
  money($('c-broker'), s.pnl.realized_broker_pnl_usd);
  money($('c-modeled'), s.pnl.realized_modeled_pnl_usd);
  $('c-fees').querySelector('.v').textContent = s.pnl.fees_usd;
  $('c-pos').querySelector('.v').textContent = s.positions.open_count;
  $('c-trades').querySelector('.v').textContent =
    `${s.positions.opens} / ${s.positions.closes}`;
  $('c-fills').querySelector('.v').textContent =
    `${s.fills.counts.broker_fill} / ${s.fills.counts.modeled_execution_fill}`;
  $('positions').innerHTML = table(s.positions.live,
    ['symbol','qty','opened_ts_utc','broker_cost_usd']);
  $('decisions').innerHTML = table(s.decisions.recent,
    ['decision_ts_utc','symbol','action','edge_label']);
  $('orders').innerHTML = table(s.orders.recent,
    ['ts_utc','event_type','symbol','side','qty','status','reasons']);
  $('fills').innerHTML = table(s.fills.recent,
    ['ts_utc','event_type','symbol','qty','price','divergence_bps']);
  $('status').innerHTML = table(s.status_plane.recent,
    ['ts_utc','symbol','field','from','to','source']);
  $('killrows').innerHTML = table(s.kill.transitions,
    ['ts_utc','from','to','cause']);
  const rj = Object.entries(s.orders.reject_reasons || {})
    .map(([reason, count]) => ({reason, count}));
  $('rejects').innerHTML = table(rj, ['reason','count']);
  $('reports').innerHTML = table((s.reports || []).map(r => ({
    file: r.file, mode: r.mode,
    incomplete: r.session_incomplete ? 'YES' : '',
    broker_pnl: r.trading ? r.trading.realized_broker_pnl_usd : '',
    kill: r.kill_state})),
    ['file','mode','incomplete','broker_pnl','kill']);
  const anyIncomplete = (s.reports || []).some(r => r.session_incomplete);
  $('incomplete').style.display = anyIncomplete ? '' : 'none';
  $('incomplete').className = 'badge warn';
  const corr = Object.keys(s.corruption || {});
  $('foot').textContent = `generated ${s.generated_utc}  ·  decisions ${s.decisions.total}` +
    `  ·  reconcile runs ${s.reconcile.runs} (drift rows ${s.reconcile.drift_rows})` +
    (corr.length ? `  ·  CORRUPTION: ${corr.join(', ')}` : '');
}
tick(); setInterval(tick, 2000);
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/state"):
            source = getattr(self.server, "state_source", None)
            if source is None:
                payload = {"error": "no journal dir configured"}
            else:
                try:
                    payload = source.snapshot(
                        limit=getattr(self.server, "state_limit", 50))
                except Exception as exc:  # noqa: BLE001 — view must not die
                    payload = {"error": f"snapshot failed: {exc!r}"}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):  # quiet in tests
        pass


def make_server(host: str = HOST, port: int = PORT, *,
                journal_dir=None, report_dir=None,
                limit: int = 50) -> ThreadingHTTPServer:
    if host != HOST:
        raise ValueError(f"dashboard binds {HOST} only; refused host {host!r}")
    server = ThreadingHTTPServer((host, port), _Handler)
    server.state_source = None
    server.state_limit = int(limit)
    if journal_dir is not None:
        from dashboard.state import JournalStateSource

        server.state_source = JournalStateSource(journal_dir,
                                                 report_dir=report_dir)
    return server


if __name__ == "__main__":
    server = make_server()
    print(f"serving on http://{HOST}:{server.server_address[1]}")
    server.serve_forever()
