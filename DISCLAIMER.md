# Disclaimer

**This is not investment advice.** Nothing in this repository — the code, the documents, the measurements, or
the conclusions — is a recommendation to buy, sell, or hold any security, or to adopt any trading strategy.

**No warranty.** The software is provided "AS IS", without warranty of any kind, express or implied, as set out
in the Apache License, Version 2.0 (see `LICENSE`). The author is not liable for any loss arising from its use.

**It has never traded real money.** The three run gates are `false` in the committed configuration and have been
`false` in every commit in this repository's history. No reviewed backtest artifact passes the promotion gate,
which independently blocks every real-strategy open. Total money lost or gained in the market: zero.

**The strategies do not work.** Both strategy families in this repository were tested against criteria that were
written down before the runs, and both failed those criteria. They are published as negative results — evidence
of what does *not* work — not as something to run. Using them to trade would be using code its own author
measured and rejected.

**Backtests are not returns.** Every profit-and-loss figure reported here is a *modelled* figure produced by a
simulator over historical vendor data. It is labelled `ModeledUSD` in the code precisely to keep it separate
from broker-reported money (`BrokerUSD`). No modelled figure was ever realised.

**Trading involves risk of loss.** Automated trading adds failure modes that manual trading does not have:
software defects, stale or wrong market data, broker outages, unhandled corporate actions, and orders that
execute far from where you expected. This repository is largely *about* those failure modes. Reading it should
make you more cautious, not less.

**If you run any part of this, you do so at your own risk**, and you are responsible for complying with the
terms of service and data licences of any broker or data vendor you connect it to.
