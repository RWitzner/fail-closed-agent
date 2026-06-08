# .secrets/ — credentials (git-ignored except this README)

Never commit anything here except this file. Tests use spy/no-op brokers and
make **no** network calls, so M0 needs no secrets.

Expected files (created when the relevant milestone needs live access):

| File | Milestone | Shape |
|------|-----------|-------|
| `databento.json` | M1 | `{"api_key": "..."}` |
| `alpaca.json` | M5 | `{"key_id": "...", "secret_key": "...", "base_url": "https://paper-api.alpaca.markets"}` |

Key B of two-key arming (live) is supplied at runtime (here or via operator env),
never committed; key A is the committed config flag. The live broker requires both.
