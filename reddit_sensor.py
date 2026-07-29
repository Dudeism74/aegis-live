"""Observe Reddit sentiment without granting it authority to place trades."""

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import requests


TICKER_PATTERN = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z]{1,5})(?![A-Za-z0-9])")
POSITIVE_PHRASES = {
    "buy": 1.0,
    "buying": 1.0,
    "bullish": 1.5,
    "calls": 1.0,
    "call options": 1.2,
    "long": 0.8,
    "moon": 1.0,
    "breakout": 1.2,
    "squeeze": 0.8,
    "rip": 0.7,
    "upside": 0.8,
    "undervalued": 1.0,
    "beat": 0.7,
    "strong": 0.5,
}
NEGATIVE_PHRASES = {
    "sell": 1.0,
    "selling": 1.0,
    "bearish": 1.5,
    "puts": 1.0,
    "put options": 1.2,
    "short": 0.8,
    "crash": 1.2,
    "dump": 1.0,
    "rug": 1.0,
    "downside": 0.8,
    "overvalued": 1.0,
    "miss": 0.7,
    "bankrupt": 1.2,
    "bankruptcy": 1.2,
    "weak": 0.5,
}


@dataclass(frozen=True)
class RedditAggregate:
    symbol: str
    sentiment_score: float
    mention_count: int
    weighted_mentions: float
    subreddits: tuple[str, ...]
    sample_posts: tuple[str, ...]


@dataclass(frozen=True)
class PositiveRedditSignal:
    symbol: str
    observed_at: str
    sentiment_score: float
    mention_count: int
    weighted_mentions: float
    baseline_mentions: float
    baseline_samples: int
    spike_ratio: float
    subreddits: tuple[str, ...]
    sample_posts: tuple[str, ...]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def extract_tickers(text: str, allowed_tickers: Iterable[str]) -> set[str]:
    allowed = {ticker.upper() for ticker in allowed_tickers}
    return {match.group(1).upper() for match in TICKER_PATTERN.finditer(text or "") if match.group(1).upper() in allowed}


def sentiment_score(text: str) -> float:
    """Return a deterministic score from -1 to 1 for auditable research."""
    lowered = f" {str(text or '').lower()} "
    positive = sum(weight * lowered.count(phrase) for phrase, weight in POSITIVE_PHRASES.items())
    negative = sum(weight * lowered.count(phrase) for phrase, weight in NEGATIVE_PHRASES.items())
    raw = positive - negative
    if "/s" in lowered or "yeah right" in lowered:
        raw *= -1
    return float(math.tanh(raw / 2.5))


def analyze_posts(
    posts: Iterable[dict[str, Any]],
    allowed_tickers: Iterable[str],
    *,
    now: datetime | None = None,
    lookback_minutes: int = 120,
    max_samples: int = 5,
) -> list[RedditAggregate]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now_utc.timestamp() - max(1, lookback_minutes) * 60
    buckets: dict[str, dict[str, Any]] = {}
    seen_posts: set[str] = set()

    for post in posts:
        post_id = str(post.get("id") or "")
        if not post_id or post_id in seen_posts:
            continue
        seen_posts.add(post_id)
        created = float(post.get("created_utc") or 0)
        if created < cutoff:
            continue
        text = f"{post.get('title', '')}\n{post.get('selftext', '')}"
        tickers = extract_tickers(text, allowed_tickers)
        if not tickers:
            continue
        score = sentiment_score(text)
        engagement = max(0, int(post.get("score") or 0)) + max(0, int(post.get("num_comments") or 0))
        weight = 1.0 + math.log1p(engagement)
        subreddit = str(post.get("subreddit") or "unknown")
        permalink = str(post.get("permalink") or "")
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink

        for ticker in tickers:
            bucket = buckets.setdefault(
                ticker,
                {"weighted_sum": 0.0, "weight": 0.0, "count": 0, "subreddits": set(), "samples": []},
            )
            bucket["weighted_sum"] += score * weight
            bucket["weight"] += weight
            bucket["count"] += 1
            bucket["subreddits"].add(subreddit)
            if url and len(bucket["samples"]) < max_samples:
                bucket["samples"].append(url)

    aggregates: list[RedditAggregate] = []
    for ticker, bucket in buckets.items():
        total_weight = float(bucket["weight"])
        aggregates.append(
            RedditAggregate(
                symbol=ticker,
                sentiment_score=float(bucket["weighted_sum"] / total_weight) if total_weight else 0.0,
                mention_count=int(bucket["count"]),
                weighted_mentions=total_weight,
                subreddits=tuple(sorted(bucket["subreddits"])),
                sample_posts=tuple(bucket["samples"]),
            )
        )
    return sorted(aggregates, key=lambda item: (item.sentiment_score, item.mention_count), reverse=True)


class RedditClient:
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    API_ROOT = "https://oauth.reddit.com"

    def __init__(self, client_id: str, client_secret: str, user_agent: str, timeout_seconds: float = 15):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        response = self.session.post(
            self.TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("Reddit OAuth response did not contain an access token")
        self._token = token
        self._token_expires_at = time.time() + float(payload.get("expires_in") or 3600)
        return token

    def newest_posts(self, subreddit: str, limit: int = 100) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.API_ROOT}/r/{subreddit}/new",
            params={"limit": max(1, min(int(limit), 100)), "raw_json": 1},
            headers={"Authorization": f"bearer {self._access_token()}", "User-Agent": self.user_agent},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        children = response.json().get("data", {}).get("children", [])
        return [child.get("data", {}) for child in children if isinstance(child, dict)]


class RedditSensor:
    """Collect positive Reddit observations and leave trade authority unchanged."""

    def __init__(
        self,
        client: RedditClient,
        ledger: Any,
        allowed_tickers: Iterable[str],
        *,
        subreddits: Iterable[str],
        post_limit: int = 100,
        lookback_minutes: int = 120,
        minimum_mentions: int = 3,
        minimum_sentiment: float = 0.35,
        minimum_spike_ratio: float = 1.5,
        baseline_samples: int = 12,
        cooldown_minutes: int = 240,
    ):
        self.client = client
        self.ledger = ledger
        self.allowed_tickers = tuple(ticker.upper() for ticker in allowed_tickers)
        self.subreddits = tuple(item.strip() for item in subreddits if item.strip())
        self.post_limit = post_limit
        self.lookback_minutes = lookback_minutes
        self.minimum_mentions = minimum_mentions
        self.minimum_sentiment = minimum_sentiment
        self.minimum_spike_ratio = minimum_spike_ratio
        self.baseline_samples = baseline_samples
        self.cooldown_minutes = cooldown_minutes

    @classmethod
    def from_env(cls, ledger: Any, allowed_tickers: Iterable[str]) -> "RedditSensor | None":
        if not env_bool("AEGIS_REDDIT_ENABLED", False):
            return None
        client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
        client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
        user_agent = os.getenv("REDDIT_USER_AGENT", "").strip()
        if not client_id or not client_secret or not user_agent:
            logging.error("Reddit sensor enabled but REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, or REDDIT_USER_AGENT is missing")
            return None
        subreddits = os.getenv("AEGIS_REDDIT_SUBREDDITS", "wallstreetbets,stocks,options").split(",")
        return cls(
            RedditClient(client_id, client_secret, user_agent),
            ledger,
            allowed_tickers,
            subreddits=subreddits,
            post_limit=int(os.getenv("AEGIS_REDDIT_POST_LIMIT", "100")),
            lookback_minutes=int(os.getenv("AEGIS_REDDIT_LOOKBACK_MINUTES", "120")),
            minimum_mentions=int(os.getenv("AEGIS_REDDIT_MIN_MENTIONS", "3")),
            minimum_sentiment=float(os.getenv("AEGIS_REDDIT_MIN_SENTIMENT", "0.35")),
            minimum_spike_ratio=float(os.getenv("AEGIS_REDDIT_MIN_SPIKE_RATIO", "1.5")),
            baseline_samples=int(os.getenv("AEGIS_REDDIT_BASELINE_SAMPLES", "12")),
            cooldown_minutes=int(os.getenv("AEGIS_REDDIT_COOLDOWN_MINUTES", "240")),
        )

    def scan(self, observed_at: datetime | None = None) -> list[PositiveRedditSignal]:
        now_utc = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        posts: list[dict[str, Any]] = []
        for subreddit in self.subreddits:
            try:
                posts.extend(self.client.newest_posts(subreddit, self.post_limit))
            except Exception as exc:
                logging.error("Reddit fetch failed for r/%s: %s", subreddit, exc)
        if not posts:
            return []

        positives: list[PositiveRedditSignal] = []
        for aggregate in analyze_posts(
            posts,
            self.allowed_tickers,
            now=now_utc,
            lookback_minutes=self.lookback_minutes,
        ):
            baseline, sample_count = self.ledger.reddit_baseline_mentions(
                aggregate.symbol, limit=self.baseline_samples
            )
            spike_ratio = aggregate.mention_count / baseline if baseline > 0 else float(aggregate.mention_count)
            self.ledger.record_reddit_observation(
                observed_at=now_utc.isoformat(),
                symbol=aggregate.symbol,
                sentiment_score=aggregate.sentiment_score,
                mention_count=aggregate.mention_count,
                weighted_mentions=aggregate.weighted_mentions,
                baseline_mentions=baseline,
                spike_ratio=spike_ratio,
            )
            enough_history = sample_count >= min(3, self.baseline_samples)
            spike_ok = spike_ratio >= self.minimum_spike_ratio if enough_history else True
            if (
                aggregate.sentiment_score < self.minimum_sentiment
                or aggregate.mention_count < self.minimum_mentions
                or not spike_ok
                or self.ledger.has_recent_reddit_signal(
                    aggregate.symbol, now_utc.isoformat(), self.cooldown_minutes
                )
            ):
                continue
            positives.append(
                PositiveRedditSignal(
                    symbol=aggregate.symbol,
                    observed_at=now_utc.isoformat(),
                    sentiment_score=aggregate.sentiment_score,
                    mention_count=aggregate.mention_count,
                    weighted_mentions=aggregate.weighted_mentions,
                    baseline_mentions=baseline,
                    baseline_samples=sample_count,
                    spike_ratio=spike_ratio,
                    subreddits=aggregate.subreddits,
                    sample_posts=aggregate.sample_posts,
                )
            )
        return positives
