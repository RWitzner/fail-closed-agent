# .secrets/ — credentials + runtime arming (git-ignored except this README)

Never commit anything here except this file. Tests use spy/no-op brokers and
make **no** network calls or credential reads.

Expected files (see `docs/runbooks/paper-go-live-checklist.md` for the ordered steps):

| File | Used by | Shape |
|------|---------|-------|
| `databento.json` | M1 historical verifier, `agent.m7_run_driver`, `databento_live_source` (tier-2b) | `{"api_key": "..."}` |
| `alpaca_paper.json` | `AlpacaPaperBroker`, `agent.verify_alpaca_paper` | `{"key_id": "...", "secret_key": "...", "base_url": "https://paper-api.alpaca.markets"}` — exactly these keys; `base_url` MUST be the paper host (enforced pre-SDK) |
| `run_gates.json` | the PAPER run gates (runtime key) | `{"enabled": true, "paper_trading": {"enabled": true}}` — identity-`true` only; absent/malformed ⇒ both gates read `false` (fail-closed). **Delete this file to disarm.** |

The committed `config/agent_rules.json` gates stay `false` forever; `run_gates.json`
is the runtime override that arms a paper run. Opening orders additionally require a
reviewed passing backtest artifact (S9) — arming alone opens nothing.

Key B of two-key arming (LIVE money, M8) is a separate runtime credential supplied
here or via operator env, never committed; key A is the committed config flag. The
live broker requires both, independently. Nothing in this directory can arm live.
