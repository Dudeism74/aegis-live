# PythonAnywhere deployment

Keep `.env`, `credentials.json`, the historical CSV, and `aegis_ledger.sqlite3`
outside version control. Back them up before replacing the deployed code.

Set `AEGIS_STRATEGY_CAPITAL=3600` in `.env`. This prevents Alpaca's much larger
paper-account equity from inflating the strategy's intended position sizes.

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
