# M6 Reconcile Runbook

## Command

Run the paper broker reconciliation job from the repo root:

```bash
PYTHONPATH=scripts python3 -m agent reconcile --journal-dir journal
```

Optional operator-only cash adjudication after manual review:

```bash
PYTHONPATH=scripts python3 -m agent reconcile --journal-dir journal --rebaseline-cash
```

`--rebaseline-cash` is only for an explained cash residue. It changes the cash
drift action to `rebaselined`, refreshes the cash baseline to broker truth, and
still exits 1 for that pass because drift was found.

## Exit Codes

Alert on every non-zero exit.

| Code | Meaning | Immediate action |
| --- | --- | --- |
| 0 | Completed and clean; drift latch clear. | No action. |
| 1 | Drift found this pass, or a drift latch remains set. | Inspect `journal/reconcile_alerts.jsonl`, then resolve or explicitly rebaseline cash if adjudicated. |
| 2 | Usage/mode error or run lock held. | Check for a live or hung agent process. If the `.lock` file is malformed and the owner is known dead, remove it manually and rerun. |
| 3 | Could not reconcile: broker unreadable, credentials degrade, parse failure, or journal corruption. | Retry only after checking broker credentials/connectivity and journal health. A corrupt complete journal line is fatal and writes no reconcile row. |

Exit 3 takes precedence over exit 1 when a pass both journals drift and fails to
complete. The drift remains journaled/latched and should surface as exit 1 on a
later completed pass.

## Operational Notes

- Reconcile is observation plus journal only. The pass must not submit orders,
  cancel orders, mint preflight tokens, or call the kill actuator.
- The CLI composition inherits startup recovery. A prior dangling order can
  trigger the existing `restart_unknown_state` best-effort cancel before the
  reconcile pass; that startup recovery is the only sanctioned broker mutation.
- The run gates stay off in committed config. Drift detection must work while
  opening remains disabled.
- The nightly job closes the EOD/truncated-run gap; SOD reconcile on `agent
  paper` covers the next startup path.
