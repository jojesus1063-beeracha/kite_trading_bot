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
from trade_log import get_trade_history, get_today_summary, load_bot_status
from pipeline_dashboard import load_pipeline_dashboard
from pipeline_dashboard_section import PIPELINE_FORM_CARD

import pyotp

TOTP_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "totp_config.json")


def get_available_balance():
    """
    Fetches real available cash from Zerodha via kite.margins(). Returns
    (balance, error) -- balance is None if the call fails for any reason
    (not authenticated, network issue, token expired, etc.), with error
    holding a short human-readable reason. Never raises -- this is a
    read-only display helper, not part of any trading decision path.
    """
    try:
        kite = get_kite_client()
        margins = kite.margins()
        equity = margins.get("equity", {})
        # "net" is Kite's true current available balance -- it correctly
        # includes today's intraday payins/payouts and P&L adjustments,
        # unlike "available.cash" which only reflects the day's OPENING
        # balance and misses same-day fund additions.
        net = equity.get("net")
        if net is None:
            return None, "Unexpected response format"
        return float(net), None
    except Exception as e:
        return None, str(e)


def _load_totp_config():
    if not os.path.exists(TOTP_CONFIG_PATH):
        return None
    try:
        with open(TOTP_CONFIG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _verify_2fa_code(code, totp_cfg):
    """
    Returns True if `code` is a valid current TOTP code OR an unused
    backup code (marking it used in that case). Backup-code usage is
    persisted to disk immediately so it can never be reused, even
    across restarts.
    """
    code = (code or "").strip()
    if not code:
        return False

    totp = pyotp.TOTP(totp_cfg["secret"])
    if totp.verify(code):
        return True

    backup_codes = totp_cfg.get("backup_codes", [])
    used_codes = totp_cfg.get("used_backup_codes", [])
    if code in backup_codes and code not in used_codes:
        used_codes.append(code)
        totp_cfg["used_backup_codes"] = used_codes
        with open(TOTP_CONFIG_PATH, "w") as f:
            json.dump(totp_cfg, f, indent=2)
        return True

    return False
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
    --accent: #22c55e;
    --accent-dark: #16a34a;
    --loss: #f87171;
    --warning: #fbbf24;
    --info: #60a5fa;
    --neutral: #9ca3af;
    --bg: #0b0e14;
    --bg-elevated: #11151d;
    --card: #151a24;
    --card-hover: #1a2030;
    --border: #242b3a;
    --border-subtle: #1c2230;
    --text: #e8eaed;
    --text-muted: #8b93a7;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 0;
    -webkit-font-smoothing: antialiased;
  }
  .topbar {
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
    color: var(--text);
    padding: 18px 28px;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 0.2px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .topbar::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent);
    display: inline-block;
    box-shadow: 0 0 8px var(--accent);
  }
  .container {
    max-width: 1180px;
    margin: 28px auto;
    padding: 0 16px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border-subtle);
    border-radius: 12px;
    padding: 22px 24px;
    margin-bottom: 20px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.2), 0 0 0 1px rgba(255,255,255,0.02) inset;
    animation: fadeIn 0.3s ease-out;
    transition: border-color 0.15s ease;
  }
  .card:hover { border-color: var(--border); }
  .card { content-visibility: auto; contain-intrinsic-size: auto 320px; }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .card h2 {
    margin-top: 0;
    margin-bottom: 4px;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 0.1px;
    color: var(--text);
  }
  label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--text-muted);
    margin: 16px 0 6px;
  }
  input[type=text], input[type=number], input[type=password] {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    background: var(--bg-elevated);
    color: var(--text);
    transition: border-color 0.15s ease;
  }
  input[type=text]::placeholder, input[type=number]::placeholder, input[type=password]::placeholder {
    color: var(--text-muted);
  }
  input:focus { outline: none; border-color: var(--accent); }
  .btn {
    background: var(--accent);
    color: #06120a;
    border: none;
    padding: 11px 26px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    margin-top: 20px;
    transition: background 0.15s ease, transform 0.1s ease;
  }
  .btn:hover { background: var(--accent-dark); transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
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
    background: var(--bg-elevated);
    transition: all 0.12s;
    user-select: none;
  }
  .chip-label:hover { border-color: var(--accent); color: var(--text); }
  .chip-input:checked + .chip-label {
    background: var(--accent);
    color: #06120a;
    border-color: var(--accent);
  }
  .exchange-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
  .exchange-table td { padding: 8px 4px; border-bottom: 1px solid var(--border-subtle); }
  .exchange-table .sym { font-weight: 700; }
  .radio-pair { display: flex; gap: 16px; }
  .radio-pair label { display: inline-flex; align-items: center; gap: 4px; font-weight: 500; text-transform: none; color: var(--text); margin: 0; font-size: 14px; }
  .radio-pair input { width: auto; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px;
  }
  .stock-card {
    border: 1px solid var(--border-subtle);
    border-radius: 10px;
    padding: 14px;
    background: var(--bg-elevated);
    transition: border-color 0.15s ease, transform 0.1s ease;
  }
  .stock-card:hover { border-color: var(--border); transform: translateY(-1px); }
  .stock-symbol { font-weight: 700; font-size: 14px; color: var(--text); }
  .stock-exchange { font-size: 11px; color: var(--text-muted); font-weight: 600; }
  .stock-price { font-size: 22px; font-weight: 700; margin: 4px 0; color: var(--text); }
  .pill {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 700;
  }
  .pill.up { background: rgba(34, 197, 94, 0.15); color: var(--accent); }
  .pill.down { background: rgba(248, 113, 113, 0.15); color: var(--loss); }
  .pill.warning { background: rgba(251, 191, 36, 0.15); color: var(--warning); }
  .pill.info { background: rgba(96, 165, 250, 0.15); color: var(--info); }
  .pill.neutral { background: rgba(156, 163, 175, 0.15); color: var(--neutral); }
  table.history { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.history th {
    text-align: left;
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.4px;
    padding: 10px 8px;
    border-bottom: 1px solid var(--border);
  }
  table.history td { padding: 10px 8px; border-bottom: 1px solid var(--border-subtle); }
  table.history tr:hover td { background: var(--card-hover); }
  .banner {
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.25);
    color: var(--loss);
    padding: 12px 16px;
    border-radius: 8px;
    font-size: 14px;
    margin-bottom: 16px;
  }
  .checkbox-row { display: flex; align-items: center; gap: 8px; margin-top: 20px; }
  .checkbox-row input { width: auto; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
  }
  .status-dot.live { background: var(--accent); box-shadow: 0 0 6px var(--accent); animation: pulse 2s infinite; }
  .status-dot.stopped { background: var(--loss); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
  @media (max-width: 700px) {
    .topbar { padding: 14px 12px; align-items: flex-start !important; flex-direction: column; }
    .topbar > div { width: 100%; overflow-x: auto; }
    .container { margin: 16px auto; padding: 0 10px; }
    .card { padding: 16px 14px; border-radius: 10px; }
    .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    table { display: block; max-width: 100%; overflow-x: auto; }
  }
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

TWO_FACTOR_PAGE = BASE_STYLE + """
<!doctype html>
<title>Verify - Trading Bot</title>
<body>
  <div class="topbar">Trading Bot</div>
  <div class="container" style="max-width: 380px;">
    <div class="card">
      <h2>Enter verification code</h2>
      <p style="color: var(--text-muted); font-size: 13px;">
        Open your authenticator app and enter the 6-digit code, or use a backup code.
      </p>
      {% if error %}<p style="color: var(--loss);">{{ error }}</p>{% endif %}
      <form method="post">
        <input type="text" name="code" placeholder="6-digit code or backup code" autocomplete="off" autofocus>
        <button type="submit" class="btn" style="width:100%;">Verify</button>
      </form>
      <p style="margin-top:16px;"><a href="/login">Back to login</a></p>
    </div>
  </div>
</body>
"""

FORM_PAGE = BASE_STYLE + """
<!doctype html>
<title>Trading Bot Dashboard</title>

<script defer src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<body>
  <div class="topbar" style="display: flex; justify-content: space-between; align-items: center;">
    <span>Trading Bot Dashboard</span>
    <div style="display: flex; gap: 10px; align-items: center;">
      <a href="/monitor" style="font-size: 13px; font-weight: 600; background: var(--accent); color: #fff;
                   border-radius: 8px; padding: 6px 14px; text-decoration: none;">
        Live Monitor
      </a>
      <span style="font-size: 13px; font-weight: 600; background: var(--card); border: 1px solid var(--border);
                   border-radius: 8px; padding: 6px 14px; color: var(--text-muted);">
        {% if available_balance is not none %}
          Available: <span style="color: var(--accent);">Rs {{ "{:,.2f}".format(available_balance) }}</span>
        {% else %}
          Balance unavailable
        {% endif %}
      </span>
    </div>
  </div>
  <div class="container">

    {% if saved %}<div class="banner" style="background:#e3f9f0; color: var(--accent);">Saved — restart the bot for changes to take effect.</div>{% endif %}

""" + PIPELINE_FORM_CARD + """

    <div class="card">
      <h2>Trade History</h2>
      <p>
        Today: {{ today_summary.count }} trade(s), total P&amp;L:
        <span class="pill {{ 'up' if today_summary.total_pnl >= 0 else 'down' }}">
          ₹{{ "%.2f"|format(today_summary.total_pnl) }}
        </span>
      </p>

      <form method="get" style="margin: 16px 0;">
        <label>View trades for date</label>
        <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
          <select name="trade_date" onchange="this.form.submit()" style="width: auto; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: #fafbfc;">
            {% for d in trade_dates_available %}
              <option value="{{ d }}" {{ 'selected' if d == selected_trade_date else '' }}>
                {{ d }}{{ ' (today)' if d == today_summary.get('date_str', '') else '' }}
              </option>
            {% endfor %}
          </select>
          <span style="color: var(--text-muted); font-size: 13px;">
            {{ selected_day_summary.count }} trade(s), total P&amp;L:
            <span class="pill {{ 'up' if selected_day_summary.total_pnl >= 0 else 'down' }}">
              ₹{{ "%.2f"|format(selected_day_summary.total_pnl) }}
            </span>
          </span>
        </div>
      </form>

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
        <p style="color: var(--text-muted);">No trades recorded on {{ selected_trade_date }}.</p>
      {% endif %}
    </div>

    <div class="card">
      <h2>Live Bot Activity</h2>
      {% if bot_status %}
        <p style="font-size:12px; color:var(--text-muted); margin-bottom: 14px;">Last updated: {{ bot_status.updated }}</p>

        <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px;">
          <span class="pill up">{{ bot_status_counts.entry }} signal{{ 's' if bot_status_counts.entry != 1 else '' }}</span>
          <span class="pill info">{{ bot_status_counts.position }} open</span>
          <span class="pill warning">{{ bot_status_counts.attention }} need attention</span>
          <span class="pill neutral">{{ bot_status_counts.routine }} routine</span>
        </div>

        {% if bot_status_notable %}
          <table class="history">
            <tr><th>Symbol</th><th>Status</th></tr>
            {% for s in bot_status_notable %}
            <tr>
              <td style="font-family: 'SF Mono', Consolas, monospace; font-weight: 700;">{{ s.symbol }}</td>
              <td><span class="pill {{ s.pill_class }}">{{ s.status }}</span></td>
            </tr>
            {% endfor %}
          </table>
        {% else %}
          <p style="color: var(--text-muted); font-size: 13px;">No signals, open positions, or issues right now.</p>
        {% endif %}

        {% if bot_status_routine %}
          <button type="button" onclick="toggleRoutine()" id="routine-toggle"
                  style="background: transparent; border: 1px solid var(--border); color: var(--text-muted);
                         padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 600;
                         cursor: pointer; margin-top: 14px;">
            Show {{ bot_status_counts.routine }} routine symbol{{ 's' if bot_status_counts.routine != 1 else '' }}
          </button>
          <div id="routine-table" style="display: none; margin-top: 10px;">
            <table class="history">
              <tr><th>Symbol</th><th>Status</th></tr>
              {% for s in bot_status_routine %}
              <tr>
                <td style="font-family: 'SF Mono', Consolas, monospace; font-weight: 700;">{{ s.symbol }}</td>
                <td><span class="pill {{ s.pill_class }}">{{ s.status }}</span></td>
              </tr>
              {% endfor %}
            </table>
          </div>
        {% endif %}
      {% else %}
        <p>No activity data yet. This appears once the bot completes its first cycle.</p>
      {% endif %}
    </div>

    <div class="card">
      <h2>Watchlist &amp; Settings</h2>
      <form method="post" id="watchlist-form">
        <label>Add another symbol not listed above (comma-separated)</label>
        <div style="display: flex; gap: 12px; align-items: center;">
          <input type="text" name="extra_symbols" placeholder="e.g. IRCTC, ZOMATO" style="flex: 1;">
          <div class="radio-pair">
            <label><input type="radio" name="extra_exchange" value="NSE" checked> NSE</label>
            <label><input type="radio" name="extra_exchange" value="BSE"> BSE</label>
          </div>
        </div>

        <div style="display: flex; justify-content: space-between; align-items: center;">
          <label style="margin: 16px 0 6px;">Stocks to trade (tap to select)</label>
          <button type="button" onclick="clearAllChips()"
                  style="background: transparent; border: 1px solid var(--border); color: var(--text-muted);
                         padding: 5px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer;">
            Remove all
          </button>
        </div>
        <div class="chip-grid">
          {% for sym in selected_chips %}
            <input type="checkbox" id="chip-{{ sym }}" name="watchlist" value="{{ sym }}"
                   class="chip-input" checked>
            <label for="chip-{{ sym }}" class="chip-label">{{ sym }}</label>
          {% endfor %}
        </div>

        {% if other_chips %}
          <button type="button" onclick="toggleOtherChips()" id="other-chips-toggle"
                  style="background: transparent; border: 1px solid var(--border); color: var(--text-muted);
                         padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 600;
                         cursor: pointer; margin-top: 10px;">
            Show {{ other_chips|length }} more available stock{{ 's' if other_chips|length != 1 else '' }}
          </button>
          <div id="other-chips-grid" class="chip-grid" style="display: none; margin-top: 10px;">
            {% for sym in other_chips %}
              <input type="checkbox" id="chip-{{ sym }}" name="watchlist" value="{{ sym }}" class="chip-input">
              <label for="chip-{{ sym }}" class="chip-label">{{ sym }}</label>
            {% endfor %}
          </div>
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
            <label>Fixed stop-loss (%)</label>
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
          <div>
            <label>Fixed profit target (%)</label>
            <input type="number" step="0.1" name="profit_target_percent" value="{{ profit_target_percent }}">
          </div>
        </div>

        <div class="checkbox-row">
          <input type="checkbox" name="use_adx_filter" id="use_adx_filter" {{ 'checked' if use_adx_filter else '' }}>
          <label for="use_adx_filter" style="margin:0;">
            Require ADX trend-strength confirmation (filters out choppy false trends — see backtest before enabling live)
          </label>
        </div>

        <div class="checkbox-row">
          <input type="checkbox" name="enable_fixed_target" id="enable_fixed_target" {{ 'checked' if enable_fixed_target else '' }}>
          <label for="enable_fixed_target" style="margin:0;">
            Fixed profit target mode (exits at the target % above instead of trailing — bypasses trailing stop, structure break, and trend reversal while on)
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
        <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px;">
          <span class="pill up">{{ dashboard_counts.up }} up</span>
          <span class="pill down">{{ dashboard_counts.down }} down</span>
          {% if dashboard_counts.error %}<span class="pill warning">{{ dashboard_counts.error }} error{{ 's' if dashboard_counts.error != 1 else '' }}</span>{% endif %}
          <span class="pill neutral">{{ dashboard_counts.flat }} flat (&lt;{{ "%.0f"|format(1.0) }}%)</span>
        </div>

        {% if dashboard_notable %}
          <div class="grid">
            {% for stock in dashboard_notable %}
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
        {% else %}
          <p style="color: var(--text-muted); font-size: 13px;">No significant movers right now.</p>
        {% endif %}

        {% if dashboard_routine %}
          <button type="button" onclick="toggleDashboardRoutine()" id="dashboard-routine-toggle"
                  style="background: transparent; border: 1px solid var(--border); color: var(--text-muted);
                         padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 600;
                         cursor: pointer; margin-top: 14px;">
            Show {{ dashboard_routine|length }} flat stock{{ 's' if dashboard_routine|length != 1 else '' }}
          </button>
          <div id="dashboard-routine-grid" class="grid" style="display: none; margin-top: 10px;">
            {% for stock in dashboard_routine %}
              <div class="stock-card">
                <div class="stock-symbol">{{ stock.symbol }}</div>
                <div class="stock-exchange">{{ stock.exchange }}</div>
                <div class="stock-price">₹{{ "%.2f"|format(stock.ltp) }}</div>
                <span class="pill {{ 'up' if stock.change_pct >= 0 else 'down' }}">
                  {{ "%.2f"|format(stock.change_pct) }}%
                </span>
                <canvas id="chart-{{ stock.symbol }}-{{ stock.exchange }}" width="150" height="50" style="margin-top:8px;"></canvas>
              </div>
            {% endfor %}
          </div>
        {% endif %}

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
          <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px;">
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
                <p>Profit factor: {{ "%.2f"|format(r.profit_factor) if r.profit_factor is not none else "N/A" }}</p>
                <p>Avg winner: {{ "₹%.2f"|format(r.avg_winner) if r.avg_winner is not none else "N/A" }}</p>
                <p>Avg loser: {{ "₹%.2f"|format(r.avg_loser) if r.avg_loser is not none else "N/A" }}</p>
                <p>Expectancy/trade: ₹{{ "%.2f"|format(r.expectancy) }}</p>
                <p>Max drawdown: ₹{{ "%.2f"|format(r.max_drawdown) }}</p>
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
                <p>Profit factor: {{ "%.2f"|format(r.profit_factor) if r.profit_factor is not none else "N/A" }}</p>
                <p>Avg winner: {{ "₹%.2f"|format(r.avg_winner) if r.avg_winner is not none else "N/A" }}</p>
                <p>Avg loser: {{ "₹%.2f"|format(r.avg_loser) if r.avg_loser is not none else "N/A" }}</p>
                <p>Expectancy/trade: ₹{{ "%.2f"|format(r.expectancy) }}</p>
                <p>Max drawdown: ₹{{ "%.2f"|format(r.max_drawdown) }}</p>
              {% endif %}
            </div>
            <div class="stock-card">
              <div class="stock-symbol">ADX Dynamic</div>
              {% set r = backtest_result.dynamic %}
              {% if r.total_trades == 0 %}
                <p style="color: var(--text-muted); font-size: 13px;">No trades in this period.</p>
              {% else %}
                <p>Trades: {{ r.total_trades }}</p>
                <p>Win rate: {{ "%.1f"|format(r.win_rate) }}%</p>
                <p>Total P&amp;L: <span class="pill {{ 'up' if r.total_pnl >= 0 else 'down' }}">₹{{ "%.2f"|format(r.total_pnl) }}</span></p>
                <p>Avg P&amp;L/trade: ₹{{ "%.2f"|format(r.avg_pnl) }}</p>
                <p>Profit factor: {{ "%.2f"|format(r.profit_factor) if r.profit_factor is not none else "N/A" }}</p>
                <p>Avg winner: {{ "₹%.2f"|format(r.avg_winner) if r.avg_winner is not none else "N/A" }}</p>
                <p>Avg loser: {{ "₹%.2f"|format(r.avg_loser) if r.avg_loser is not none else "N/A" }}</p>
                <p>Expectancy/trade: ₹{{ "%.2f"|format(r.expectancy) }}</p>
                <p>Max drawdown: ₹{{ "%.2f"|format(r.max_drawdown) }}</p>
              {% endif %}
            </div>
          </div>
        {% endif %}
      {% endif %}
    </div>

    <p><a href="/logout">Log out</a></p>
  </div>

  <script>
    function clearAllChips() {
      document.querySelectorAll('.chip-input').forEach(function(el) { el.checked = false; });
    }
    function toggleRoutine() {
      var table = document.getElementById('routine-table');
      var btn = document.getElementById('routine-toggle');
      var showing = table.style.display !== 'none';
      table.style.display = showing ? 'none' : 'block';
      btn.textContent = showing ? btn.textContent.replace('Hide', 'Show') : btn.textContent.replace('Show', 'Hide');
    }
    function toggleOtherChips() {
      var grid = document.getElementById('other-chips-grid');
      var btn = document.getElementById('other-chips-toggle');
      var showing = grid.style.display !== 'none';
      grid.style.display = showing ? 'none' : 'flex';
      btn.textContent = showing ? btn.textContent.replace('Hide', 'Show') : btn.textContent.replace('Show', 'Hide');
    }
    function toggleDashboardRoutine() {
      var grid = document.getElementById('dashboard-routine-grid');
      var btn = document.getElementById('dashboard-routine-toggle');
      var showing = grid.style.display !== 'none';
      grid.style.display = showing ? 'none' : 'grid';
      btn.textContent = showing ? btn.textContent.replace('Hide', 'Show') : btn.textContent.replace('Show', 'Hide');
    }
    const sparkData = {{ spark_json|safe }};
    function renderSparklines() {
      if (typeof Chart === "undefined") return;
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
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", renderSparklines);
    else renderSparklines();
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
        "sl_buffer_pct": saved.get("sl_buffer_pct", getattr(cfg, "STOP_LOSS_PERCENT", 0.45)),
        "risk_reward_min": saved.get("risk_reward_min", cfg.RISK_REWARD_MIN),
        "max_trades_per_day": saved.get("max_trades_per_day", cfg.MAX_TRADES_PER_DAY),
        "max_daily_loss_pct": saved.get("max_daily_loss_pct", cfg.MAX_DAILY_LOSS_PCT),
        "trend_ema_fast": saved.get("trend_ema_fast", cfg.TREND_EMA_FAST),
        "trend_ema_slow": saved.get("trend_ema_slow", cfg.TREND_EMA_SLOW),
        "entry_ema": saved.get("entry_ema", cfg.ENTRY_EMA),
        "paper_trading": saved.get("paper_trading", cfg.PAPER_TRADING),
        "use_adx_filter": saved.get("use_adx_filter", cfg.USE_ADX_FILTER),
        "adx_threshold": saved.get("adx_threshold", cfg.ADX_THRESHOLD),
        "profit_target_percent": saved.get("profit_target_percent", cfg.PROFIT_TARGET_PERCENT),
        "enable_fixed_target": saved.get("enable_fixed_target", cfg.ENABLE_FIXED_TARGET),
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
    return session.get("logged_in") is True  # password_verified alone is NOT enough


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            totp_cfg = _load_totp_config()
            if totp_cfg is None:
                # No 2FA enrolled -- fail open to password-only so a
                # missing/deleted totp_config.json can never lock
                # someone out entirely.
                session["logged_in"] = True
                return redirect("/")
            session["password_verified"] = True
            return redirect("/verify-2fa")
        error = "Wrong password."
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    if not session.get("password_verified"):
        return redirect("/login")

    error = None
    if request.method == "POST":
        totp_cfg = _load_totp_config()
        if totp_cfg is None:
            # Config vanished between login and this step -- fail open
            # rather than strand a correctly-password-authenticated user.
            session.pop("password_verified", None)
            session["logged_in"] = True
            return redirect("/")

        if _verify_2fa_code(request.form.get("code"), totp_cfg):
            session.pop("password_verified", None)
            session["logged_in"] = True
            return redirect("/")
        error = "Invalid code. Try again."

    return render_template_string(TWO_FACTOR_PAGE, error=error)


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
        "profit_factor": result.get("profit_factor"),
        "avg_winner": result.get("avg_winner"),
        "avg_loser": result.get("avg_loser"),
        "expectancy": result.get("expectancy"),
        "max_drawdown": result.get("max_drawdown"),
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
        # Three-way ADX comparison: off / binary / dynamic. Uses the new
        # additive cfg.ADX_MODE (see adx_confidence.py) so this exercises
        # the same code path as live trading in all three modes.
        original_use_adx = cfg.USE_ADX_FILTER
        original_adx_mode = getattr(cfg, "ADX_MODE", "")
        try:
            cfg.ADX_MODE = "off"
            off_result = run_backtest_data(symbol, from_date, to_date, exchange)

            cfg.ADX_MODE = "binary"
            binary_result = run_backtest_data(symbol, from_date, to_date, exchange)

            cfg.ADX_MODE = "dynamic"
            dynamic_result = run_backtest_data(symbol, from_date, to_date, exchange)

            result["off"] = _summarize_backtest(off_result)
            result["on"] = _summarize_backtest(binary_result)       # kept as "on" -- existing template key
            result["dynamic"] = _summarize_backtest(dynamic_result)  # new
            result["error"] = None
        except Exception as e:
            result["error"] = str(e)
        finally:
            cfg.USE_ADX_FILTER = original_use_adx
            cfg.ADX_MODE = original_adx_mode

    session["backtest_result"] = result
    return redirect("/")


from monitor_route import MONITOR_PAGE
from trade_log import load_bot_status
from watchlist_range_analytics import load_watchlist_snapshot
from watchlist_dashboard_helpers import compute_summary_cards, classify_report_freshness
import json as _json
from dashboard_bot_reload import apply_saved_config


@app.route("/api/monitor-data")
def api_monitor_data():
    """Read-only JSON endpoint for the /monitor page's client-side
    polling. Makes ZERO Kite API calls -- reads only generated status,
    selector and telemetry files plus read-only systemd state."""
    if not require_login():
        return {"error": "not authenticated"}, 401
    status = load_bot_status() or {}
    session_data = status.get("session_summary", {})
    watchlist_snapshot = load_watchlist_snapshot()
    freshness = classify_report_freshness(watchlist_snapshot)
    summary_cards = compute_summary_cards(watchlist_snapshot)
    return {
        "updated": status.get("updated", "N/A"),
        "positions": status.get("positions", []),
        "portfolio": status.get("portfolio_summary", {}),
        "session": session_data,
        "health": status.get("health", {}),
        "watchlist_snapshot": watchlist_snapshot,
        "freshness": freshness,
        "summary_cards": summary_cards,
        "watchlist_symbols": watchlist_snapshot.get("symbols", []) if watchlist_snapshot else [],
        "pipeline": load_pipeline_dashboard(),
    }


@app.route("/monitor")
@app.route("/Monitor")
def monitor():
    if not require_login():
        return redirect("/login")
    status = load_bot_status() or {}
    session_data = status.get("session_summary", {})
    pf = session_data.get("profit_factor")
    if pf is None:
        pf_display = "N/A"
    elif pf == float("inf"):
        pf_display = "inf"
    else:
        pf_display = f"{pf:.2f}"

    watchlist_snapshot = load_watchlist_snapshot()
    freshness = classify_report_freshness(watchlist_snapshot)
    summary_cards = compute_summary_cards(watchlist_snapshot)
    watchlist_symbols_json = _json.dumps(watchlist_snapshot.get("symbols", []) if watchlist_snapshot else [])

    return render_template_string(
        MONITOR_PAGE,
        updated=status.get("updated", "N/A"),
        positions=status.get("positions", []),
        portfolio=status.get("portfolio_summary", {}),
        session=session_data,
        health=status.get("health", {}),
        profit_factor_display=pf_display,
        watchlist_snapshot=watchlist_snapshot,
        freshness=freshness,
        summary_cards=summary_cards,
        watchlist_symbols_json=watchlist_symbols_json,
        pipeline=load_pipeline_dashboard(),
    )


@app.route("/", methods=["GET", "POST"])
def index():
    if not require_login():
        return redirect("/login")

    saved = False
    if request.method == "POST":
        selected = request.form.getlist("watchlist")
        extra = [s.strip().upper() for s in request.form.get("extra_symbols", "").split(",") if s.strip()]
        extra_exchange = request.form.get("extra_exchange", "NSE")
        extra_set = set(extra)
        # Preserve each symbol's PREVIOUSLY-SAVED exchange for anything
        # not freshly typed into extra_symbols this save -- otherwise
        # every resave silently reverts existing BSE symbols to NSE.
        _prior_watchlist = load_current().get("watchlist", [])
        _prior_exchange_map = {w["symbol"]: w["exchange"] for w in _prior_watchlist}
        all_symbols = list(dict.fromkeys(selected + extra))

        watchlist = [
            {"symbol": s, "exchange": extra_exchange if s in extra_set else _prior_exchange_map.get(s, "NSE")}
            for s in all_symbols
        ]

        # Load existing settings first and MERGE the form's fields into
        # them, rather than replacing the whole file -- otherwise any
        # setting added outside this form (via script/dashboard-override
        # additions like MAX_POSITION_SIZE_PCT, ENABLE_CANDLE_ALIGNED_POLLING,
        # ADX_MODE, etc.) gets silently wiped out on every save, since this
        # form only has inputs for a subset of all available settings.
        if os.path.exists(USER_CONFIG_PATH):
            with open(USER_CONFIG_PATH) as f:
                data = json.load(f)
        else:
            data = {}

        data.update({
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
            "profit_target_percent": float(request.form["profit_target_percent"]),
            "enable_fixed_target": "enable_fixed_target" in request.form,
        })
        with open(USER_CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
        saved = True
        # DASHBOARD_AUTO_APPLY_CONFIG
        apply_saved_config()

    current = load_current()
    selected_symbols = [w["symbol"] for w in current["watchlist"]]
    exchange_map = {w["symbol"]: w["exchange"] for w in current["watchlist"]}
    universe = sorted(set(STOCK_UNIVERSE) | set(selected_symbols), key=lambda s: (s not in selected_symbols, s))
    selected_chips = [s for s in universe if s in selected_symbols]
    other_chips = [s for s in universe if s not in selected_symbols]

    dashboard_data, dashboard_error = get_dashboard_data(current["watchlist"])
    dashboard_notable = []
    dashboard_routine = []
    dashboard_counts = {"up": 0, "down": 0, "error": 0, "flat": 0}
    MOVER_THRESHOLD_PCT = 1.0
    if dashboard_data:
        for stock in dashboard_data:
            if stock.get("error"):
                dashboard_notable.append(stock)
                dashboard_counts["error"] += 1
                continue
            change = stock.get("change_pct", 0)
            if change >= 0:
                dashboard_counts["up"] += 1
            else:
                dashboard_counts["down"] += 1
            if abs(change) >= MOVER_THRESHOLD_PCT:
                dashboard_notable.append(stock)
            else:
                dashboard_routine.append(stock)
                dashboard_counts["flat"] += 1
        # Biggest movers first within the notable group
        dashboard_notable.sort(key=lambda s: (not s.get("error"), -abs(s.get("change_pct", 0))))
        dashboard_routine.sort(key=lambda s: s.get("symbol", ""))
    spark_json = json.dumps({
        f"{d['symbol']}-{d['exchange']}": d["spark"] for d in (dashboard_data or []) if not d.get("error")
    })

    from datetime import datetime as _dt
    selected_trade_date = request.args.get("trade_date") or _dt.now().strftime("%Y-%m-%d")
    all_trade_history = get_trade_history(limit=5000)
    trade_history = [t for t in all_trade_history if t.get("date") == selected_trade_date]
    trade_dates_available = sorted({t["date"] for t in all_trade_history}, reverse=True)
    if selected_trade_date not in trade_dates_available:
        trade_dates_available = [selected_trade_date] + trade_dates_available
    selected_day_summary = {
        "count": len(trade_history),
        "total_pnl": sum(t["pnl"] for t in trade_history),
    }
    today_summary = get_today_summary()
    available_balance, balance_error = get_available_balance()
    bot_status = load_bot_status()
    bot_status_notable = []
    bot_status_routine = []
    bot_status_counts = {"entry": 0, "position": 0, "attention": 0, "routine": 0}
    if bot_status and bot_status.get("symbols"):
        def _categorize(status):
            """Returns (pill_class, is_notable) for a status string.
            Purely a display classification -- bot_status.json and
            main.py's writer are completely untouched."""
            if status.startswith("ENTRY"):
                pill = "up" if "BUY" in status else "down"
                return pill, True
            if status.startswith("position open") or status.startswith("CLOSED"):
                return "info", True
            if status in ("risk limit reached", "signal found, order failed", "no candle data"):
                return "warning", True
            return "neutral", False  # no signal, outside trading window, etc.

        symbols_with_pill = []
        for s in bot_status["symbols"]:
            pill_class, is_notable = _categorize(s.get("status", ""))
            entry = dict(s, pill_class=pill_class)
            symbols_with_pill.append(entry)
            if is_notable:
                bot_status_notable.append(entry)
                if entry["status"].startswith("ENTRY"):
                    bot_status_counts["entry"] += 1
                elif entry["status"].startswith("position open") or entry["status"].startswith("CLOSED"):
                    bot_status_counts["position"] += 1
                else:
                    bot_status_counts["attention"] += 1
            else:
                bot_status_routine.append(entry)
                bot_status_counts["routine"] += 1

        # ENTRY signals first within the notable group, alphabetical
        # within each subgroup -- same ordering intent as before.
        bot_status_notable.sort(key=lambda s: (not s["status"].startswith("ENTRY"), s["symbol"]))
        bot_status_routine.sort(key=lambda s: s["symbol"])

        bot_status = dict(bot_status)
        bot_status["symbols"] = symbols_with_pill
    backtest_result = session.pop("backtest_result", None)

    return render_template_string(
        FORM_PAGE,
        saved=saved,
        stock_universe=universe,
        selected_symbols=selected_symbols,
        exchange_map=exchange_map,
        dashboard_data=dashboard_data,
        dashboard_error=dashboard_error,
        dashboard_notable=dashboard_notable,
        dashboard_routine=dashboard_routine,
        dashboard_counts=dashboard_counts,
        selected_chips=selected_chips,
        other_chips=other_chips,
        spark_json=spark_json,
        trade_history=trade_history,
        today_summary=today_summary,
        selected_trade_date=selected_trade_date,
        trade_dates_available=trade_dates_available,
        selected_day_summary=selected_day_summary,
        bot_status=bot_status,
        available_balance=available_balance,
        balance_error=balance_error,
        bot_status_notable=bot_status_notable,
        bot_status_routine=bot_status_routine,
        bot_status_counts=bot_status_counts,
        backtest_result=backtest_result,
        pipeline=load_pipeline_dashboard(),
        bt_symbol=backtest_result["symbol"] if backtest_result else None,
        bt_from_date=backtest_result["from_date"] if backtest_result else None,
        bt_to_date=backtest_result["to_date"] if backtest_result else None,
        **{k: v for k, v in current.items() if k != "watchlist"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
