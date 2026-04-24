"""Sentiment data fetcher — pulls from X (Twitter) and Yahoo Finance."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SentimentContext:
    tweets: str
    yahoo_news: str
    yahoo_summary: str

    def as_text(self) -> str:
        parts = []
        if self.tweets:
            parts.append(f"<twitter_sentiment>\n{self.tweets}\n</twitter_sentiment>")
        if self.yahoo_news:
            parts.append(f"<yahoo_news>\n{self.yahoo_news}\n</yahoo_news>")
        if self.yahoo_summary:
            parts.append(f"<yahoo_summary>\n{self.yahoo_summary}\n</yahoo_summary>")
        return "\n\n".join(parts) if parts else "No sentiment data available."


def fetch_x_sentiment(query: str = "BTC OR Bitcoin", max_results: int = 20) -> str:
    """Search recent tweets via X API v2 using bearer token from Keychain."""
    try:
        from claw_v2.social import _load_keychain_credential
        bearer = _load_keychain_credential("x-bearer-token")
        if not bearer:
            logger.warning("X bearer token not found in Keychain")
            return ""

        import httpx
        resp = httpx.get(
            "https://api.x.com/2/tweets/search/recent",
            params={
                "query": f"{query} -is:retweet lang:en",
                "max_results": min(max_results, 100),
                "tweet.fields": "created_at,public_metrics,author_id",
                "sort_order": "relevancy",
            },
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("X API error %d: %s", resp.status_code, resp.text[:200])
            return ""

        tweets = resp.json().get("data", [])
        lines = []
        for t in tweets:
            metrics = t.get("public_metrics", {})
            likes = metrics.get("like_count", 0)
            rts = metrics.get("retweet_count", 0)
            text = t.get("text", "").replace("\n", " ")
            lines.append(f"[{likes}♥ {rts}↻] {text}")
        return "\n".join(lines)

    except Exception as e:
        logger.warning("X sentiment fetch failed: %s", e)
        return ""


def fetch_yahoo_finance(ticker: str = "BTC-USD") -> tuple[str, str]:
    """Fetch news headlines and price summary from Yahoo Finance via yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # News headlines
        news_lines = []
        for item in (t.news or [])[:10]:
            title = item.get("title", "")
            publisher = item.get("publisher", "")
            if title:
                news_lines.append(f"- [{publisher}] {title}")
        news = "\n".join(news_lines) if news_lines else ""

        # Price summary
        info = t.info or {}
        summary_parts = []
        for key in ["regularMarketPrice", "regularMarketChange", "regularMarketChangePercent",
                     "fiftyDayAverage", "twoHundredDayAverage", "marketCap",
                     "volume", "averageVolume", "fiftyTwoWeekHigh", "fiftyTwoWeekLow"]:
            val = info.get(key)
            if val is not None:
                label = key.replace("regularMarket", "").replace("fiftyTwoWeek", "52w_")
                label = label.replace("fiftyDay", "50d_").replace("twoHundredDay", "200d_")
                summary_parts.append(f"{label}: {val}")

        summary = " | ".join(summary_parts) if summary_parts else ""
        return news, summary

    except Exception as e:
        logger.warning("Yahoo Finance fetch failed: %s", e)
        return "", ""


def fetch_sentiment(asset: str = "BTC") -> SentimentContext:
    """Fetch sentiment from all sources for a given asset."""
    ticker_map = {"BTC": ("BTC OR Bitcoin", "BTC-USD"),
                  "ETH": ("ETH OR Ethereum", "ETH-USD"),
                  "DOGE": ("DOGE OR Dogecoin", "DOGE-USD")}
    x_query, yf_ticker = ticker_map.get(asset, (asset, f"{asset}-USD"))

    tweets = fetch_x_sentiment(x_query)
    yahoo_news, yahoo_summary = fetch_yahoo_finance(yf_ticker)

    return SentimentContext(
        tweets=tweets,
        yahoo_news=yahoo_news,
        yahoo_summary=yahoo_summary,
    )
