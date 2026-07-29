from datetime import datetime, timedelta, timezone

import pytest

from ledger import Ledger, REDDIT_SHEET_HEADERS
from reddit_sensor import analyze_posts, extract_tickers, sentiment_score


def post(post_id, title, *, score=0, comments=0, minutes_ago=5, subreddit="wallstreetbets"):
    return {
        "id": post_id,
        "title": title,
        "selftext": "",
        "score": score,
        "num_comments": comments,
        "created_utc": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp(),
        "subreddit": subreddit,
        "permalink": f"/r/{subreddit}/comments/{post_id}/test/",
    }


def signal_payload():
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "NVDA",
        "subreddits": ("wallstreetbets",),
        "sentiment_score": 0.8,
        "mention_count": 4,
        "weighted_mentions": 9.5,
        "baseline_mentions": 1.5,
        "baseline_samples": 6,
        "spike_ratio": 2.6667,
        "price_at_signal": 100.0,
        "technical_checked": True,
        "technical_pass": False,
        "above_kama": True,
        "rsi_oversold": False,
        "intraday_bounce": True,
        "rsi": 44.0,
        "lower_band": 31.0,
        "kama": 95.0,
        "atr": 4.0,
        "market_direction": "BULL",
        "realized_vol": 18.0,
        "sample_posts": ("https://www.reddit.com/r/wallstreetbets/comments/abc/test/",),
    }


def test_extract_tickers_uses_aegis_allowlist():
    assert extract_tickers("CEO likes $NVDA and AMD but not GDP", {"NVDA", "AMD"}) == {"NVDA", "AMD"}


def test_sentiment_is_directional_and_bounded():
    assert 0 < sentiment_score("bullish buy calls breakout") <= 1
    assert -1 <= sentiment_score("bearish sell puts crash") < 0


def test_post_aggregation_deduplicates_and_weights_engagement():
    now = datetime.now(timezone.utc)
    posts = [
        post("a", "$NVDA bullish calls", score=100, comments=25),
        post("a", "$NVDA bullish calls", score=100, comments=25),
        post("b", "NVDA buy breakout", score=5),
        post("c", "AMD bearish puts", score=50),
    ]
    aggregates = {item.symbol: item for item in analyze_posts(posts, {"NVDA", "AMD"}, now=now)}
    assert aggregates["NVDA"].mention_count == 2
    assert aggregates["NVDA"].sentiment_score > 0
    assert aggregates["AMD"].sentiment_score < 0


def test_reddit_signal_is_durable_and_queues_revisions(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    signal_id = ledger.add_reddit_signal(signal_payload())
    queued = ledger.unsynced_reddit_rows()
    assert len(queued) == 1
    assert len(__import__("json").loads(queued[0]["row_json"])) == len(REDDIT_SHEET_HEADERS)

    ledger.update_reddit_outcome(signal_id, "1h", 105.0, datetime.now(timezone.utc).isoformat())
    revisions = ledger.unsynced_reddit_rows()
    assert len(revisions) == 2
    latest = ledger.reddit_signals_needing_outcomes()[0]
    assert latest["return_1h"] == pytest.approx(0.05)


def test_reddit_baseline_uses_prior_observations(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    for index, count in enumerate((1, 2, 3)):
        ledger.record_reddit_observation(
            observed_at=f"2026-07-17T1{index}:00:00+00:00",
            symbol="NVDA",
            sentiment_score=0.5,
            mention_count=count,
            weighted_mentions=float(count),
            baseline_mentions=0,
            spike_ratio=1,
        )
    baseline, samples = ledger.reddit_baseline_mentions("NVDA", limit=12)
    assert samples == 3
    assert baseline == pytest.approx(2.0)
