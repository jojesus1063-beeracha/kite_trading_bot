"""
Browser-based dashboard for the trading bot: configure settings,
pick your watchlist from a dropdown, see live price/trend info for
whatever's selected, and review trade history.

Run this, then visit http://YOUR_SERVER_IP:5000 in any browser.

IMPORTANT — set a password before running this on a public server:
    export CONFIG_UI_PASSWORD="something only you know"
Without this set, the app refuses to start, since this form would
otherwise be reachable by anyone who finds your server's IP.

This does NOT restart main.py automatically — if the bot is already
running, stop and restart it after saving changes for them to apply.
"""

import json
import os
from datetime import datetime, timedelta

from flask import Flask, request, redirect, session, render_template_string

import config as cfg
from auth import get_kite_client
from stocks import STOCK_UNIVERSE
from trade_log import get_trade_history, get_today_summary

app = Flask(__name__)
app.secret_key = os.environ.get("CONFIG_UI_SECRET", os.urandom(24).hex())

PASSWORD = os.environ.get("CONFIG_UI_PASSWORD")
if not PASSWORD:
    raise SystemExit(
        "CONFIG_UI_PASSWORD is not set. Run:\n"
        '  export CONFIG_UI_PASSWORD="choose_a_password"\n'
        "before starting this app, so the form isn't open to anyone."
    )

USER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config.json")
INSTRUMENTS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instruments_cache.json")
INSTRUMENTS_CACHE_MAX_AGE_DAYS = 7

LOGIN_PAGE = """
<!doctype html>
<title>Login</title>
<body style="font-family: sans-serif; max-width: 400px; margin: 80px auto;">
  <h2>Bot Configuration</h2>
  {% if error %}<p style="color: red;">{{ error }}</p>{% endif %}
  <form method="post">
    <input type="password" name="password" placeholder="Password" style="width: 100%; padding: 8px; font-size: 16px;">
    <button type="submit" style="margin-top: 10px; padding: 8px 16px;">Log in</button>
  </form>
</body>
"""

FORM_PAGE = """
<!doctype html>
<title>Bot Configuration</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<body style="font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 0 16px;">
  <h2>Trading Bot Configuration</h2>
  {% if saved %}<p style="color: green;">Saved. Restart the bot for changes to take effect.</p>{% endif %}
  <form method="post">
    <label><strong>Stocks to trade</strong> (hold Ctrl / Cmd to select more than one)</label><br>
    <select name="watchlist" multiple size="12" style="width: 100%; padding: 6px; font-size: 15px;">
      {% for sym in stock_universe %}
        <option value="{{ sym }}" {{ 'selected' if sym in selected_watchlist else '' }}>{{ sym }}</option>
      {% endfor %}
    </select><br><br>

    <label>Add another symbol not in the list above (comma-separated)</label><br>
    <input type="text" name="extra_symbols" placeholder="e.g. IRCTC, ZOMATO" style="width: 100%; padding: 6px;"><br><br>

    <label>Trading capital (INR)</label><br>
    <input type="number" name="capital" value="{{ capital }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Risk per trade (% of capital)</label><br>
    <input type="number" step="0.1" name="risk_per_trade_pct" value="{{ risk_per_trade_pct }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Stop-loss buffer (% beyond signal candle)</label><br>
    <input type="number" step="0.01" name="sl_buffer_pct" value="{{ sl_buffer_pct }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Minimum reward:risk ratio</label><br>
    <input type="number" step="0.1" name="risk_reward_min" value="{{ risk_reward_min }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Max trades per day</label><br>
    <input type="number" name="max_trades_per_day" value="{{ max_trades_per_day }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Max daily loss (% of capital) — kill switch</label><br>
    <input type="number" step="0.1" name="max_daily_loss_pct" value="{{ max_daily_loss_pct }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Trend EMA — fast period (15-min chart)</label><br>
    <input type="number" name="trend_ema_fast" value="{{ trend_ema_fast }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Trend EMA — slow period (15-min chart)</label><br>
    <input type="number" name="trend_ema_slow" value="{{ trend_ema_slow }}" style="width: 100%; padding: 6px;"><br><br>

    <label>Entry EMA period (5-min chart)</label><br>
    <input type="number" name="entry_ema" value="{{ entry_ema }}" style="width: 100%; padding: 6px;"><br><br>

    <label>
      <input type="checkbox" name="paper_trading" {{ 'checked' if paper_trading else '' }}>
      Paper trading (simulate only — uncheck ONLY when ready to risk real money)
    </label><br><br>

    <button type="submit" style="padding: 10px 20px; font-size: 16px;">Save</button>
  </form>
  <p><a href="/logout">Log out</a></p>

  <hr style="margin: 40px 0;">

  <h2>Live Dashboard</h2>
  {% if dashboard_error %}
    <p style="color: #a00; background: #fee; padding: 12px; border-radius: 4px;">
      Couldn't load live prices: {{ dashboard_error }}<br>
      Make sure you've run <code>python3 auth.py</code> today to connect to Kite.
    </p>
  {% elif not selected_watchlist %}
    <p>No stocks selected above yet.</p>
  {% else %}
    <div style="display: flex; flex-wrap: wrap; gap: 16px;">
      {% for stock in dashboard_data %}
        <div style="border: 1px solid #ccc; border-radius: 6px; padding: 12px; width: 220px;">
          <strong>{{ stock.symbol }}</strong><br>
          {% if stock.error %}
            <span style="color: #a00;">{{ stock.error }}</span>
          {% else %}
            <span style="font-size: 20px;">₹{{ "%.2f"|format(stock.ltp) }}</span><br>
            <span style="color: {{ 'green' if stock.change_pct >= 0 else '#a00' }};">
              {{ "%.2f"|format(stock.change_pct) }}%
            </span>
            <canvas id="chart-{{ stock.symbol }}" width="200" height="60"></canvas>
          {% endif %}
        </div>
      {% endfor %}
    </div>
    <p style="color: #777; font-size: 13px;">Reload the page to refresh prices.</p>
  {% endif %}

  <script>
    const sparkData = {{ spark_json|safe }};
    for (const symbol in sparkData) {
      const canvas = document.getElementById("chart-" + symbol);
      if (!canvas) continue;
      new Chart(canvas, {
        type: 'line',
        data: {
          labels: sparkData[symbol].map((_, i) => i),
          datasets: [{
            data: sparkData[symbol],
            borderColor: '#3366cc',
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.2,
          }]
        },
        options: {
          responsive: false,
          plugins: { legend: { display: false } },
          scales: { x: { display: false }, y: { display: false } }
        }
      });
    }
  </script>

  <hr style="margin: 40px 0;">

  <h2>Trade History</h2>
  <p>
    Today: {{ today_summary.count }} trade(s),
    total P&L:
    <span style="color: {{ 'green' if today_summary.total_pnl >= 0 else '#a00' }};">
      ₹{{ "%.2f"|format(today_summary.total_pnl) }}
    </span>
  </p>
  {% if trade_history %}
    <table style="width: 100%; border-collapse: collapse;">
      <tr style="border-bottom: 2px solid #333; text-align: left;">
        <th style="padding: 6px;">Date</th>
        <th style="padding: 6px;">Time</th>
        <th style="padding: 6px;">Symbol</th>
        <th style="padding: 6px;">Dir</th>
        <th style="padding: 6px;">Qty</th>
        <th style="padding: 6px;">Entry</th>
        <th style="padding: 6px;">Exit</th>
        <th style="padding: 6px;">P&L</th>
        <th style="padding: 6px;">Result</th>
      </tr>
      {% for t in trade_history %}
        <tr style="border-bottom: 1px solid #ddd;">
          <td style="padding: 6px;">{{ t.date }}</td>
          <td style="padding: 6px;">{{ t.time }}</td>
          <td style="padding: 6px;">{{ t.symbol }}</td>
          <td style="padding: 6px;">{{ t.direction }}</td>
          <td style="padding: 6px;">{{ t.qty }}</td>
          <td style="padding: 6px;">{{ "%.2f"|format(t.entry) }}</td>
          <td style="padding: 6px;">{{ "%.2f"|format(t.exit) }}</td>
          <td style="padding: 6px; color: {{ 'green' if t.pnl >= 0 else '#a00' }};">{{ "%.2f"|format(t.pnl) }}</td>
          <td style="padding: 6px;">{{ t.result }}</td>
        </tr>
      {% endfor %}
    </table>
  {% else %}
    <p>No trades recorded yet — this fills in once the bot runs and closes its first position.</p>
  {% endif %}
</body>
"""


def load_current():
    if os.path.exists(USER_CONFIG_PATH):
        with open(USER_CONFIG_PATH) as f:
            saved = json.load(f)
    else:
        saved = {}
    return {
        "watchlist": saved.get("watchlist", cfg.WATCHLIST),
        "capital": saved.get("capital", cfg.CAPITAL),
        "risk_per_trade_pct": saved.get("risk_per_trade_pct", cfg.RISK_PER_TRADE_PCT),
        "sl_buffer_pct": saved.get("sl_buffer_pct", cfg.SL_BUFFER_PCT),
        "risk_reward_min": saved.get("risk_reward_min", cfg.RISK_REWARD_MIN),
        "max_trades_per_day": saved.get("max_trades_per_day", cfg.MAX_TRADES_PER_DAY),
        "max_daily_loss_pct": saved.get("max_daily_loss_pct", cfg.MAX_DAILY_LOSS_PCT),
        "trend_ema_fast": saved.get("trend_ema_fast", cfg.TREND_EMA_FAST),
        "trend_ema_slow": saved.get("trend_ema_slow", cfg.TREND_EMA_SLOW),
        "entry_ema": saved.get("entry_ema", cfg.ENTRY_EMA),
        "paper_trading": saved.get("paper_trading", cfg.PAPER_TRADING),
    }


def get_instrument_map(kite):
    """Cache NSE instrument tokens on disk — refetching the full list
    (thousands of rows) on every page load would be slow and wasteful."""
    if os.path.exists(INSTRUMENTS_CACHE_PATH):
        age_days = (datetime.now().timestamp() - os.path.getmtime(INSTRUMENTS_CACHE_PATH)) / 86400
        if age_days < INSTRUMENTS_CACHE_MAX_AGE_DAYS:
            with open(INSTRUMENTS_CACHE_PATH) as f:
                return json.load(f)

    instruments = kite.instruments("NSE")
    mapping = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
    with open(INSTRUMENTS_CACHE_PATH, "w") as f:
        json.dump(mapping, f)
    return mapping


def get_dashboard_data(symbols):
    """Returns (data, error). data is a list of dicts per symbol;
    error is a user-facing string if the whole fetch failed (e.g. no
    valid access token yet today)."""
    if not symbols:
        return [], None

    try:
        kite = get_kite_client()
    except Exception as e:
        return None, f"not connected to Kite ({e})"

    try:
        quote_keys = [f"NSE:{s}" for s in symbols]
        quotes = kite.quote(quote_keys)
    except Exception as e:
        return None, str(e)

    try:
        instrument_map = get_instrument_map(kite)
    except Exception:
        instrument_map = {}

    results = []
    for s in symbols:
        q = quotes.get(f"NSE:{s}")
        if not q:
            results.append({"symbol": s, "error": "No data returned"})
            continue

        ltp = q["last_price"]
        prev_close = q.get("ohlc", {}).get("close") or ltp
        change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0.0

        spark = []
        token = instrument_map.get(s)
        if token:
            try:
                to_date = datetime.now()
                from_date = to_date - timedelta(days=45)
                candles = kite.historical_data(token, from_date, to_date, "day")
                spark = [c["close"] for c in candles[-30:]]
            except Exception:
                spark = []

        results.append({"symbol": s, "ltp": ltp, "change_pct": change_pct, "spark": spark})

    return results, None


def require_login():
    return session.get("logged_in") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["logged_in"] = True
            return redirect("/")
        error = "Wrong password."
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/", methods=["GET", "POST"])
def index():
    if not require_login():
        return redirect("/login")

    saved = False
    if request.method == "POST":
        selected = request.form.getlist("watchlist")
        extra = [s.strip().upper() for s in request.form.get("extra_symbols", "").split(",") if s.strip()]
        watchlist = list(dict.fromkeys(selected + extra))  # dedup, keep order

        data = {
            "watchlist": watchlist,
            "capital": float(request.form["capital"]),
            "risk_per_trade_pct": float(request.form["risk_per_trade_pct"]),
            "sl_buffer_pct": float(request.form["sl_buffer_pct"]),
            "risk_reward_min": float(request.form["risk_reward_min"]),
            "max_trades_per_day": int(request.form["max_trades_per_day"]),
            "max_daily_loss_pct": float(request.form["max_daily_loss_pct"]),
            "trend_ema_fast": int(request.form["trend_ema_fast"]),
            "trend_ema_slow": int(request.form["trend_ema_slow"]),
            "entry_ema": int(request.form["entry_ema"]),
            "paper_trading": "paper_trading" in request.form,
        }
        with open(USER_CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
        saved = True

    current = load_current()
    universe = sorted(set(STOCK_UNIVERSE) | set(current["watchlist"]))

    dashboard_data, dashboard_error = get_dashboard_data(current["watchlist"])
    spark_json = json.dumps({
        d["symbol"]: d["spark"] for d in (dashboard_data or []) if not d.get("error")
    })

    trade_history = get_trade_history(limit=50)
    today_summary = get_today_summary()

    return render_template_string(
        FORM_PAGE,
        saved=saved,
        stock_universe=universe,
        selected_watchlist=current["watchlist"],
        dashboard_data=dashboard_data,
        dashboard_error=dashboard_error,
        spark_json=spark_json,
        trade_history=trade_history,
        today_summary=today_summary,
        **{k: v for k, v in current.items() if k != "watchlist"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
