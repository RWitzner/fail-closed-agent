# An autonomous trading agent that has never placed a single trade

**And that's the point.**

This is a complete autonomous US-equities trading agent: live market data, a $100k Alpaca **paper** account
with real credentials and the full order lifecycle wired, risk gates, a kill switch, an event-sourced journal,
and a dashboard. It ran a live session against real market data with no human in the loop. It has submitted
**zero orders** and lost **$0**.

Not because it was switched off. Because it was built so that opening a position requires passing a gate that
nothing has ever passed — and because the two strategies it was given were measured against criteria fixed in
advance, failed them, and were closed under a written stop rule.

There are three things here worth your time, and the trading strategies are not among them.

**1. A safety architecture for agents that can spend money.** Right now a lot of autonomous agents are being
handed credentials, budgets, and deploy access, and there are very few public examples of doing that carefully.
This repository is one: two-key arming, a single chokepoint that takes an unforgeable capability token,
fail-closed defaults on every unknown state, a bounded-blindness kill, an external source of truth that the
local model may label but never overwrite. None of it is trading-specific. → **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**

**2. A method: pre-registered research applied to trading.** Hypotheses and pass thresholds written down
*before* the data is pulled; a search budget fixed in advance; a stop rule that names what a null result
specifically does **not** authorise; adversarial multi-lens reviews of the designs, and — from the second
strategy family onward — a research packet predeclared and independently reviewed before the run. It was not
applied uniformly: the first family had pinned criteria but no research packet, and the first cross-model
review aimed at a strategy family came *after* that family's null, on the decision to stop. This is standard
in clinical research and close to nonexistent in public trading repositories.

**3. Two honest nulls, with the cost decomposition.** Both strategy families failed, and the measurements say
*why* — including the finding that on this data substrate the entry-leg half-spread alone consumes 88–94 % of
the entire execution-realism budget the criteria allow, before you even ask whether the signal is any good.
→ **[`docs/RESULTS.md`](docs/RESULTS.md)**

> **Do not use this to trade.** The strategies in this repository were measured by their own author and
> rejected. See [`DISCLAIMER.md`](DISCLAIMER.md).

---

## What it actually is

| | |
|---|---|
| **Domain** | US equities, intraday, single-name large-cap |
| **Data** | Databento historical and Alpaca IEX live, behind one pluggable transport seam (the Databento live seam is built but was never subscribed — it fails closed) |
| **Broker** | Alpaca *paper* — the committed adapter is paper-only by construction; paper and live share one API, so the path to live is the same code path |
| **Language** | Python 3, standard library only for everything that matters |
| **Tests** | **2000**, offline, hermetic — no install, no credentials, no external network |
| **Real money at risk, ever** | **$0** |
| **Orders submitted by the agent, ever** | **0** |

The one order this repository has ever sent to a broker was a deliberate infrastructure drill, not a trade: a
non-marketable $1.00 limit on AAPL, submitted and cancelled on the paper account on 2026-07-10 with zero shares
filled, by the account verifier (`agent.verify_alpaca_paper --allow-order-drill`, step A of the
[go-live checklist](docs/runbooks/paper-go-live-checklist.md); a fill is a hard verification failure there, not
an accepted outcome). It never went through the agent's decision loop or its token-gated `submit_order`
chokepoint — that path has still never been used.

It is built `observe → paper → live`, and it never left the first rung. Every tier is real: the recorder
records real vendor data with dual-hash replay; the market-state machine models sessions, halts, LULD bands,
short-sale restrictions and corporate actions; the risk tier implements an intraday margin model; the execution
tier drives a real broker's order lifecycle with a second-quote preflight for fill realism. The agent is
live-like on purpose — a safety property you only test in simulation is a safety property you have not tested.

### It ran, live, and did nothing — for auditable reasons

On 2026-07-10 it ran for 3 h 39 m against live IEX data with real broker credentials loaded — 15:29 to 19:08
UTC, stopped before the close, so no end-of-day report was written. In that time it made **438 decisions
(219 one-minute bars x two symbols). Every single one was `do_nothing`. It submitted no orders.**

Be precise about *why*, because the flattering version would be "five different gates each did their job". What
actually happened: the free IEX feed carries no usable limit-up/limit-down band, so the status plane could not
establish tradability and fell back to its documented `UNKNOWN` — which is wired to `halted`. **All 438
decisions carry `session_state: halted` and `tradability: not_tradable`.** The agent was structurally blocked
for the entire session by pattern 3 below, and it collected the other reasons on the way past:
`feature_cutoff_mismatch`, `features_unavailable`, `quote_stale`, `spread_too_wide`.

That is the fail-closed design working exactly as specified — an unverified status source means nothing opens —
rather than five independent gates being exercised. It is a weaker demonstration than it first looks, and a
truer one: the agent could not have opened a position that day even if a strategy had wanted to.

That is the whole thesis in one session. An agent that does nothing is trivial. An agent that does nothing
**while fully connected to live data with the whole order path wired to a real broker API, and can tell you
exactly which gate stopped it each time**, is the engineering result.

The account was an Alpaca *paper* account holding $100k of simulated money — and deliberately so: the broker
adapter is structurally paper-only. `AlpacaPaperBroker` raises at construction unless the credentials pin
`base_url` to the paper host, and this repository contains no live broker class at all.
→ `scripts/agent/broker/alpaca.py`, `tests/agent/test_alpaca_adapter.py`

---

## The safety architecture, concretely

Seven patterns, none of them about trading. Each is explained with its implementation and its test in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the short version:

**Two-key arming.** Touching real money needs a committed config flag (visible in git, visible in review) *and*
a runtime secret that is never committed. No single commit and no single process can supply both.
→ `scripts/agent/arming.py`, `tests/agent/test_two_key_arming.py`

**One chokepoint, guarded by a capability token.** There is exactly one function through which the agent can
open or increase a position, and it cannot be called without an object that only the gate system can mint.
Tokens cannot be constructed, copied, deep-copied or pickled — every one of those raises. They are typed by
capability, so *reducing* risk is never blocked by the machinery that blocks *increasing* it.
→ `scripts/agent/execution_preflight.py`, `tests/agent/test_preflight_token.py`
*(Honest limit: two broker writes are deliberately not token-gated — `cancel_order`, because cancelling reduces
risk, and the operator account-verification drill, which submits outside the agent loop. Both are named in the
architecture document.)*

**Fail closed on unknown.** Every external state has an explicit `UNKNOWN` that is wired to the restrictive
branch. A stale feed and a halted market produce the same decision. Integrations that have not been verified
against the real vendor API *raise* rather than proceed.
→ `scripts/agent/market_state.py`, `tests/agent/test_market_state.py`

**Bounded blindness.** If the agent cannot see authoritative account state for longer than a fixed bound while
holding a position, it flattens and halts. While blind, numeric limits are *skipped* rather than evaluated on
numbers it does not trust.
→ `scripts/agent/risk/risk_kill.py`, `tests/agent/test_orchestrator.py`

**External truth, local model as label only.** The broker's ledger is the position of record. The agent's own
modelled P&L is a different *type* (`ModeledUSD` vs `BrokerUSD`), and guards raise if one reaches the other's
slot. Conflating them is a test failure, not a production incident.
→ `scripts/agent/serializer.py`, `tests/agent/test_broker_reconcile.py`

**Append-only journal with per-row content hashes.** Every decision, order, fill and state transition is one
immutable JSONL row with a canonical content hash, correlation IDs and a per-stream monotonic sequence.
Corruption is fatal on read; a truncated final line from a crash mid-write is not.
→ `scripts/agent/journal.py`, `tests/agent/test_journal_replay.py`
*(Honest limit: rows are individually hashed, **not** chained. Deletion and reordering replay clean. The
architecture document explains what that costs and what a chain would fix.)*

**Predeclaration, a search budget, and a stop rule.** Criteria pinned before the run; artifact writers refuse
anything whose `rules_hash` is not the hash *derived* from the committed config; the verifier recomputes the
pass criteria rather than believing a `pass: true` field.
→ `scripts/agent/backtest_gate.py`, `scripts/agent/backtest_historical.py`

You can verify the posture yourself in about ten seconds:

```bash
python3 -c "import json;print(json.load(open('config/risk_rules.json'))['live_trading']['enabled'])"   # False
python3 -c "import json;d=json.load(open('config/agent_rules.json'));print(d['enabled'],d['paper_trading']['enabled'])"  # False False
ls -A artifacts/backtests/   # .gitkeep — nothing was ever promoted
```

Those three gates are `false` in the committed config, and were `false` in **every version of every config
file in the repository's entire history**.

---

## The results: two families, both nulled

Full detail, with the criteria fixed in advance and every measurement: **[`docs/RESULTS.md`](docs/RESULTS.md)**.

**Family 1 — intraday momentum.** Ten large-caps, L1 one-minute bars, 5-minute hold. Holdout window, 22
sessions, **11,368 trades: −$6,918.83 modelled, zero of ten symbols passing**, giving up $5,183.91 against
simply holding the same exposure. Profit factors from 0.001 to 0.71 against a 1.10 floor.

The adjacent 21-session window decomposes the loss, and the answer is not what you would guess. Over 9,923
trades on $8.35 M of notional, the signal marked frictionlessly mid-to-mid earned **+$21.70 — 0.03 bps,
indistinguishable from zero.** The round-trip half-spread cost **−$6,939.02** and fees **−$388.34**, for a net
of −$7,305.66. So the strategy was not badly wrong; it had nothing at all, and paid the spread 9,923 times to
find that out.

**Family 2 — intraday cross-sectional relative strength.** Same universe, long-only proxy, top-2 by rank,
30-bar horizon, on a clean window nothing had been measured on. 21 sessions, **1,144 trades: −$839.68
modelled, profit factor 0.55, average trade −8.78 bps.** The realism gaps blew both caps: p95 of **29.82 bps**
against a 15 bps limit, worst single fill **97.48 bps** against 50.

It beat one benchmark — by $405.64 against an equal-weight basket of the same names. That is exactly the trap
the two-benchmark rule exists to catch: it did not make money, the basket simply lost more. Against the
exposure-matched benchmark it was $120.65 worse.

**The finding that generalises.** Decomposing the execution gap: the **entry leg alone** has a p95 half-spread
of **13.27 bps — 88 % of the entire 15 bps budget** — and it is horizon-invariant, because you pay it once on
the way in no matter how long you hold. Set that against family 2's average trade of −8.78 bps. On Level-1
one-minute data in a ten-name large-cap universe, **the cost of getting in is the same order of magnitude as
anything these strategies could earn.** That is not a modelling artifact and a better signal does not fix it.

Two families nulled on one substrate triggered a stop rule written before the second one ran. It permitted a
substrate change and explicitly forbade the two most tempting alternatives: building the short side, and trying
a third variant of the same idea. The substrate experiment was predeclared, its feasibility was measured, the
measurement said the odds were poor — and it was never run. That is recorded rather than quietly dropped,
because a research packet that vanishes without a result is how selective reporting begins.

---

## Run it

The offline suite is standard-library only and runs on a bare checkout with **no installation at all**. It
reads no credentials and contacts no external host; three tests bind a loopback socket to exercise the local
dashboard, and that is the only networking it does:

```bash
git clone <this repo> && cd stocks
python3 -m unittest discover -s tests -p 'test_*.py' -t .
# Ran 2000 tests in ~8s — OK
```

The `-t .` is required: it sets the top-level directory to the repo root so tests import as `tests.agent.*`.
Without it, `discover -s tests` treats `tests/agent/` as a top-level `agent` package, shadows the real
`scripts/agent`, and the imports break. Python 3.11 or newer.

There is no installed package, so command-line entry points need `PYTHONPATH=scripts`:

```bash
PYTHONPATH=scripts python3 -m agent.paper_session --help      # the day runner
PYTHONPATH=scripts python3 -m dashboard --journal-dir journal # read-only local view, loopback only
```

`requirements.txt` pins the three third-party packages the credentialed paths use. Every one is imported
*lazily inside a function*, never at module scope, which is why the test suite stays green without them — and
why an outsider can read and test the whole system without a vendor account.

**What you cannot do:** reproduce the P&L numbers. They come from Databento and Alpaca market data, which may
not be redistributed, so the recorded quotes and run artifacts are git-ignored and not published. The method is
fully public — harness, criteria, manifest binding, verifier, thresholds, and the pipeline that produced the
numbers are all committed and tested. With your own vendor entitlement you can re-run it; from this repository
alone you can audit the method but not the data.

---

## Repository map

```
README.md              you are here
docs/ARCHITECTURE.md   the seven patterns — start here if you build agents, not strategies
docs/RESULTS.md        the two nulls, the criteria, the cost decomposition, the stop rule
docs/method/           two documents harvested from a separate prediction-market workspace
DISCLAIMER.md          not advice, no warranty, never traded real money
PLAN.md                the internal build log, frozen — the chronology is part of the evidence
CLAUDE.md              instructions for coding agents working in this repo (safety boundaries first)

config/                the three run gates and the risk rules — all false, all committed
scripts/agent/         orchestrator · strategy · risk · market state · execution preflight · journal
scripts/recorder/      vendor recorder, replay, dual-hash reconcile
dashboard/             stdlib-only read-only local view
tests/                 the 2000-test offline suite
docs/superpowers/      per-milestone contracts, plans, and adversarial review handoffs
```

`docs/superpowers/reviews/` is worth a look if the method interests you: those are the actual adversarial
review handoffs, including a cross-model review of the decision to stop, and the failure reviews written when
each family died. Nothing in them was softened or removed for publication; the only release-time change was
rewriting absolute local filesystem paths to repo-relative ones. Several were revised in-project after they
were first written — returned verdicts, resolution logs, post-hardening corrections — and `git log` on that
directory shows every such change.

---

## Honest limitations

- **The strategies have no edge.** Both were rejected on their own predeclared criteria. Do not trade them.
- **The nulls null tested configurations, not ideas.** Horizon, universe, substrate and strategy shape are
  confounded on purpose — separating them would have cost more than the answer was worth. The decision to stop
  follows from the budget rule, not from a proof about the substrate.
- **The journal is hashed per row, not chained.** Tamper-evidence against deletion or reordering is not there.
- **The live session's journal is not in this repository.** Its rows carry vendor-derived market-data
  provenance, so `journal/` is git-ignored. The 438-decision claim above therefore rests on the author's word.
  What you can check offline is the same machinery under replay (`tests/agent/test_observe_e2e.py`).
- **The paper-mode gates are single-key.** The two-key AND governs live capital only.
- **One data seam's fail-closed default has been deliberately inverted** after being verified against the live
  API — documented in the architecture notes, and an honest example of how a fail-closed posture erodes.
- **Family 1 had no research packet.** Its pass criteria were predeclared in a committed contract, but the
  universe list was recorded at the run rather than before it, and the design itself was never reviewed before
  it ran. Later packets hash-bind the universe and are reviewed before the run for exactly this reason.
- **The committed market-calendar fixture ends 2026-12-31.** After that the operational entry points refuse to
  run until it is regenerated (`agent.calendar_fixture`, committed) — fail-closed, but it will look like the
  tool is broken.
- **It has never been paper-validated, let alone live-validated.** Every claim about live behaviour here is a
  claim about a system that has correctly refused to do anything.

---

## How this was built, and what it is now

Almost all of the code, the specifications, the review handoffs and these documents were written by AI coding
agents working under the instruction files in this repository (`CLAUDE.md`, `AGENTS.md`) and under human
direction on every decision that mattered: what to test, what the criteria were, when to stop. The adversarial
reviews cited throughout were themselves run by agents, including across models. That is worth stating plainly
rather than leaving to be inferred from the `CLAUDE.md` in the root — and it is part of why the safety
architecture is built the way it is. An agent wrote the chokepoint that constrains agents.

This repository is **an archived artifact, not an active project.** The strategy search is stopped, no
maintenance is planned, and issues or pull requests may go unanswered. It is published to be read, cited and
copied from, not to be run.

---

## Licence

Apache-2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Databento and Alpaca are trademarks of their
respective owners; this project is not affiliated with or endorsed by either.
