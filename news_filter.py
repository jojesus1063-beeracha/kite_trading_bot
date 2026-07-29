import time
import requests
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("news_filter")

_cache = {}


def _cache_get(symbol, ttl_minutes):
    entry = _cache.get(symbol)
    if entry is None:
        return None
    ts, result = entry
    if (time.time() - ts) > ttl_minutes * 60:
        return None
    return result


def _cache_set(symbol, result):
    _cache[symbol] = (time.time(), result)


def fetch_news(company_name, cfg):
    """
    Fetches recent news for a company from Marketaux, filtered to the
    last cfg.NEWS_LOOKBACK_HOURS. Returns a list of article dicts, or
    [] on any failure (never raises). Times out after
    cfg.NEWS_TIMEOUT_SECONDS.
    """
    api_key = getattr(cfg, "MARKETAUX_API_KEY", None)
    if not api_key:
        logger.warning("News source unavailable: MARKETAUX_API_KEY not configured")
        return []
    try:
        published_after = (datetime.utcnow() - timedelta(hours=cfg.NEWS_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
        resp = requests.get(
            "https://api.marketaux.com/v1/news/all",
            params={
                "search": company_name,
                "countries": "in",
                "published_after": published_after,
                "api_token": api_key,
                "limit": 5,
            },
            timeout=cfg.NEWS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        logger.warning(f"News source unavailable ({company_name}): {e}")
        return []


def classify_sentiment(articles, company_name):
    """
    Classifies overall sentiment from a list of articles as POSITIVE,
    NEGATIVE, NEUTRAL, or UNKNOWN (if no articles). Uses the entity
    sentiment_score (already computed by Marketaux) for whichever
    entity best matches company_name, averaged across articles.
    """
    if not articles:
        return "UNKNOWN", None, None

    scores = []
    best_headline, best_published = None, None
    for article in articles:
        for entity in article.get("entities", []):
            name = (entity.get("name") or "").lower()
            if company_name.lower() in name or name in company_name.lower():
                score = entity.get("sentiment_score")
                if score is not None:
                    scores.append(score)
                    if best_headline is None:
                        best_headline = article.get("title")
                        best_published = article.get("published_at")

    if not scores:
        return "UNKNOWN", None, None

    avg_score = sum(scores) / len(scores)
    if avg_score > 0.15:
        sentiment = "POSITIVE"
    elif avg_score < -0.15:
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"
    return sentiment, best_headline, best_published


def evaluate_news(symbol, company_name, cfg):
    """
    Full evaluation for one symbol: checks cache, fetches if needed,
    classifies sentiment. Returns a dict with sentiment/headline/
    published_at, always -- never raises, fails safe to UNKNOWN.
    """
    cached = _cache_get(symbol, cfg.NEWS_CACHE_MINUTES)
    if cached is not None:
        return cached

    try:
        articles = fetch_news(company_name, cfg)
        sentiment, headline, published_at = classify_sentiment(articles, company_name)
        result = {"sentiment": sentiment, "headline": headline, "published_at": published_at}
    except Exception as e:
        logger.warning(f"News evaluation failed for {symbol}: {e}")
        result = {"sentiment": "UNKNOWN", "headline": None, "published_at": None}

    _cache_set(symbol, result)
    return result


def get_news_confidence(direction, sentiment, base_score, cfg):
    """
    Applies the news-based confidence modifier per direction and
    sentiment, clamped to [0, 100]. Returns (final_score, decision,
    reason) where decision is "PROCEED" or "REJECT".
    """
    reason = "no news impact"
    score = base_score

    if direction == "BUY":
        if sentiment == "POSITIVE":
            score += cfg.POSITIVE_NEWS_CONFIDENCE_BONUS
            reason = "positive news, confidence increased"
        elif sentiment == "NEGATIVE":
            if getattr(cfg, "NEGATIVE_NEWS_BLOCK", True):
                return max(0, min(100, score)), "REJECT", "negative corporate news"
            score -= cfg.NEGATIVE_NEWS_CONFIDENCE_PENALTY
            reason = "negative news, confidence reduced"
        elif sentiment == "UNKNOWN":
            reason = "news unavailable, continuing normally"
    else:  # SELL
        if sentiment == "POSITIVE":
            score -= cfg.POSITIVE_NEWS_CONFIDENCE_BONUS
            reason = "positive news reduces SELL confidence"
        elif sentiment == "NEGATIVE":
            score += cfg.NEGATIVE_NEWS_CONFIDENCE_PENALTY
            reason = "negative news increases SELL confidence"
        elif sentiment == "UNKNOWN":
            reason = "news unavailable, continuing normally"

    score = max(0, min(100, score))
    return score, "PROCEED", reason
