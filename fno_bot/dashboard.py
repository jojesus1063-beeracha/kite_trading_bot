"""
Standalone read-only monitoring dashboard for the F&O opening scalper.

Deliberately a SEPARATE Flask process on a SEPARATE port from the
equity bot's dashboard (configure_app.py, port 5000). This file never
imports anything from the equity bot, never shares its Flask `app`
instance, and never writes to any fno_bot state -- it only reads the
same JSON/JSONL files the bot itself already produces
(fno_bot_status.json, fno_trade_history.jsonl, audit_logs/*.jsonl,
shadow_logs/*.jsonl). That keeps the isolation guarantee from the
architecture review intact: even if this dashboard crashes, hangs, or
someone edits it carelessly, the trading process is completely
unaffected, because it never talks to this process at all.

Run with:
    python3 -m fno_bot.dashboard
Then open http://<host>:5050/ (bind to 0.0.0.0 if viewing from another
device on the same network, same as the equity dashboard's pattern).

This is intentionally simple (server-rendered HTML, meta-refresh every
5s, no JS framework, no auth) -- it is a read-only status page for
personal monitoring during PAPER/SHADOW sessions, not a production
web app. Add a password gate before ever exposing it beyond localhost,
same caveat the equity dashboard already carries.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask

from fno_bot.reporting import trade_log
from fno_bot.audit.event_log import AUDIT_DIR
from fno_bot.audit.shadow_log import SHADOW_LOG_DIR

IST = ZoneInfo("Asia/Kolkata")

app = Flask(__name__)

REFRESH_SECONDS = 5


def _tail_jsonl(path: str, limit: int = 20) -> list:
    """Read the last `limit` JSON lines from a JSONL file. Returns []
    if the file doesn't exist or any line fails to parse -- a
    monitoring page must never crash on a partially-written line."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        return []
    records = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def _today_audit_events(limit: int = 30) -> list:
    path = os.path.join(AUDIT_DIR, f"events_{datetime.now(IST).date().isoformat()}.jsonl")
    return list(reversed(_tail_jsonl(path, limit)))


def _today_shadow_records(limit: int = 20) -> list:
    path = os.path.join(SHADOW_LOG_DIR, f"shadow_{datetime.now(IST).date().isoformat()}.jsonl")
    return list(reversed(_tail_jsonl(path, limit)))


def _status_badge(mode: str) -> str:
    colors = {"SHADOW": "#888", "PAPER": "#2b6cb0", "LIVE": "#c53030"}
    color = colors.get(mode, "#888")
    return f'<span style="background:{color};color:#fff;padding:2px 10px;border-radius:10px;font-size:0.85em;">{mode}</span>'


PAGE_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>F&O Opening Scalper -- Monitor</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }}
  h1 {{ font-size:1.3em; margin-bottom:4px; }}
  .sub {{ color:#9aa0a6; margin-bottom:20px; font-size:0.9em; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }}
  .card {{ background:#1a1d24; border:1px solid #2a2e37; border-radius:8px; padding:16px 20px; min-width:150px; }}
  .card .label {{ color:#9aa0a6; font-size:0.75em; text-transform:uppercase; letter-spacing:0.05em; }}
  .card .value {{ font-size:1.5em; margin-top:4px; }}
  .win {{ color:#48bb78; }}
  .loss {{ color:#f56565; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:28px; font-size:0.9em; }}
  th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #2a2e37; }}
  th {{ color:#9aa0a6; font-weight:normal; font-size:0.8em; text-transform:uppercase; }}
  section h2 {{ font-size:1em; color:#c0c4cc; border-bottom:1px solid #2a2e37; padding-bottom:6px; }}
  .empty {{ color:#666; font-style:italic; padding:10px 0; }}
  code {{ background:#1a1d24; padding:1px 6px; border-radius:4px; }}
</style>
</head>
<body>
  <h1>F&amp;O Opening Scalper &mdash; Monitor {mode_badge}</h1>
  <div class="sub">Read-only. Auto-refreshes every {refresh}s. Separate process from the equity dashboard (port 5000) and from the trading bot itself -- purely a viewer of its log files.</div>

  <div class="cards">
    <div class="card"><div class="label">State</div><div class="value">{state}</div></div>
    <div class="card"><div class="label">Underlying</div><div class="value">{underlying}</div></div>
    <div class="card"><div class="label">Status updated</div><div class="value" style="font-size:0.95em;">{status_updated}</div></div>
    <div class="card"><div class="label">Trades today</div><div class="value">{count}</div></div>
    <div class="card"><div class="label">Wins / Losses</div><div class="value"><span class="win">{wins}</span> / <span class="loss">{losses}</span></div></div>
    <div class="card"><div class="label">Net P&amp;L today</div><div class="value {pnl_class}">{net_pnl}</div></div>
  </div>

  <section>
    <h2>Recent closed trades</h2>
    {trades_table}
  </section>

  <section>
    <h2>Recent audit events</h2>
    {events_table}
  </section>

  <section>
    <h2>Shadow / counterfactual captures (no-trade evidence)</h2>
    {shadow_table}
  </section>

</body>
</html>
"""


def _render_trades_table(trades: list) -> str:
    if not trades:
        return '<div class="empty">No closed trades yet today.</div>'
    rows = []
    for t in reversed(trades[-15:]):
        result_class = "win" if t.get("result") == "WIN" else "loss"
        rows.append(
            f"<tr><td>{t.get('time','')}</td><td>{t.get('underlying','')}</td>"
            f"<td>{t.get('strike','')} {t.get('option_type','')}</td>"
            f"<td>{t.get('quantity','')}</td>"
            f"<td>{t.get('entry_price','')} &rarr; {t.get('exit_price','')}</td>"
            f"<td>{t.get('exit_reason','')}</td>"
            f"<td class='{result_class}'>{t.get('net_pnl','')}</td>"
            f"<td>{t.get('mode','')}</td></tr>"
        )
    return (
        "<table><tr><th>Time</th><th>Underlying</th><th>Contract</th><th>Qty</th>"
        "<th>Entry &rarr; Exit</th><th>Exit reason</th><th>Net P&amp;L</th><th>Mode</th></tr>"
        + "".join(rows) + "</table>"
    )


def _render_events_table(events: list) -> str:
    if not events:
        return '<div class="empty">No audit events logged yet today.</div>'
    rows = []
    for e in events:
        ts = e.get("timestamp_ist", "")[11:19] if e.get("timestamp_ist") else ""
        extra = {k: v for k, v in e.items() if k not in ("timestamp_ist", "event")}
        extra_str = ", ".join(f"{k}={v}" for k, v in list(extra.items())[:4])
        rows.append(f"<tr><td>{ts}</td><td><code>{e.get('event','')}</code></td><td>{extra_str}</td></tr>")
    return "<table><tr><th>Time</th><th>Event</th><th>Details</th></tr>" + "".join(rows) + "</table>"


def _render_shadow_table(records: list) -> str:
    if not records:
        return '<div class="empty">No shadow/counterfactual captures yet today.</div>'
    rows = []
    for r in records:
        ts = r.get("timestamp_ist", "")[11:19] if r.get("timestamp_ist") else ""
        rows.append(
            f"<tr><td>{ts}</td><td>CE ref {r.get('reference_ce_price','')}</td>"
            f"<td>CE MFE {r.get('ce_mfe_pct')}% / MAE {r.get('ce_mae_pct')}%</td>"
            f"<td>PE ref {r.get('reference_pe_price','')}</td>"
            f"<td>PE MFE {r.get('pe_mfe_pct')}% / MAE {r.get('pe_mae_pct')}%</td></tr>"
        )
    return "<table><tr><th>Time</th><th>CE ref</th><th>CE MFE/MAE</th><th>PE ref</th><th>PE MFE/MAE</th></tr>" + "".join(rows) + "</table>"


@app.route("/")
def index():
    status = trade_log.load_bot_status() or {}
    summary = trade_log.get_today_summary()
    trades = trade_log.get_trade_history(limit=50)
    events = _today_audit_events()
    shadow = _today_shadow_records()

    net_pnl = summary.get("total_net_pnl", 0) or 0
    pnl_class = "win" if net_pnl > 0 else ("loss" if net_pnl < 0 else "")

    html = PAGE_TEMPLATE.format(
        refresh=REFRESH_SECONDS,
        mode_badge=_status_badge(status.get("mode", "?")),
        state=status.get("state", "UNKNOWN"),
        underlying=status.get("underlying", "-"),
        status_updated=status.get("updated_at", "-") if status else "no status file yet -- bot not started",
        count=summary.get("count", 0),
        wins=summary.get("wins", 0),
        losses=summary.get("losses", 0),
        net_pnl=round(net_pnl, 2),
        pnl_class=pnl_class,
        trades_table=_render_trades_table(trades),
        events_table=_render_events_table(events),
        shadow_table=_render_shadow_table(shadow),
    )
    return html


@app.route("/api/status")
def api_status():
    """Small JSON endpoint in case you want to poll this from a
    script or from the equity dashboard's UI later, without ever
    importing fno_bot code into that process -- HTTP stays the only
    coupling point, which is what keeps the isolation guarantee."""
    status = trade_log.load_bot_status() or {}
    summary = trade_log.get_today_summary()
    return {"status": status, "today_summary": summary}


if __name__ == "__main__":
    # Port 5050, deliberately different from the equity dashboard's
    # 5000, so both can run at the same time without collision.
    app.run(host="0.0.0.0", port=5050, debug=False)
