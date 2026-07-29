import time
from unittest.mock import patch
import config as cfg
from news_filter import classify_sentiment, get_news_confidence, evaluate_news, _cache, fetch_news

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

# --- classify_sentiment ---
articles_positive = [{"title": "Good news", "published_at": "2026-07-29T10:00:00",
                      "entities": [{"name": "Reliance Industries", "sentiment_score": 0.6}]}]
check("Positive sentiment score classifies POSITIVE",
      classify_sentiment(articles_positive, "Reliance Industries")[0] == "POSITIVE")

articles_negative = [{"title": "Bad news", "published_at": "2026-07-29T10:00:00",
                      "entities": [{"name": "Reliance Industries", "sentiment_score": -0.5}]}]
check("Negative sentiment score classifies NEGATIVE",
      classify_sentiment(articles_negative, "Reliance Industries")[0] == "NEGATIVE")

articles_neutral = [{"title": "Meh news", "published_at": "2026-07-29T10:00:00",
                      "entities": [{"name": "Reliance Industries", "sentiment_score": 0.05}]}]
check("Near-zero sentiment score classifies NEUTRAL",
      classify_sentiment(articles_neutral, "Reliance Industries")[0] == "NEUTRAL")

check("No articles classifies UNKNOWN", classify_sentiment([], "Reliance Industries")[0] == "UNKNOWN")

articles_no_match = [{"title": "Unrelated", "published_at": "2026-07-29T10:00:00",
                      "entities": [{"name": "Some Other Company", "sentiment_score": 0.9}]}]
check("No matching entity classifies UNKNOWN",
      classify_sentiment(articles_no_match, "Reliance Industries")[0] == "UNKNOWN")

# --- get_news_confidence ---
score, decision, reason = get_news_confidence("BUY", "POSITIVE", 80, cfg)
check("BUY + POSITIVE increases confidence, proceeds", score == 85 and decision == "PROCEED")

score, decision, reason = get_news_confidence("BUY", "NEGATIVE", 80, cfg)
check("BUY + NEGATIVE rejects (block enabled)", decision == "REJECT")

score, decision, reason = get_news_confidence("SELL", "POSITIVE", 80, cfg)
check("SELL + POSITIVE reduces confidence", score == 75 and decision == "PROCEED")

score, decision, reason = get_news_confidence("SELL", "NEGATIVE", 80, cfg)
check("SELL + NEGATIVE increases confidence", score == 100 and decision == "PROCEED")  # clamped at 100

score, decision, reason = get_news_confidence("BUY", "NEUTRAL", 80, cfg)
check("BUY + NEUTRAL continues unchanged", score == 80 and decision == "PROCEED")

score, decision, reason = get_news_confidence("BUY", "UNKNOWN", 80, cfg)
check("BUY + UNKNOWN continues unchanged", score == 80 and decision == "PROCEED")

score, decision, reason = get_news_confidence("BUY", "NEGATIVE", 10, cfg)
check("Confidence never goes below 0", score >= 0)

# --- Fail-safe: fetch_news never raises, even on network error ---
with patch("requests.get", side_effect=Exception("network down")):
    result = fetch_news("Reliance Industries", cfg)
    check("fetch_news fails safe (empty list) on network error, does not raise", result == [])

# --- Caching ---
_cache.clear()
with patch("news_filter.fetch_news", return_value=[]):
    r1 = evaluate_news("TESTSYM", "Test Company", cfg)
    check("First evaluate_news call caches a result", "TESTSYM" in _cache)

with patch("news_filter.fetch_news", side_effect=Exception("should not be called")):
    r2 = evaluate_news("TESTSYM", "Test Company", cfg)
    check("Second call within cache window uses cache, does not refetch", r2 == r1)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
