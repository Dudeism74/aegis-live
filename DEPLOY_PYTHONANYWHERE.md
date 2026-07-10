# PythonAnywhere deployment

Keep `.env`, `credentials.json`, the historical CSV, and `aegis_ledger.sqlite3`
outside version control. Back them up before replacing the deployed code.

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
.venv/bin/python -m unittest discover -v
```

The bot defaults to paper mode. Do not add the live authorization variables.
