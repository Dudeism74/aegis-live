# Reddit research sensor

This sensor is an observation and research lane. It does not submit orders, increase position size, bypass KAMA, RSI, ATR, the entry window, risk limits, paper safeguards, or any other Aegis trading rule.

Aegis checks Reddit every 15 minutes during open market hours. It reads recent posts through Reddit OAuth, extracts only symbols already present in the Aegis ticker list, calculates an auditable deterministic sentiment score, compares mention count with recent observations, and emits a positive signal only when the configured sentiment, mention, and spike thresholds pass.

After a positive Reddit signal, Aegis runs the existing KAMA, BB-RSI, bounce, and ATR checks for that ticker. The result is logged even when the technical setup fails. No order is submitted by the Reddit path.

A new `Reddit Sensor` worksheet is created automatically in `Aegis Trading Log`. Each signal records the Reddit score, mention count, baseline, spike ratio, source subreddits, sample post links, price at signal, every technical gate, market regime, and SPY realized volatility. The same row is updated with the first available market-open price at least 1 hour, 24 hours, and 120 hours after the signal.

The SQLite ledger remains the source of truth. Sheet writes use a durable revision queue and are retried without stopping trading. If Google Sheets is unavailable, research records remain queued locally.

## Reddit access

Create an approved Reddit API application and place its client ID, client secret, and a descriptive user agent in `.env`. Use OAuth credentials. This implementation intentionally does not fall back to the unofficial `.json` endpoint.

Set:

```text
AEGIS_REDDIT_ENABLED=true
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=aegis-research/1.0 by u/your_username
```

Keep the sensor disabled until all three values are present. A missing credential disables Reddit collection without affecting Aegis trading.

## Interpretation

A sentiment score is not a probability that a stock will rise. The 1 hour, 24 hour, and 120 hour returns measure precision after positive signals. They do not measure false negatives because Aegis does not log every stock that Reddit failed to flag.
