"""
Simple browser-based configuration form for the trading bot.

Run this, then visit http://YOUR_SERVER_IP:5000 in any browser to view
and edit the bot's settings — no file editing required.

IMPORTANT — set a password before running this on a public server:
    export CONFIG_UI_PASSWORD="something only you know"
Without this set, the app refuses to start, since this form would
otherwise be reachable by anyone who finds your server's IP.

This does NOT restart main.py automatically — if the bot is already
running, stop and restart it after saving changes for them to apply
(main.py reads config.py fresh each time it starts).
"""

import json
import os

from flask import Flask, request, redirect, session, render_template_string

import config as cfg

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
<body style="font-family: sans-serif; max-width: 600px; margin: 40px auto;">
  <h2>Trading Bot Configuration</h2>
  {% if saved %}<p style="color: green;">Saved. Restart the bot for changes to take effect.</p>{% endif %}
  <form method="post">
    <label>Watchlist (comma-separated symbols)</label><br>
    <input type="text" name="watchlist" value="{{ watchlist }}" style="width: 100%; padding: 6px;"><br><br>

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
</body>
"""


def load_current():
    if os.path.exists(USER_CONFIG_PATH):
        with open(USER_CONFIG_PATH) as f:
            saved = json.load(f)
    else:
        saved = {}
    return {
        "watchlist": ", ".join(saved.get("watchlist", cfg.WATCHLIST)),
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
        data = {
            "watchlist": [s.strip().upper() for s in request.form["watchlist"].split(",") if s.strip()],
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
    return render_template_string(FORM_PAGE, saved=saved, **current)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
