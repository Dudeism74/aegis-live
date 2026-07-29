# PythonAnywhere deployment

Keep `.env`, `credentials.json`, the historical CSV, and `aegis_ledger.sqlite3`
outside version control. Back them up before replacing the deployed code.

Keep these paper-trading safeguards in `.env`:

```text
AEGIS_TRADING_MODE=paper
AEGIS_STRATEGY_CAPITAL=3600
AEGIS_LIVE_AUTHORIZED=false
```

The strategy-capital limit prevents Alpaca's much larger paper-account equity
from inflating the intended position sizes. Live mode remains blocked unless
explicit authorization is true and the configured approved account matches the
runtime account. Do not add live account identifiers to source control.

## Choose exactly one operating model

The always-on process and the five-minute scheduled task are mutually
exclusive. Do not configure both. Both models use the same process lock, which
is released on normal exit and exceptions. It blocks overlapping persistent
instances without preventing later scheduled runs after an earlier run exits.

## Model 1: always-on process

Run one persistent process:

```text
cd ~/aegis-live && .venv/bin/python main.py
```

Set:

```text
AEGIS_RUN_ONCE=false
AEGIS_CYCLE_SECONDS=300
```

The process lock exits with status 2 if another copy is already active.

## Model 2: scheduled every five minutes

Set:

```text
AEGIS_RUN_ONCE=true
```

Each invocation performs one cycle, releases the lock, and exits. Do not set
`AEGIS_RUN_ONCE=false` in a task scheduled every five minutes.

Before restart, run:

```text
.venv/bin/python -m pytest -q
```

The bot defaults to paper mode. Do not add the live authorization variables.

## QQQ risk governor and PSQ hedge

The hedge overlay is independent of the KAMA, RSI, ATR, ticker, entry-window,
and strategy-capital settings. It uses current-session QQQ performance from
Alpaca snapshots.

Keep the overlay in observation mode first:

```text
AEGIS_HEDGE_MODE=observe
AEGIS_HEDGE_SYMBOL=PSQ
AEGIS_HEDGE_MODERATE_QQQ_RETURN=-0.01
AEGIS_HEDGE_SEVERE_QQQ_RETURN=-0.015
AEGIS_HEDGE_MODERATE_EXPOSURE_FACTOR=0.50
AEGIS_HEDGE_RATIO=0.25
AEGIS_HEDGE_MIN_REBALANCE_USD=25
AEGIS_HEDGE_REBALANCE_TOLERANCE=0.10
AEGIS_HEDGE_ENTRY_CONFIRM_CYCLES=3
AEGIS_HEDGE_EXIT_CONFIRM_CYCLES=3
AEGIS_HEDGE_REENTRY_COOLDOWN_MINUTES=120
```

Observation mode logs the QQQ risk state and hypothetical PSQ target, but it
does not change entries or submit hedge orders. After reviewing paper results,
set `AEGIS_HEDGE_MODE=paper` to activate it. Active hedge mode is blocked if
Aegis is connected to a live account.

In active paper mode, a QQQ session return at or below -1.0% halves the
notional of otherwise valid entries. A return at or below -1.5% blocks new
entries and targets PSQ at 25% of the current market value of Aegis-managed
long positions. An existing hedge remains open through the moderate state to
reduce threshold churn, then closes when the state returns to normal or no
Aegis-managed longs remain. If QQQ snapshot data is unavailable, new entries
are blocked, and any existing hedge is left unchanged.

PSQ entry requires three consecutive severe observations, and a normal-state
exit requires three consecutive observations. With the standard five-minute
cycle, each confirmation takes about fifteen minutes. A full confirmed exit
starts a durable 120-minute reentry cooldown that survives process restarts.
An interrupted sequence, a gap longer than fifteen minutes, or a new trading
day resets confirmation. The immediate QQQ entry governor is unchanged:
severe risk still blocks new strategy longs while hedge confirmation is
pending. Closing PSQ because no Aegis-managed longs remain is immediate.

An open hedge may be reduced as strategy exposure falls, but it is never
averaged down or increased during the same risk event.

PSQ is excluded from the five-position strategy limit and from ATR stop/target
management. Hedge submissions, fills, reconciliation, restart recovery, and
Google Sheet rows use the same confirmed-fill-only ledger path as strategy
orders. Every hedge reason and email alert includes the QQQ session return and
the applicable confirmation state. The existing Sheet1 column layout is
unchanged.

PSQ targets the inverse of the Nasdaq-100's daily return. It is a short-term
overlay, not a permanent holding. Product source:
https://www.proshares.com/our-etfs/leveraged-and-inverse/psq

Do not disable the overlay while PSQ is open without first closing or manually
reconciling that hedge. Restart the always-on process after changing `.env`.

## Paper trading report card

Aegis creates a `Report Card` tab in the existing `Aegis Trading Log`
workbook. It uses only confirmed SELL events with a Ledger Event ID and leaves
the 34-column `Sheet1` layout unchanged.

The card separately grades win rate, expectancy in R, profit factor, maximum
realized drawdown, and net realized PSQ hedge cost. Grades remain `N/A` until
30 completed strategy exits. A paper-readiness result requires at least 50
completed strategy exits and every graded metric at C or better. This result is
an evaluation gate only; it never changes the paper-only safeguards or
authorizes live trading.

Hedge cost covers realized PSQ profit or loss only. It does not include an open
hedge's unrealized result, commissions, taxes, or opportunity cost.
