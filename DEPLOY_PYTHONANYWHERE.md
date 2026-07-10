# PythonAnywhere deployment

Keep `.env`, `credentials.json`, the historical CSV, and `aegis_ledger.sqlite3`
outside version control. Back them up before replacing the deployed code.

Set `AEGIS_STRATEGY_CAPITAL=3600` in `.env`. This prevents Alpaca's much larger
paper-account equity from inflating the strategy's intended position sizes.

## Preferred: Always-on Task

Run one persistent process:

```text
cd ~/aegis-live && .venv/bin/python main.py
```

Set `AEGIS_RUN_ONCE=false`. The process lock exits with status 2 if another copy
is already active.

## Alternative: scheduled every five minutes

Set `AEGIS_RUN_ONCE=true` so each invocation performs one cycle and exits. Do
not schedule the old endless-loop configuration every five minutes.

Before restart, run:

```text
.venv/bin/python -m pytest -q
```

The bot defaults to paper mode. Do not add the live authorization variables.
