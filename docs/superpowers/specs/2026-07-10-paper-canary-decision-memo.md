# Decision memo — paper trading without a validated edge (the S9 question)

**Status:** PROPOSAL ONLY. Nothing in this memo is built or armed. Robin's
standing goal (2026-07-10: "vi skal ikke vente på M7d — vi skal i gang, det er
okay at fejle, vi justerer løbende") collides with exactly ONE committed gate,
and weakening a committed gate requires an explicit, separately-approved
decision — so the collision is surfaced here instead of being coded around.

## The situation, precisely

Everything else on the path to an unattended paper trader is now built and
merged (status data plane, reconnect, supervisor, weekly aggregator, live
dashboard, evidence/report hardening). The remaining blockers are:

1. **External (only Robin can do):** step A (Alpaca paper account →
   `.secrets/alpaca_paper.json` + verifier + drill), step B (paid Databento
   live subscription + one tier-2b verification session), Track D drills
   (incl. the status-channel coverage drill), step D (reviewed caps commit),
   step E (runtime arming `.secrets/run_gates.json`).
2. **The S9 gate:** `execution_preflight` stage 5 requires a passing reviewed
   backtest artifact for every NON-synthetic strategy before any OPEN. No
   strategy has one (momentum nulled, RS-proxy nulled, M7d not run and a GO
   is structurally improbable on the measured realism floors). With S9 as it
   stands, an armed paper session runs, journals, reconciles — and never
   opens a real-strategy position.

"Se trades i dag" works ALREADY without touching S9: replay/synthetic
sessions produce the full order lifecycle in the dashboard (verified
end-to-end 2026-07-10), and observe-mode live sessions (steps A+B, no
arming) bank live-data operational evidence with zero orders.

## The options

**Option 1 — observe-mode now, S9 unchanged (built, zero decisions needed).**
Do steps A+B, run `agent.paper_autorun` daily in observe/live, watch the
dashboard, let M7d (or a later family) decide whether trading ever arms.
Honest limitation: no real orders, so "justere løbende" adjusts data/ops
quality, not trading behavior.

**Option 2 — predeclared PAPER CANARY without validated edge (the change
this memo exists to decide).** Amend the runbook + preflight so paper opens
are allowed for a named strategy WITHOUT an S9 artifact, under ALL of:

- a new committed, git-visible flag (e.g.
  `agent_rules.paper_canary.allow_unvalidated_strategy = {strategy_id}`,
  default ABSENT) — key A, reviewed commit;
- the runtime gates file must ALSO name the same strategy id — key B, so no
  single commit/process arms it (mirrors the live two-key discipline);
- tighten-only micro-caps in the reviewed step-D commit (proposal:
  `max_position_usd ≤ 500`, `max_gross_exposure_usd ≤ 1000`,
  `max_daily_loss_usd ≤ 50`, one symbol, long-only) — the point is
  operational learning, not PnL;
- S1 unchanged: committed config alone still opens NOTHING; the canary tests
  stay green;
- an explicit journal row + report marker `unvalidated_canary=true` on every
  session so the evidence can never masquerade as edge-validated paper
  evidence (it feeds the OPS half of the weekly report, never step C);
- live_trading untouched; M8 untouched; the paper-phase EDGE criteria still
  require a passing artifact — the canary cannot promote anything.

Cost: one TDD pass through preflight stage 5 + config schema + runbook +
canary tests, reviewed. This memo deliberately does NOT include the code.

**Option 3 — wait for M7d (~2026-07-14 holdout) before deciding.** The
research path stays clean; if C2 GOes (improbable) S9 gets a real artifact
and Option 2 is moot.

## Recommendation

Do Option 1's external steps NOW regardless (they are prerequisites for every
path). Decide Option 2 explicitly — yes or no — rather than letting it happen
implicitly: it is a deliberate, bounded loosening of one gate for operational
learning, which is defensible under "paper er til at fejle", but it must be
predeclared, capped, labeled, and reviewed so the evidence streams stay
honest. If yes, the build follows this memo as its predeclaration.

**Explicitly not done pending this decision:** no preflight change, no config
schema change, no cap value committed. S9 blocks unvalidated opens today,
exactly as before.
