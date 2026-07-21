"""
Browser-based dashboard for the trading bot: configure settings,
pick your watchlist with tappable stock chips, see live price/trend
info for whatever's selected, and review trade history.

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

BASE_STYLE = """
<style>
  :root {
    --accent: #00b386;
    --accent-dark: #009973;
    --loss: #e5484d;
    --bg: #f4f6f8;
    --card: #ffffff;
    --border: #e3e7eb;
    --text: #1a1f2b;
    --text-muted: #6b7280;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 0;
  }
  .topbar {
    background: var(--accent);
    color: white;
    padding: 16px 24px;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 0.3px;
  }
  .container {
    max-width: 900px;
    margin: 24px auto;
    padding: 0 16px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
    margin-bottom: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .card h2 {
    margin-top: 0;
    font-size: 18px;
    font-weight: 700;
  }
  label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-muted);
    margin: 16px 0 6px;
  }
  input[type=text], input[type=number], input[type=password] {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    background: #fafbfc;
  }
  input:focus { outline: none; border-color: var(--accent); }
  .btn {
    background: var(--accent);
    color: white;
    border: none;
    padding: 12px 28px;
    border-radius: 8px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    margin-top: 20px;
  }
  .btn:hover { background: var(--accent-dark); }
  .chip-grid { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .chip-input { display: none; }
  .chip-label {
    display: inline-block;
    padding: 7px 14px;
    border-radius: 20px;
    border: 1px solid var(--border);
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-muted);
    background: #fafbfc;
    transition: all 0.12s;
    user-select: none;
  }
  .chip-input:checked + .chip-label {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px;
  }
  .stock-card {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
    background: #fafbfc;
  }
  .stock-symbol { font-weight: 700; font-size: 14px; }
  .stock-price { font-size: 22px; font-weight: 700; margin: 4px 0; }
  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 700;
  }
  .pill.up { background: #e3f9f0; color: var(--accent); }
  .pill.down { background: #fdeceb; color: var(--loss); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--text-muted); font-weight: 600; padding: 8px; border-bottom: 2px solid var(--border); }
  td { padding: 8px; border-bottom: 1px solid var(--border); }
  .banner {
    background: #fdeceb;
    color: var(--loss);
    padding: 12px 16px;
    border-radius: 8px;
    font-size: 14px;
    margin-bottom: 16px;
  }
  .checkbox-row { display: flex; align-items: center; gap: 8px; margin-top: 20px; }
  .checkbox-row input { width: auto; }
  a { color: var(--accent); }
</style>
"""

LOGIN_PAGE = BASE_STYLE + """
<!doctype html>
<title>Login</title>
<body>
  <div class="topbar">Trading Bot</div>
  <div class="container" style="max-width: 380px;">
    <div class="card">
      <h2>Log in</h2>
      {% if error %}<p style="color: var(--loss);">{{ error }}</p>{% endif %}
      <form method="post">
        <input type="password" name="password" placeholder="Password">
        <button type="submit" class="btn" style="width:100%;">Log in</button>
      </form>
    </div>
  </div>
</body>
"""

FORM_PAGE = BASE_STYLE + """
<!doctype html>
<title>Trading Bot Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<body>
  <div class="topbar">Trading Bot Dashboard</div>
  <div class="container">

    {% if saved %}<div class="banner" style="background:#e3f9f0; color: var(--accent);">Saved — restart the bot for changes to take effect.</div>{% endif %}

    <div class="card">
      <h2>Watchlist &amp; Settings</h2>
      <form method="post">
        <label>Stocks to trade (tap to select)</label>
        <div class="chip-grid">
          {% for sym in stock_universe %}
            <input type="checkbox" id="chip-{{ sym }}" name="watchlist" value="{{ sym }}"
                   class="chip-input" {{ 'checked' if sym in selected_watchlist else '' }}>
            <label for="chip-{{ sym }}" class="chip-label">{{ sym }}</label>
          {% endfor %}
        </div>

        <label>Add another symbol not listed above (comma-separated)</label>
        <input type="text" name="extra_symbols" placeholder="e.g. IRCTC, ZOMATO">

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
          <div>
            <label>Trading capital (INR)</label>
            <input type="number" name="capital" value="{{ capital }}">
          </div>
          <div>
            <label>Risk per trade (%)</label>
            <input type="number" step="0.1" name="risk_per_trade_pct" value="{{ risk_per_trade_pct }}">
          </div>
          <div>
            <label>Stop-loss buffer (%)</label>
            <input type="number" step="0.01" name="sl_buffer_pct" value="{{ sl_buffer_pct }}">
          </div>
          <div>
            <label>Minimum reward:risk ratio</label>
            <input type="number" step="0.1" name="risk_reward_min" value="{{ risk_reward_min }}">
          </div>
          <div>
            <label>Max trades per day</label>
            <input type="number" name="max_trades_per_day" value="{{ max_trades_per_day }}">
          </div>
          <div>
            <label>Max daily loss (%) — kill switch</label>
            <input type="number" step="0.1" name="max_daily_loss_pct" value="{{ max_daily_loss_pct }}">
          </div>
          <div>
            <label>Trend EMA — fast (15-min)</label>
            <input type="number" name="trend_ema_fast" value="{{ trend_ema_fast }}">
          </div>
          <div>
            <label>Trend EMA — slow (15-min)</label>
            <input type="number" name="trend_ema_slow" value="{{ trend_ema_slow }}">
          </div>
          <div>
            <label>Entry EMA (5-min)</label>
            <input type="number" name="entry_ema" value="{{ entry_ema }}">
          </div>
        </div>

        <div class="checkbox-row">
          <input type="checkbox" name="paper_trading" id="paper_trading" {{ 'checked' if paper_trading else '' }}>
          <label for="paper_trading" style="margin:0;">Paper trading (simulate only — uncheck ONLY when ready to risk real money)</label>
        </div>

        <button type="submit" class="btn">Save</button>
      </form>
    </div>

    <div class="card">
      <h2>Live Dashboard</h2>
      {% if dashboard_error %}
        <div class="banner">
          Couldn't load live prices: {{ dashboard_error }}<br>
          Make sure you've run <code>python3 auth.py</code> today to connect to Kite.
        </div>
      {% elif not selected_watchlist %}
        <p style="color: var(--text-muted);">No stocks selected above yet.</p>
      {% else %}
        <div class="grid">
          {% for stock in dashboard_data %}
            <div class="stock-card">
              <div class="stock-symbol">{{ stock.symbol }}</div>
              {% if stock.error %}
                <span style="color: var(--loss); font-size: 13px;">{{ stock.error }}</span>
              {% else %}
                <div class="stock-price">₹{{ "%.2f"|format(stock.ltp) }}</div>
                <span class="pill {{ 'up' if stock.change_pct >= 0 else 'down' }}">
                  {{ "%.2f"|format(stock.change_pct) }}%
                </span>
                <canvas id="chart-{{ stock.symbol }}" width="150" height="50" style="margin-top:8px;"></canvas>
              {% endif %}
            </div>
          {% endfor %}
        </div>
        <p style="color: var(--text-muted); font-size: 12px; margin-top: 12px;">Reload the page to refresh prices.</p>
      {% endif %}
    </div>

    <div class="card">
      <h2>Trade History</h2>
      <p>
        Today: {{ today_summary.count }} trade(s), total P&amp;L:
        <span class="pill {{ 'up' if today_summary.total_pnl >= 0 else 'down' }}">
          ₹{{ "%.2f"|format(today_summary.total_pnl) }}
        </span>
      </p>
      {% if trade_history %}
        <table>
          <tr>
            <th>Date</th><th>Time</th><th>Symbol</th><th>Dir</th><th>Qty</th>
            <th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Result</th>
          </tr>
          {% for t in trade_history %}
            <tr>
              <td>{{ t.date }}</td>
              <td>{{ t.time }}</td>
              <td>{{ t.symbol }}</td>
              <td>{{ t.direction }}</td>
              <td>{{ t.qty }}</td>
              <td>{{ "%.2f"|format(t.entry) }}</td>
              <td>{{ "%.2f"|format(t.exit) }}</td>
              <td><span class="pill {{ 'up' if t.pnl >= 0 else 'down' }}">{{ "%.2f"|format(t.pnl) }}</span></td>
              <td>{{ t.result }}</td>
            </tr>
          {% endfor %}
        </table>
      {% else %}
        <p style="color: var(--text-muted);">No trades recorded yet — this fills in once the bot runs and closes its first position.</p>
      {% endif %}
    </div>

    <p><a href="/logout">Log out</a></p>
  </div>

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
            borderColor: '#00b386',
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.25,
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
        watchlist = list(dict.fromkeys(selected + extra))

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
