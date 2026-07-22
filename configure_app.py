"""
Browser-based dashboard for the trading bot: configure settings,
pick your watchlist with tappable stock chips (each with its own
NSE/BSE choice), see live price/trend info for whatever's selected,
and review trade history.

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
from backtest import run_backtest_data

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
INSTRUMENTS_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
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
  .exchange-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
  .exchange-table td { padding: 8px 4px; border-bottom: 1px solid var(--border); }
  .exchange-table .sym { font-weight: 700; }
  .radio-pair { display: flex; gap: 16px; }
  .radio-pair label { display: inline-flex; align-items: center; gap: 4px; font-weight: 500; color: var(--text); margin: 0; }
  .radio-pair input { width: auto; }
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
  .stock-exchange { font-size: 11px; color: var(--text-muted); font-weight: 600; }
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
  table.history { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.history th { text-align: left; color: var(--text-muted); font-weight: 600; padding: 8px; border-bottom: 2px solid var(--border); }
  table.history td { padding: 8px; border-bottom: 1px solid var(--border); }
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
                   class="chip-input" {{ 'checked' if sym in selected_symbols else '' }}>
            <label for="chip-{{ sym }}" class="chip-label">{{ sym }}</label>
          {% endfor %}
        </div>

        <label>Add another symbol not listed above (comma-separated)</label>
        <input type="text" name="extra_symbols" placeholder="e.g. IRCTC, ZOMATO">

        {% if selected_symbols %}
          <label>Exchange for each selected stock</label>
          <table class="exchange-table">
            {% for sym in selected_symbols %}
              <tr>
                <td class="sym">{{ sym }}</td>
                <td>
                  <div class="radio-pair">
                    <label>
                      <input type="radio" name="exchange_{{ sym }}" value="NSE"
                             {{ 'checked' if exchange_map.get(sym, 'NSE') == 'NSE' else '' }}> NSE
                    </label>
                    <label>
                      <input type="radio" name="exchange_{{ sym }}" value="BSE"
                             {{ 'checked' if exchange_map.get(sym, 'NSE') == 'BSE' else '' }}> BSE
                    </label>
                  </div>
                </td>
              </tr>
            {% endfor %}
          </table>
          <p style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
            Any symbol you add via the text field above defaults to NSE — save once, then set its exchange here.
          </p>
        {% endif %}

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px;">
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
          <div>
            <label>ADX threshold (trend-strength filter)</label>
            <input type="number" name="adx_threshold" value="{{ adx_threshold }}">
          </div>
        </div>

        <div class="checkbox-row">
          <input type="checkbox" name="use_adx_filter" id="use_adx_filter" {{ 'checked' if use_adx_filter else '' }}>
          <label for="use_adx_filter" style="margin:0;">
            Require ADX trend-strength confirmation (filters out choppy false trends — see backtest before enabling live)
          </label>
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
      {% elif not selected_symbols %}
        <p style="color: var(--text-muted);">No stocks selected above yet.</p>
      {% else %}
        <div class="grid">
          {% for stock in dashboard_data %}
            <div class="stock-card">
              <div class="stock-symbol">{{ stock.symbol }}</div>
              <div class="stock-exchange">{{ stock.exchange }}</div>
              {% if stock.error %}
                <span style="color: var(--loss); font-size: 13px;">{{ stock.error }}</span>
              {% else %}
                <div class="stock-price">₹{{ "%.2f"|format(stock.ltp) }}</div>
                <span class="pill {{ 'up' if stock.change_pct >= 0 else 'down' }}">
                  {{ "%.2f"|format(stock.change_pct) }}%
                </span>
                <canvas id="chart-{{ stock.symbol }}-{{ stock.exchange }}" width="150" height="50" style="margin-top:8px;"></canvas>
              {% endif %}
            </div>
          {% endfor %}
        </div>
        <p style="color: var(--text-muted); font-size: 12px; margin-top: 12px;">Reload the page to refresh prices.</p>
      {% endif %}
    </div>

    <div class="card">
      <h2>Strategy Backtest Comparison</h2>
      <p style="color: var(--text-muted); font-size: 13px;">
        Runs the same period twice — once with the ADX filter off, once on — so you can see whether it actually helps before flipping it on live.
      </p>
      <form method="post" action="/backtest">
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
          <div>
            <label>Symbol</label>
            <input type="text" name="bt_symbol" placeholder="e.g. RELIANCE" value="{{ bt_symbol or '' }}">
          </div>
          <div>
            <label>Exchange</label>
            <div class="radio-pair" style="margin-top: 10px;">
              <label><input type="radio" name="bt_exchange" value="NSE" checked> NSE</label>
              <label><input type="radio" name="bt_exchange" value="BSE"> BSE</label>
            </div>
          </div>
          <div>
            <label>From date</label>
            <input type="date" name="bt_from_date" value="{{ bt_from_date or '' }}">
          </div>
          <div>
            <label>To date</label>
            <input type="date" name="bt_to_date" value="{{ bt_to_date or '' }}">
          </div>
        </div>
        <button type="submit" class="btn">Run Comparison</button>
      </form>

      {% if backtest_result %}
        {% if backtest_result.error %}
          <div class="banner" style="margin-top: 20px;">
            Couldn't run backtest: {{ backtest_result.error }}<br>
            Make sure you've run <code>python3 auth.py</code> today, and that the symbol/exchange/dates are valid.
          </div>
        {% else %}
          <p style="margin-top: 20px; color: var(--text-muted);">
            {{ backtest_result.exchange }}:{{ backtest_result.symbol }},
            {{ backtest_result.from_date }} to {{ backtest_result.to_date }}
          </p>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
            <div class="stock-card">
              <div class="stock-symbol">ADX filter OFF</div>
              {% set r = backtest_result.off %}
              {% if r.total_trades == 0 %}
                <p style="color: var(--text-muted); font-size: 13px;">No trades in this period.</p>
              {% else %}
                <p>Trades: {{ r.total_trades }}</p>
                <p>Win rate: {{ "%.1f"|format(r.win_rate) }}%</p>
                <p>Total P&amp;L: <span class="pill {{ 'up' if r.total_pnl >= 0 else 'down' }}">₹{{ "%.2f"|format(r.total_pnl) }}</span></p>
                <p>Avg P&amp;L/trade: ₹{{ "%.2f"|format(r.avg_pnl) }}</p>
              {% endif %}
            </div>
            <div class="stock-card">
              <div class="stock-symbol">ADX filter ON</div>
              {% set r = backtest_result.on %}
              {% if r.total_trades == 0 %}
                <p style="color: var(--text-muted); font-size: 13px;">No trades in this period.</p>
              {% else %}
                <p>Trades: {{ r.total_trades }}</p>
                <p>Win rate: {{ "%.1f"|format(r.win_rate) }}%</p>
                <p>Total P&amp;L: <span class="pill {{ 'up' if r.total_pnl >= 0 else 'down' }}">₹{{ "%.2f"|format(r.total_pnl) }}</span></p>
                <p>Avg P&amp;L/trade: ₹{{ "%.2f"|format(r.avg_pnl) }}</p>
              {% endif %}
            </div>
          </div>
        {% endif %}
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
        <table class="history">
          <tr>
            <th>Date</th><th>Time</th><th>Symbol</th><th>Exch</th><th>Dir</th><th>Qty</th>
            <th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Result</th>
          </tr>
          {% for t in trade_history %}
            <tr>
              <td>{{ t.date }}</td>
              <td>{{ t.time }}</td>
              <td>{{ t.symbol }}</td>
              <td>{{ t.get('exchange', 'NSE') }}</td>
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
    for (const key in sparkData) {
      const canvas = document.getElementById("chart-" + key);
      if (!canvas) continue;
      new Chart(canvas, {
        type: 'line',
        data: {
          labels: sparkData[key].map((_, i) => i),
          datasets: [{
            data: sparkData[key],
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

    saved_watchlist = saved.get("watchlist")
    if saved_watchlist is None:
        watchlist = cfg.WATCHLIST
    else:
        # Support both plain-string (old) and dict (new) formats.
        watchlist = [
            {"symbol": w, "exchange": "NSE"} if isinstance(w, str) else w
            for w in saved_watchlist
        ]

    return {
        "watchlist": watchlist,
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
        "use_adx_filter": saved.get("use_adx_filter", cfg.USE_ADX_FILTER),
        "adx_threshold": saved.get("adx_threshold", cfg.ADX_THRESHOLD),
    }


def get_instrument_map(kite, exchange):
    """Cache instrument tokens per exchange on disk — refetching the
    full list (thousands of rows) on every page load would be slow."""
    cache_path = os.path.join(INSTRUMENTS_CACHE_DIR, f"instruments_cache_{exchange.lower()}.json")
    if os.path.exists(cache_path):
        age_days = (datetime.now().timestamp() - os.path.getmtime(cache_path)) / 86400
        if age_days < INSTRUMENTS_CACHE_MAX_AGE_DAYS:
            with open(cache_path) as f:
                return json.load(f)

    instruments = kite.instruments(exchange)
    mapping = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
    with open(cache_path, "w") as f:
        json.dump(mapping, f)
    return mapping


def get_dashboard_data(watchlist):
    """watchlist is a list of {"symbol", "exchange"} dicts.
    Returns (data, error)."""
    if not watchlist:
        return [], None

    try:
        kite = get_kite_client()
    except Exception as e:
        return None, f"not connected to Kite ({e})"

    try:
        quote_keys = [f"{w['exchange']}:{w['symbol']}" for w in watchlist]
        quotes = kite.quote(quote_keys)
    except Exception as e:
        return None, str(e)

    instrument_maps = {}
    results = []
    for w in watchlist:
        symbol, exchange = w["symbol"], w["exchange"]
        q = quotes.get(f"{exchange}:{symbol}")
        if not q:
            results.append({"symbol": symbol, "exchange": exchange, "error": "No data returned"})
            continue

        ltp = q["last_price"]
        prev_close = q.get("ohlc", {}).get("close") or ltp
        change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0.0

        if exchange not in instrument_maps:
            try:
                instrument_maps[exchange] = get_instrument_map(kite, exchange)
            except Exception:
                instrument_maps[exchange] = {}

        spark = []
        token = instrument_maps[exchange].get(symbol)
        if token:
            try:
                to_date = datetime.now()
                from_date = to_date - timedelta(days=45)
                candles = kite.historical_data(token, from_date, to_date, "day")
                spark = [c["close"] for c in candles[-30:]]
            except Exception:
                spark = []

        results.append({"symbol": symbol, "exchange": exchange, "ltp": ltp,
                         "change_pct": change_pct, "spark": spark})

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


def _summarize_backtest(result):
    if result is None:
        return None
    return {
        "total_trades": result["total_trades"],
        "win_rate": result["win_rate"],
        "total_pnl": result["total_pnl"],
        "avg_pnl": result["avg_pnl"],
    }


@app.route("/backtest", methods=["POST"])
def backtest_comparison():
    if not require_login():
        return redirect("/login")

    symbol = request.form.get("bt_symbol", "").strip().upper()
    exchange = request.form.get("bt_exchange", "NSE")
    from_date = request.form.get("bt_from_date")
    to_date = request.form.get("bt_to_date")

    result = {"symbol": symbol, "exchange": exchange, "from_date": from_date, "to_date": to_date}

    if not symbol or not from_date or not to_date:
        result["error"] = "Please fill in symbol and both dates."
    else:
        original_use_adx = cfg.USE_ADX_FILTER
        try:
            cfg.USE_ADX_FILTER = False
            off_result = run_backtest_data(symbol, from_date, to_date, exchange)
            cfg.USE_ADX_FILTER = True
            on_result = run_backtest_data(symbol, from_date, to_date, exchange)
            result["off"] = _summarize_backtest(off_result)
            result["on"] = _summarize_backtest(on_result)
            result["error"] = None
        except Exception as e:
            result["error"] = str(e)
        finally:
            cfg.USE_ADX_FILTER = original_use_adx

    session["backtest_result"] = result
    return redirect("/")


@app.route("/", methods=["GET", "POST"])
def index():
    if not require_login():
        return redirect("/login")

    saved = False
    if request.method == "POST":
        selected = request.form.getlist("watchlist")
        extra = [s.strip().upper() for s in request.form.get("extra_symbols", "").split(",") if s.strip()]
        all_symbols = list(dict.fromkeys(selected + extra))

        watchlist = [
            {"symbol": s, "exchange": request.form.get(f"exchange_{s}", "NSE")}
            for s in all_symbols
        ]

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
            "use_adx_filter": "use_adx_filter" in request.form,
            "adx_threshold": float(request.form["adx_threshold"]),
        }
        with open(USER_CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
        saved = True

    current = load_current()
    selected_symbols = [w["symbol"] for w in current["watchlist"]]
    exchange_map = {w["symbol"]: w["exchange"] for w in current["watchlist"]}
    universe = sorted(set(STOCK_UNIVERSE) | set(selected_symbols))

    dashboard_data, dashboard_error = get_dashboard_data(current["watchlist"])
    spark_json = json.dumps({
        f"{d['symbol']}-{d['exchange']}": d["spark"] for d in (dashboard_data or []) if not d.get("error")
    })

    trade_history = get_trade_history(limit=50)
    today_summary = get_today_summary()
    backtest_result = session.pop("backtest_result", None)

    return render_template_string(
        FORM_PAGE,
        saved=saved,
        stock_universe=universe,
        selected_symbols=selected_symbols,
        exchange_map=exchange_map,
        dashboard_data=dashboard_data,
        dashboard_error=dashboard_error,
        spark_json=spark_json,
        trade_history=trade_history,
        today_summary=today_summary,
        backtest_result=backtest_result,
        bt_symbol=backtest_result["symbol"] if backtest_result else None,
        bt_from_date=backtest_result["from_date"] if backtest_result else None,
        bt_to_date=backtest_result["to_date"] if backtest_result else None,
        **{k: v for k, v in current.items() if k != "watchlist"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
