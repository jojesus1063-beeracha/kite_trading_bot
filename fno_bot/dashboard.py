"""Read-only F&O activity dashboard.

Set ``FNO_DASHBOARD_RUNTIME_ROOT`` to the active bot worktree when the
dashboard runs from a different checkout. This process never calls Kite and
never writes trading state.
"""
import json
import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify

from fno_bot.strategies.session_router import route_session

IST = ZoneInfo("Asia/Kolkata")
app = Flask(__name__)


def _runtime_paths(root=None):
    root = root or os.environ.get("FNO_DASHBOARD_RUNTIME_ROOT")
    if not root:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot = os.path.join(os.path.abspath(root), "fno_bot")
    today = datetime.now(IST).date().isoformat()
    return {
        "root": os.path.abspath(root),
        "audit": os.path.join(bot, "audit_logs", f"events_{today}.jsonl"),
        "trades": os.path.join(bot, "fno_trade_history.jsonl"),
        "status": os.path.join(bot, "fno_bot_status.json"),
        "positions": os.path.join(bot, "fno_open_positions.json"),
    }


def _tail_jsonl(path, limit=5000):
    if not os.path.exists(path):
        return []
    try:
        with open(path) as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


def _load_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _event_time(record):
    value = record.get("timestamp_ist")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None


def summarize_activity(events, trades, status, positions, now=None):
    now = now or datetime.now(IST)
    today = now.date().isoformat()
    trades_today = [row for row in trades if row.get("date") == today]
    wins = sum(1 for row in trades_today if (row.get("net_pnl") or 0) > 0)
    net_pnl = sum(float(row.get("net_pnl") or 0) for row in trades_today)
    costs = sum(float(row.get("costs") or 0) for row in trades_today)

    evaluations = [row for row in events if row.get("event") == "PROFESSIONAL_SIGNAL_EVALUATED"]
    latest_by_symbol = {}
    rejection_counts = Counter()
    eligible = 0
    for row in evaluations:
        symbol = row.get("symbol") or "?"
        latest_by_symbol[symbol] = row
        if row.get("direction"):
            eligible += 1
        else:
            rejection_counts[row.get("reason") or "unspecified"] += 1

    timed_events = [(_event_time(row), row) for row in events]
    latest_event_at = next((stamp for stamp, _ in reversed(timed_events) if stamp), None)
    event_age = (now - latest_event_at).total_seconds() if latest_event_at else None
    socket_events = [row.get("event") for row in events if row.get("event") in {
        "WEBSOCKET_READY", "WEBSOCKET_CLOSED", "WEBSOCKET_ERROR"
    }]
    socket_state = "CONNECTED" if socket_events and socket_events[-1] == "WEBSOCKET_READY" else (
        "DISCONNECTED" if socket_events else "UNKNOWN"
    )

    candidates = []
    for symbol, row in latest_by_symbol.items():
        metrics = row.get("metrics") or {}
        candidates.append({
            "symbol": symbol, "direction": row.get("direction"),
            "confidence": row.get("confidence"), "reason": row.get("reason"),
            "spread_pct": row.get("max_spread_pct"),
            "spot_roc_pct": metrics.get("underlying_roc_pct"),
            "ce_roc_pct": metrics.get("ce_roc_pct"), "pe_roc_pct": metrics.get("pe_roc_pct"),
            "volume_delta": metrics.get("selected_volume_delta"), "oi": metrics.get("selected_oi"),
            "time": (row.get("timestamp_ist") or "")[11:19],
        })
    candidates.sort(key=lambda row: (row["direction"] is None, -(row["confidence"] or 0), row["symbol"]))

    recent_events = [{
        "time": (row.get("timestamp_ist") or "")[11:19], "event": row.get("event"),
        "symbol": row.get("symbol"),
        "message": row.get("reason") or row.get("exit_reason") or row.get("direction") or "",
    } for row in reversed(events[-60:])]

    return {
        "generated_at": now.isoformat(), "session": route_session(now).value,
        "mode": status.get("mode", "PAPER"), "bot_state": status.get("state", "UNKNOWN"),
        "socket_state": socket_state,
        "last_event_age_seconds": round(event_age, 1) if event_age is not None else None,
        "scanned_symbols": len(latest_by_symbol), "evaluations": len(evaluations),
        "eligible_evaluations": eligible,
        "open_positions": list((positions.get("positions") or {}).values()),
        "summary": {"trades": len(trades_today), "wins": wins, "losses": len(trades_today) - wins,
                    "win_rate_pct": round(wins / len(trades_today) * 100, 1) if trades_today else None,
                    "net_pnl": round(net_pnl, 2), "costs": round(costs, 2)},
        "candidates": candidates[:50],
        "rejections": [{"reason": reason, "count": count} for reason, count in rejection_counts.most_common(12)],
        "trades": list(reversed(trades_today[-25:])), "events": recent_events,
    }


def current_activity(root=None):
    paths = _runtime_paths(root)
    activity = summarize_activity(
        _tail_jsonl(paths["audit"]), _tail_jsonl(paths["trades"], 10000),
        _load_json(paths["status"], {}), _load_json(paths["positions"], {}),
    )
    activity["runtime_root"] = paths["root"]
    return activity


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Options Command Center</title>
<style>:root{--bg:#071018;--panel:#0d1923;--line:#20313d;--text:#e7f0f4;--muted:#8296a3;--cyan:#38d6c7;--green:#42d392;--red:#ff6b7a;--amber:#f4bd61}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% -10%,#123541 0,transparent 35%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}.shell{max-width:1500px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:20px}.eyebrow{color:var(--cyan);font-size:12px;letter-spacing:.15em;text-transform:uppercase;font-weight:700}h1{margin:5px 0;font-size:28px}.muted{color:var(--muted);font-size:13px}.pills{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.pill{border:1px solid var(--line);background:#0b151e;padding:7px 11px;border-radius:999px;font-size:12px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.grid{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:12px}.card,.panel{background:linear-gradient(145deg,rgba(17,34,45,.95),rgba(10,22,31,.95));border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 35px rgba(0,0,0,.18)}.card{padding:15px}.label{text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-size:10px}.value{font-size:24px;font-weight:700;margin-top:7px}.layout{display:grid;grid-template-columns:1.5fr 1fr;gap:14px;margin-top:14px}.panel{padding:16px;overflow:hidden}.panel h2{font-size:14px;margin:0 0 12px}.scroll{overflow:auto;max-height:440px}table{width:100%;border-collapse:collapse;font-size:12px}th{position:sticky;top:0;background:#0e1b25;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-size:9px}th,td{text-align:left;padding:9px;border-bottom:1px solid #192a35;white-space:nowrap}.reason{white-space:normal;max-width:350px}.barrow{display:grid;grid-template-columns:minmax(130px,1fr) 3fr 42px;gap:8px;align-items:center;margin:9px 0;font-size:11px}.bar{height:8px;background:#142630;border-radius:9px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),#5987ff)}.empty{color:var(--muted);padding:25px;text-align:center}.footer{margin-top:14px;color:var(--muted);font-size:11px}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.top{flex-direction:column}.pills{justify-content:flex-start}}</style></head>
<body><main class="shell"><header class="top"><div><div class="eyebrow">Zerodha Live Data · Paper Only</div><h1>Options Command Center</h1><div class="muted">Opening momentum + completed-candle intraday monitoring. Read-only; no order controls.</div></div><div class="pills"><span class="pill" id="mode">—</span><span class="pill" id="session">—</span><span class="pill" id="socket">—</span><span class="pill" id="fresh">—</span></div></header>
<section class="grid"><div class="card"><div class="label">Bot state</div><div class="value" id="state">—</div></div><div class="card"><div class="label">Symbols scanned</div><div class="value" id="symbols">0</div></div><div class="card"><div class="label">Evaluations</div><div class="value" id="evaluations">0</div></div><div class="card"><div class="label">Trades</div><div class="value" id="trades">0</div></div><div class="card"><div class="label">Win rate</div><div class="value" id="winrate">—</div></div><div class="card"><div class="label">Net P&amp;L</div><div class="value" id="pnl">₹0.00</div></div></section>
<section class="layout"><div class="panel"><h2>Live candidate board</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Score</th><th>Spread</th><th>Spot ROC</th><th>CE / PE ROC</th><th>OI</th><th>Latest decision</th></tr></thead><tbody id="candidates"></tbody></table></div></div><div class="panel"><h2>Top rejection reasons</h2><div id="rejections"></div></div></section>
<section class="layout"><div class="panel"><h2>Paper trades</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Underlying</th><th>Contract</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P&amp;L</th></tr></thead><tbody id="tradeRows"></tbody></table></div></div><div class="panel"><h2>Recent activity</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Event</th><th>Symbol</th><th>Message</th></tr></thead><tbody id="events"></tbody></table></div></div></section><div class="footer">Runtime: <span id="root">—</span> · Updated <span id="updated">—</span> · refresh every 3 seconds</div></main>
<script>const $=id=>document.getElementById(id),fmt=n=>n==null?'—':Number(n).toFixed(2),esc=v=>String(v).replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x])),cell=(v,c='')=>`<td class="${c}">${esc(v??'—')}</td>`;function rows(t,d,r,c){$(t).innerHTML=d.length?d.map(r).join(''):`<tr><td colspan="${c}" class="empty">No records yet</td></tr>`}async function refresh(){try{const d=await fetch('/api/activity',{cache:'no-store'}).then(r=>r.json());$('mode').textContent=d.mode;$('session').textContent=d.session;$('socket').textContent=d.socket_state;$('socket').className='pill '+(d.socket_state==='CONNECTED'?'ok':'bad');$('fresh').textContent=d.last_event_age_seconds==null?'NO EVENTS':`${d.last_event_age_seconds}s old`;$('fresh').className='pill '+(d.last_event_age_seconds!=null&&d.last_event_age_seconds<10?'ok':'warn');$('state').textContent=d.bot_state;$('symbols').textContent=d.scanned_symbols;$('evaluations').textContent=d.evaluations;$('trades').textContent=d.summary.trades;$('winrate').textContent=d.summary.win_rate_pct==null?'—':`${d.summary.win_rate_pct}%`;$('pnl').textContent=`₹${fmt(d.summary.net_pnl)}`;$('pnl').className='value '+(d.summary.net_pnl>0?'ok':d.summary.net_pnl<0?'bad':'');$('root').textContent=d.runtime_root;$('updated').textContent=new Date(d.generated_at).toLocaleTimeString();rows('candidates',d.candidates,c=>`<tr>${cell(c.time)}${cell(c.symbol)}${cell(c.direction||'WAIT',c.direction?'ok':'warn')}${cell(fmt(c.confidence))}${cell(fmt(c.spread_pct)+'%')}${cell(fmt(c.spot_roc_pct)+'%')}${cell(fmt(c.ce_roc_pct)+' / '+fmt(c.pe_roc_pct))}${cell(c.oi)}${cell(c.reason,'reason')}</tr>`,9);const max=Math.max(1,...d.rejections.map(x=>x.count));$('rejections').innerHTML=d.rejections.length?d.rejections.map(x=>`<div class="barrow"><span>${esc(x.reason)}</span><div class="bar"><i style="width:${x.count/max*100}%"></i></div><b>${x.count}</b></div>`).join(''):'<div class="empty">No rejected signals yet</div>';rows('tradeRows',d.trades,t=>`<tr>${cell(t.time)}${cell(t.underlying)}${cell(`${t.strike} ${t.option_type}`)}${cell(t.quantity)}${cell(fmt(t.entry_price))}${cell(fmt(t.exit_price))}${cell(t.exit_reason)}${cell('₹'+fmt(t.net_pnl),t.net_pnl>0?'ok':'bad')}</tr>`,8);rows('events',d.events,e=>`<tr>${cell(e.time)}${cell(e.event)}${cell(e.symbol)}${cell(e.message,'reason')}</tr>`,4)}catch(e){$('fresh').textContent='DASHBOARD ERROR';$('fresh').className='pill bad'}}refresh();setInterval(refresh,3000);</script></body></html>"""


@app.route("/")
def index():
    return PAGE


@app.route("/api/activity")
def api_activity():
    return jsonify(current_activity())


@app.route("/api/status")
def api_status():
    activity = current_activity()
    return jsonify({key: activity[key] for key in (
        "generated_at", "session", "mode", "bot_state", "socket_state", "summary"
    )})


if __name__ == "__main__":
    app.run(host=os.environ.get("FNO_DASHBOARD_HOST", "0.0.0.0"),
            port=int(os.environ.get("FNO_DASHBOARD_PORT", "5050")), debug=False)
