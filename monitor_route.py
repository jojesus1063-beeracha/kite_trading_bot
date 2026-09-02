from watchlist_section import WATCHLIST_SECTION
from pipeline_dashboard_section import PIPELINE_MONITOR_SECTION

MONITOR_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="dark">
    <title>Matmon · Hidden Treasure Monitor</title>
    <style>
        :root { --bg:#080b12; --surface:#111722; --surface-2:#151d2a; --border:#263244; --text:#eef2f8; --muted:#91a0b5; --green:#34d399; --red:#fb7185; --yellow:#fbbf24; --blue:#60a5fa; }
        * { box-sizing: border-box; }
        html { scroll-behavior: smooth; scroll-padding-top: 76px; }
        body { font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at 50% -15%, #142036 0, var(--bg) 34rem); color: var(--text); margin: 0; padding: 0 22px 42px; font-variant-numeric: tabular-nums; -webkit-font-smoothing: antialiased; }
        .shell { width: min(100%, 1680px); margin: 0 auto; }
        .page-head { display:flex; justify-content:space-between; align-items:flex-end; gap:18px; padding:24px 2px 16px; }
        h1 { font-size: clamp(22px, 3vw, 30px); letter-spacing:-.035em; margin:0 0 5px; }
        .subtitle { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
        .page-head .subtitle { margin:0; }
        .live-mark { display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; }
        .quick-nav { position:sticky; top:0; z-index:20; display:flex; gap:6px; overflow-x:auto; padding:9px 0; margin-bottom:14px; background:rgba(8,11,18,.9); backdrop-filter:blur(14px); scrollbar-width:none; }
        .quick-nav::-webkit-scrollbar { display:none; }
        .quick-nav a { flex:none; color:var(--muted); border:1px solid var(--border); background:rgba(17,23,34,.92); border-radius:999px; padding:7px 12px; text-decoration:none; font-size:12px; font-weight:650; }
        .quick-nav a:hover { color:var(--text); border-color:#3b82f6; }
        .section { background:linear-gradient(180deg,rgba(20,28,41,.98),rgba(14,20,30,.98)); border:1px solid var(--border); border-radius:14px; padding:18px; margin-bottom:14px; box-shadow:0 12px 30px rgba(0,0,0,.18); content-visibility:auto; contain-intrinsic-size:auto 340px; }
        .section h2 { font-size:12px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); margin:0 0 14px; }
        .table-wrap { width:100%; overflow:auto; border:1px solid var(--border); border-radius:10px; scrollbar-color:#344258 transparent; }
        table { width:100%; border-collapse:separate; border-spacing:0; font-size:12px; }
        th { position:sticky; top:0; z-index:2; text-align:left; padding:9px 10px; color:var(--muted); background:#151d2a; font-weight:650; border-bottom:1px solid var(--border); white-space:nowrap; }
        td { padding:9px 10px; border-bottom:1px solid rgba(38,50,68,.65); white-space:nowrap; }
        tr:last-child td { border-bottom:0; }
        tbody tr:hover td { background:rgba(96,165,250,.055); }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:9px; }
        .metric { min-width:0; background:rgba(8,12,19,.52); border:1px solid rgba(38,50,68,.78); border-radius:10px; padding:11px 12px; }
        .metric .label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.055em; }
        .metric .value { overflow-wrap:anywhere; font-size:17px; font-weight:680; line-height:1.25; margin-top:4px; }
        .green { color:var(--green); } .red { color:var(--red); } .yellow { color:var(--yellow); }
        .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
        .dot-green { background: #22c55e; } .dot-red { background: #ef4444; } .dot-yellow { background: #eab308; }
        .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .badge-active { background: #1e3a2e; color: #22c55e; }
        .badge-near { background: #3a3419; color: #eab308; }
        .badge-hit { background: #3a2419; color: #f97316; }
        .badge-stale { background: #3a1919; color: #ef4444; }
        .empty { color: #6b7280; font-style: italic; padding: 8px; }
        a { color:var(--blue); }
        .sr-note { color:var(--muted); font-size:11px; margin:8px 2px 0; }
        @media (max-width:700px) {
            body { padding:0 10px 26px; }
            .page-head { align-items:flex-start; flex-direction:column; padding-top:17px; }
            .section { padding:13px; border-radius:12px; }
            .grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; }
            .metric { padding:9px; }
            .metric .value { font-size:15px; }
        }
        @media (prefers-reduced-motion:reduce) { html { scroll-behavior:auto; } }
    
        /* ===== MATMON HEAVEN + HIDDEN TREASURE THEME ===== */
        :root {
            --bg:#071424;
            --surface:#fffaf0;
            --surface-2:#f8edd7;
            --border:#d6a94c;
            --text:#102748;
            --muted:#66758b;
            --green:#159447;
            --red:#dc3545;
            --yellow:#c88a16;
            --blue:#2367b1;
            --gold:#d4a536;
            --gold2:#f3d98b;
            --navy:#071b35;
            --cream:#fffaf0;
        }

        html { background:#071424; }

        body {
            color:var(--text);
            padding:0 22px 42px 166px;
            background:
                radial-gradient(circle at 48% -4%, rgba(255,222,144,.95) 0,
                    rgba(255,226,161,.55) 10%,
                    rgba(173,207,238,.28) 28%,
                    transparent 48%),
                radial-gradient(ellipse at 10% 75%,rgba(255,255,255,.52),transparent 28%),
                radial-gradient(ellipse at 85% 82%,rgba(255,255,255,.48),transparent 30%),
                linear-gradient(180deg,#315f92 0,#9dc8e8 20%,#e9eff3 45%,#fbf1db 100%);
            background-attachment:fixed;
            min-height:100vh;
        }

        body:before,
        body:after {
            content:"";
            position:fixed;
            z-index:-1;
            border-radius:50%;
            filter:blur(45px);
            pointer-events:none;
        }

        body:before {
            width:520px;height:180px;left:10%;bottom:8%;
            background:rgba(255,255,255,.72);
        }

        body:after {
            width:650px;height:190px;right:4%;bottom:3%;
            background:rgba(255,251,232,.72);
        }

        .shell {
            width:min(100%,1780px);
            margin:0 auto;
        }

        .page-head {
            min-height:108px;
            align-items:center;
            padding:18px 4px 13px;
            border-bottom:1px solid rgba(216,168,67,.45);
        }

        .page-head h1 {
            font-family:Georgia,"Times New Roman",serif;
            color:#0b2a55;
            text-transform:uppercase;
            letter-spacing:.025em;
            font-size:clamp(25px,3vw,38px);
            text-shadow:0 1px 0 #fff,0 0 18px rgba(255,206,91,.35);
        }

        .subtitle {
            color:#7d5111;
            font-weight:650;
        }

        .live-mark {
            color:#174d34;
            background:rgba(255,250,240,.88);
            border:1px solid rgba(205,157,54,.65);
            border-radius:12px;
            padding:12px 15px;
            box-shadow:0 8px 30px rgba(54,78,108,.12);
        }

        .quick-nav {
            position:fixed;
            left:0;
            top:0;
            bottom:0;
            width:146px;
            z-index:50;
            display:flex;
            flex-direction:column;
            gap:5px;
            padding:126px 10px 18px;
            margin:0;
            overflow-y:auto;
            background:
                linear-gradient(180deg,rgba(5,25,52,.98),rgba(7,29,60,.97)),
                #071b35;
            border-right:1px solid rgba(222,177,75,.75);
            box-shadow:7px 0 28px rgba(3,14,31,.25);
        }

        .quick-nav:before {
            content:"✝\\A MATMON\\A SOLI DEO GLORIA";
            white-space:pre;
            position:absolute;
            top:17px;
            left:0;
            right:0;
            text-align:center;
            color:#f6d071;
            font-family:Georgia,serif;
            font-size:14px;
            line-height:1.9;
            text-shadow:0 0 12px rgba(255,210,102,.6);
        }

        .quick-nav a {
            border:1px solid transparent;
            border-radius:10px;
            padding:10px 11px;
            color:#f5e7c1;
            background:transparent;
            font-size:12px;
        }

        .quick-nav a:hover,
        .quick-nav a:first-child {
            color:#fff5c9;
            border-color:#c99831;
            background:linear-gradient(90deg,#133f77,#0b2b56);
            box-shadow:inset 3px 0 0 #f1ca69;
        }

        .section {
            position:relative;
            overflow:hidden;
            color:#142843;
            background:
                linear-gradient(180deg,rgba(255,253,246,.96),rgba(249,239,217,.94));
            border:1px solid rgba(200,151,49,.70);
            border-radius:14px;
            padding:17px;
            margin-bottom:13px;
            box-shadow:
                0 8px 30px rgba(33,61,90,.12),
                inset 0 1px 0 rgba(255,255,255,.95);
        }

        .section:before {
            content:"";
            position:absolute;
            top:0;left:0;right:0;height:2px;
            background:linear-gradient(90deg,transparent,#e5bd5c,transparent);
            opacity:.85;
        }

        .section h2 {
            color:#183153;
            font-weight:800;
            letter-spacing:.055em;
        }

        .metric {
            background:rgba(255,255,255,.64);
            border:1px solid rgba(196,153,64,.48);
            box-shadow:inset 0 1px 0 #fff;
        }

        .metric .label { color:#687588; }
        .metric .value { color:#162845; }

        .green { color:#119145!important; }
        .red { color:#d93645!important; }
        .yellow { color:#bb7c06!important; }

        .table-wrap {
            border:1px solid rgba(191,148,60,.45);
            background:rgba(255,255,255,.45);
        }

        table { color:#19314e; }

        th {
            color:#58687c;
            background:#f5ead3;
            border-bottom:1px solid rgba(190,146,57,.48);
        }

        td {
            border-bottom:1px solid rgba(203,177,126,.32);
        }

        tbody tr:hover td { background:rgba(237,201,113,.13); }

        .heaven-banner {
            display:grid;
            grid-template-columns:minmax(0,1.6fr) auto auto;
            gap:10px;
            margin-bottom:13px;
        }

        .heaven-title,
        .heaven-clock,
        .heaven-scripture {
            border:1px solid rgba(196,149,52,.63);
            border-radius:14px;
            background:rgba(255,250,237,.88);
            box-shadow:0 8px 26px rgba(35,57,79,.12);
        }

        .heaven-title {
            padding:17px 20px;
            position:relative;
            overflow:hidden;
        }

        .heaven-title:after {
            content:"";
            position:absolute;
            right:-30px;
            top:-60px;
            width:190px;
            height:190px;
            border-radius:50%;
            background:radial-gradient(circle,#fff7cb 0,rgba(249,207,90,.4) 25%,transparent 70%);
        }

        .heaven-kicker {
            font-family:Georgia,serif;
            font-size:27px;
            color:#0e2b51;
            font-weight:700;
        }

        .heaven-motto {
            color:#946317;
            font-size:12px;
            margin-top:5px;
        }

        .heaven-clock {
            min-width:205px;
            padding:14px 17px;
            font-size:13px;
            color:#233a58;
        }

        .heaven-scripture {
            max-width:280px;
            padding:13px 16px;
            font-family:Georgia,serif;
            font-size:12px;
            color:#745015;
            line-height:1.45;
        }

        .treasure-dashboard {
            display:grid;
            grid-template-columns:minmax(300px,.95fr) minmax(480px,1.65fr) minmax(280px,.82fr);
            gap:12px;
            margin-bottom:13px;
        }

        .treasure-card {
            min-width:0;
            border:1px solid rgba(198,151,54,.70);
            border-radius:14px;
            background:linear-gradient(180deg,rgba(255,252,243,.96),rgba(249,236,210,.94));
            padding:16px;
            box-shadow:0 10px 28px rgba(37,61,87,.12);
        }

        .treasure-card h3 {
            margin:0 0 11px;
            color:#173150;
            text-transform:uppercase;
            letter-spacing:.04em;
            font-size:12px;
        }

        .pressure-wrap {
            display:grid;
            grid-template-columns:1fr 1fr;
            gap:9px;
        }

        .pressure-side {
            text-align:center;
            border-radius:12px;
            padding:15px 10px;
            border:1px solid rgba(197,159,91,.35);
            background:rgba(255,255,255,.55);
        }

        .pressure-number {
            font-size:35px;
            font-family:Georgia,serif;
            font-weight:700;
            line-height:1;
            margin:5px 0;
        }

        .pressure-bar {
            height:8px;
            display:flex;
            overflow:hidden;
            border-radius:999px;
            background:#eee1c9;
            margin-top:13px;
        }

        .pressure-buy {
            background:linear-gradient(90deg,#18974b,#63bf6d);
            width:50%;
            transition:width .4s ease;
        }

        .pressure-sell {
            background:linear-gradient(90deg,#e15a4d,#c82638);
            width:50%;
            transition:width .4s ease;
        }

        .neural-table table { font-size:11px; }

        .flow-buy {
            color:#0d8c42;
            font-weight:700;
        }

        .flow-sell {
            color:#d52e3d;
            font-weight:700;
        }

        .flow-neutral {
            color:#ad7814;
            font-weight:700;
        }

        .micro-list {
            display:grid;
            gap:7px;
        }

        .micro-row {
            display:grid;
            grid-template-columns:1fr auto auto;
            gap:7px;
            align-items:center;
            padding:7px 8px;
            background:rgba(255,255,255,.48);
            border-bottom:1px solid rgba(200,164,97,.28);
            font-size:11px;
        }

        .micro-gauge {
            width:160px;
            height:80px;
            margin:8px auto 16px;
            border-radius:160px 160px 0 0;
            background:conic-gradient(
                from 270deg at 50% 100%,
                #d83a3a 0deg,
                #eca234 54deg,
                #e2cf46 90deg,
                #79b957 126deg,
                #159447 180deg,
                transparent 180deg
            );
            position:relative;
        }

        .micro-gauge:after {
            content:"";
            position:absolute;
            left:23px;right:23px;bottom:0;height:57px;
            border-radius:120px 120px 0 0;
            background:#fff7e6;
        }

        .micro-score {
            position:relative;
            margin-top:-47px;
            z-index:2;
            text-align:center;
            font-size:27px;
            font-weight:800;
        }

        .awaiting {
            color:#8c6b2b;
            padding:14px;
            border:1px dashed rgba(172,130,49,.55);
            border-radius:9px;
            background:rgba(255,249,231,.62);
            text-align:center;
            font-size:11px;
        }

        .faith-footer {
            margin:14px 0 4px;
            padding:11px 15px;
            display:flex;
            justify-content:space-between;
            gap:15px;
            flex-wrap:wrap;
            border-radius:12px;
            color:#6e4e17;
            border:1px solid rgba(194,146,46,.6);
            background:rgba(255,249,232,.78);
            font-family:Georgia,serif;
            font-size:12px;
        }

        @media(max-width:1180px) {
            body { padding-left:18px; }
            .quick-nav {
                position:sticky;
                width:auto;
                height:auto;
                top:0;
                padding:8px;
                flex-direction:row;
                background:rgba(7,27,53,.97);
            }
            .quick-nav:before { display:none; }
            .treasure-dashboard { grid-template-columns:1fr; }
            .heaven-banner { grid-template-columns:1fr; }
        }

    </style>
</head>
<body>
  <div class="shell">
    
    <div class="heaven-banner">
      <div class="heaven-title">
        <div class="heaven-kicker">✝ MATMON LIVE TRADING DASHBOARD</div>
        <div class="heaven-motto">
          Real-time · Paper Trading · Trade with Wisdom · Manage with Stewardship · Soli Deo Gloria
        </div>
      </div>

      <div class="heaven-clock">
        <strong>Last update</strong><br>
        {{ updated }}<br><br>
        <span class="{{ 'green' if health.get('api_connection') == 'Authenticated' else 'red' }}">
          ● {{ health.get('trading_mode','N/A') }} · {{ health.get('api_connection','N/A') }}
        </span>
      </div>

      <div class="heaven-scripture">
        ✦ “Commit to the LORD whatever you do, and he will establish your plans.”<br>
        <strong>Proverbs 16:3</strong>
      </div>
    </div>

    <nav class="quick-nav" aria-label="Dashboard sections">
      <a href="#system-status">System</a><a href="#portfolio-summary">Portfolio</a><a href="#pipeline-dashboard">Pipeline</a><a href="#live-positions">Positions</a><a href="#today-session">Session</a><a href="#watchlist-analysis">Watchlist</a><a href="/">Settings</a>
    </nav>

    <div class="section" id="system-status">
        <h2>System Status</h2>
        <div class="grid">
            <div class="metric"><div class="label">Trading Mode</div><div class="value {{ 'red' if health.get('trading_mode') == 'LIVE' else 'green' }}">{{ health.get('trading_mode', 'N/A') }}</div></div>
            <div class="metric"><div class="label">API Connection</div><div class="value {{ 'green' if health.get('api_connection') == 'Authenticated' else 'red' }}"><span class="dot {{ 'dot-green' if health.get('api_connection') == 'Authenticated' else 'dot-red' }}"></span>{{ health.get('api_connection', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Entry Scheduler</div><div class="value">{{ health.get('entry_scheduler', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Market Alignment</div><div class="value">{{ health.get('market_alignment', 'N/A') }}</div></div>
            <div class="metric"><div class="label">ADX Filter</div><div class="value">{{ health.get('adx_filter', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Watchlist Size</div><div class="value">{{ health.get('watchlist_size', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Open Positions</div><div class="value">{{ health.get('open_positions', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Bot Uptime</div><div class="value">{{ "%.0f"|format(health.get('bot_uptime_seconds', 0) / 60) }} min</div></div>
            <div class="metric"><div class="label">Memory</div><div class="value">{{ "%.0f"|format(health.get('memory_usage_mb', 0) or 0) }} MB</div></div>
            <div class="metric"><div class="label">Git Commit</div><div class="value">{{ health.get('git_commit_hash', 'N/A') }}</div></div>
        </div>
    </div>

    <div class="section" id="portfolio-summary">
        <h2>Portfolio Summary</h2>
        <div class="grid">
            <div class="metric"><div class="label">Open Positions</div><div class="value">{{ portfolio.get('total_open_positions', 0) }} ({{ portfolio.get('buy_positions', 0) }} BUY / {{ portfolio.get('sell_positions', 0) }} SELL)</div></div>
            <div class="metric"><div class="label">Total Exposure</div><div class="value">Rs{{ "%.2f"|format(portfolio.get('total_exposure', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Gross Unrealized P&L</div><div class="value {{ 'green' if (portfolio.get('gross_unrealized_pnl') or 0) >= 0 else 'red' }}">Rs{{ "%.2f"|format(portfolio.get('gross_unrealized_pnl', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Net Unrealized P&L</div><div class="value {{ 'green' if (portfolio.get('net_unrealized_pnl') or 0) >= 0 else 'red' }}">Rs{{ "%.2f"|format(portfolio.get('net_unrealized_pnl', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Available Cash</div><div class="value">Rs{{ "%.2f"|format(portfolio.get('available_cash', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Margin Utilization</div><div class="value">{{ "%.1f"|format(portfolio.get('margin_utilization_pct', 0) or 0) }}%</div></div>
            <div class="metric"><div class="label">Largest Winner</div><div class="value green">{{ portfolio.get('largest_winning_position') or '-' }}</div></div>
            <div class="metric"><div class="label">Largest Loser</div><div class="value red">{{ portfolio.get('largest_losing_position') or '-' }}</div></div>
            <div class="metric"><div class="label">Portfolio Live Reward:Risk</div><div class="value">{{ "%.2f"|format(portfolio.get('portfolio_reward_risk', 0) or 0) }}</div></div>
        </div>
    </div>


    <div class="treasure-dashboard" id="neural-links">

      <section class="treasure-card">
        <h3>⚖ Watchlist Order-Flow Tilt</h3>

        <div class="pressure-wrap">
          <div class="pressure-side">
            <div class="green">BUYERS</div>
            <div id="market-buyers" class="pressure-number green">--%</div>
            <small>Composite strength</small>
          </div>

          <div class="pressure-side">
            <div class="red">SELLERS</div>
            <div id="market-sellers" class="pressure-number red">--%</div>
            <small>Composite strength</small>
          </div>
        </div>

        <div class="pressure-bar">
          <div id="pressure-buy-bar" class="pressure-buy"></div>
          <div id="pressure-sell-bar" class="pressure-sell"></div>
        </div>

        <div id="market-pressure-note" class="awaiting" style="margin-top:12px">
          Awaiting live Matmon microstructure observations.
        </div>

        <div style="margin-top:13px;color:#775a23;font-size:11px">
          Buyers/Sellers are calculated per stock first, then aggregated for the watchlist.
          Observational only — not an entry gate.
        </div>
      </section>

      <section class="treasure-card neural-table">
        <h3>✦ Live Stock Neural Links · Observational</h3>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>LTP</th>
                <th>Buyers</th>
                <th>Sellers</th>
                <th>Flow</th>
                <th>Micro</th>
                <th>L1</th>
                <th>5L OBI</th>
                <th>Bid Vel</th>
                <th>Ask Vel</th>
                <th>Spread</th>
                <th>3s Path</th>
              </tr>
            </thead>
            <tbody id="neural-stock-body">
              <tr>
                <td colspan="12" class="awaiting">
                  Waiting for stock-specific live order-book observations.
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div style="margin-top:8px;color:#7a6748;font-size:10px">
          Strength combines available microprice, L1 imbalance, top-5 OBI,
          depth, velocity and spread observations. Missing values are never fabricated.
        </div>
      </section>

      <section class="treasure-card">
        <h3>♛ Microstructure Summary · Observational</h3>

        <div class="micro-gauge"></div>
        <div id="micro-score" class="micro-score">--</div>

        <div id="micro-list" class="micro-list">
          <div class="awaiting">
            Waiting for live microstructure feed.
          </div>
        </div>
      </section>

    </div>


""" + PIPELINE_MONITOR_SECTION + """

    <div class="section" id="live-positions">
        <h2>Live Positions</h2>
        {% if positions %}
        <div class="table-wrap"><table>
            <tr>
                <th>Symbol</th><th>Raw</th><th>Market</th><th>Policy</th><th>Final</th><th>Qty</th><th>Entry</th><th>Current</th>
                <th>Time in Trade</th><th>Gross P&L</th><th>Net P&L</th><th>Profit %</th>
                <th>MFE %</th><th>MAE %</th><th>Hard Stop</th><th>Strategy Stop</th><th>Active Target</th>
                <th>Dist. Stop %</th><th>Dist. Target %</th><th>Entry R:R</th><th>Live R:R</th><th>Status</th>
            </tr>
            {% for p in positions %}
            <tr>
                <td>{{ p.get('symbol') }}</td>
                <td>{{ p.get('raw_direction') or '-' }}</td>
                <td>{{ p.get('policy_market_trend') or '-' }}</td>
                <td>{{ p.get('policy_decision') or '-' }}</td>
                <td>{{ p.get('final_direction') or p.get('side') }}</td>
                <td>{{ p.get('quantity') }}</td>
                <td>{{ "%.2f"|format(p.get('entry_price', 0) or 0) }}</td>
                <td>{{ "%.2f"|format(p.get('current_price', 0) or 0) if p.get('current_price') is not none else 'N/A' }}</td>
                <td>{{ "%.0f"|format(p.get('time_in_trade_minutes', 0) or 0) }}m</td>
                <td class="{{ 'green' if (p.get('gross_unrealized_pnl') or 0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('gross_unrealized_pnl', 0) or 0) }}</td>
                <td class="{{ 'green' if (p.get('net_unrealized_pnl') or 0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('net_unrealized_pnl', 0) or 0) }}</td>
                <td class="{{ 'green' if (p.get('profit_pct') or 0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('profit_pct', 0) or 0) }}%</td>
                <td class="green">{{ "%.2f"|format(p.get('mfe_pct', 0) or 0) }}%</td>
                <td class="red">{{ "%.2f"|format(p.get('mae_pct', 0) or 0) }}%</td>
                <td>{{ "%.2f"|format(p.get('stop_price', 0) or 0) }}</td>
                <td>{{ "%.2f"|format(p.get('strategy_stop_price', 0) or 0) if p.get('strategy_stop_price') is not none else '-' }}</td>
                <td>{{ "%.2f"|format(p.get('target_price', 0) or 0) }}</td>
                <td>{{ "%.2f"|format(p.get('distance_to_stop_pct', 0) or 0) }}%</td>
                <td>{{ "%.2f"|format(p.get('distance_to_target_pct', 0) or 0) }}%</td>
                <td>{{ "%.2f"|format(p.get('entry_reward_risk', 0) or 0) if p.get('entry_reward_risk') is not none else '-' }}</td>
                <td>{{ "%.2f"|format(p.get('remaining_reward_risk', 0) or 0) if p.get('remaining_reward_risk') is not none else '-' }}</td>
                <td>
                    {% set st = p.get('status', 'ACTIVE') %}
                    <span class="badge {{ 'badge-stale' if 'STALE' in st else ('badge-hit' if 'HIT' in st else ('badge-near' if 'NEAR' in st else 'badge-active')) }}">{{ st }}</span>
                </td>
            </tr>
            {% endfor %}
        </table></div><div class="sr-note">Scroll horizontally to inspect every position field.</div>
        {% else %}
        <div class="empty">No open positions.</div>
        {% endif %}
    </div>

    <div class="section" id="today-session">
        <h2>Today's Session</h2>
        <div class="grid">
            <div class="metric"><div class="label">Trades Today</div><div class="value">{{ session.get('todays_trades', 0) }}</div></div>
            <div class="metric"><div class="label">Win Rate</div><div class="value">{{ "%.1f"|format(session.get('win_rate_pct', 0) or 0) }}%</div></div>
            <div class="metric"><div class="label">Net Realized P&L</div><div class="value {{ 'green' if (session.get('net_realized_profit') or 0) >= 0 else 'red' }}">Rs{{ "%.2f"|format(session.get('net_realized_profit', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Brokerage & Charges</div><div class="value">Rs{{ "%.2f"|format(session.get('brokerage_and_charges', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Profit Factor</div><div class="value">{{ profit_factor_display }}</div></div>
            <div class="metric"><div class="label">Expectancy</div><div class="value">Rs{{ "%.2f"|format(session.get('expectancy', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Consecutive Wins</div><div class="value green">{{ session.get('current_consecutive_wins', 0) }}</div></div>
            <div class="metric"><div class="label">Consecutive Losses</div><div class="value red">{{ session.get('current_consecutive_losses', 0) }}</div></div>
            <div class="metric"><div class="label">Max Drawdown Today</div><div class="value red">Rs{{ "%.2f"|format(session.get('max_drawdown_today', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Largest Winner</div><div class="value green">Rs{{ "%.2f"|format(session.get('largest_winner', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Largest Loser</div><div class="value red">Rs{{ "%.2f"|format(session.get('largest_loser', 0) or 0) }}</div></div>
        </div>
    </div>

    """ + WATCHLIST_SECTION + """
  </div>

<script>
(() => {
    const esc = (x) => {
        const d = document.createElement("div");
        d.textContent = (x ?? "");
        return d.innerHTML;
    };

    const num = (v, digits=2) => {
        const n = Number(v);
        return Number.isFinite(n) ? n.toFixed(digits) : "--";
    };

    const arrow = (v) => {
        const n = Number(v);
        if (!Number.isFinite(n)) return "→";
        if (n > 0) return "↑";
        if (n < 0) return "↓";
        return "→";
    };

    function sideClass(v) {
        const n = Number(v);
        if (!Number.isFinite(n)) return "flow-neutral";
        return n > 0 ? "flow-buy" : (n < 0 ? "flow-sell" : "flow-neutral");
    }

    function normalizeStocks(micro) {
        if (!micro) return [];

        if (Array.isArray(micro.stocks))
            return micro.stocks;

        if (micro.stocks && typeof micro.stocks === "object")
            return Object.entries(micro.stocks).map(([symbol, x]) => ({
                symbol,
                ...(x || {})
            }));

        if (Array.isArray(micro))
            return micro;

        return [];
    }

    function buyerPct(x) {
        for (const k of ["buyers_pct","buyer_pct","buy_pct","buyer_strength_pct"]) {
            const v = Number(x[k]);
            if (Number.isFinite(v)) return Math.max(0,Math.min(100,v));
        }

        // Derive only if a normalized directional score already exists.
        for (const k of ["composite_score","pressure_score","orderflow_score"]) {
            const v = Number(x[k]);
            if (Number.isFinite(v)) {
                const clipped = Math.max(-1,Math.min(1,v));
                return 50 + (clipped * 50);
            }
        }

        return null;
    }

    function sellerPct(x, bp) {
        for (const k of ["sellers_pct","seller_pct","sell_pct","seller_strength_pct"]) {
            const v = Number(x[k]);
            if (Number.isFinite(v)) return Math.max(0,Math.min(100,v));
        }
        return bp == null ? null : 100 - bp;
    }

    function flowText(bp, sp) {
        if (bp == null || sp == null) return "WAITING";
        const d = bp - sp;

        if (d >= 30) return "↑ STRONG BUY";
        if (d >= 10) return "↑ BUY";
        if (d <= -30) return "↓ STRONG SELL";
        if (d <= -10) return "↓ SELL";
        return "→ BALANCED";
    }

    function renderMarket(stocks) {
        const valid = stocks
            .map(x => {
                const bp = buyerPct(x);
                const sp = sellerPct(x,bp);
                return {bp,sp};
            })
            .filter(x => x.bp != null && x.sp != null);

        const b = document.getElementById("market-buyers");
        const s = document.getElementById("market-sellers");
        const bb = document.getElementById("pressure-buy-bar");
        const sb = document.getElementById("pressure-sell-bar");
        const note = document.getElementById("market-pressure-note");

        if (!valid.length) {
            b.textContent = "--%";
            s.textContent = "--%";
            bb.style.width = "50%";
            sb.style.width = "50%";
            note.textContent = "Awaiting live Matmon microstructure observations.";
            return;
        }

        const buyers = valid.reduce((a,x)=>a+x.bp,0)/valid.length;
        const sellers = 100-buyers;

        b.textContent = buyers.toFixed(0)+"%";
        s.textContent = sellers.toFixed(0)+"%";
        bb.style.width = buyers+"%";
        sb.style.width = sellers+"%";

        note.textContent =
            buyers > 60 ? "Buyer pressure dominant across observed stocks." :
            sellers > 60 ? "Seller pressure dominant across observed stocks." :
            "Observed watchlist order flow is broadly balanced.";
    }

    function renderStocks(stocks) {
        const body = document.getElementById("neural-stock-body");

        if (!stocks.length) {
            body.innerHTML =
              '<tr><td colspan="12" class="awaiting">' +
              'Waiting for stock-specific live order-book observations.' +
              '</td></tr>';
            return;
        }

        body.innerHTML = stocks.slice(0,30).map(x => {
            const bp = buyerPct(x);
            const sp = sellerPct(x,bp);
            const flow = flowText(bp,sp);

            const flowCls =
              flow.includes("BUY") ? "flow-buy" :
              flow.includes("SELL") ? "flow-sell" :
              "flow-neutral";

            const micro =
              x.microprice_bias ?? x.micro_bias ?? x.microprice_delta ?? null;

            const l1 =
              x.l1_imbalance ?? x.l1_obi ?? null;

            const obi =
              x.weighted5_imbalance ?? x.obi5 ?? x.five_level_obi ?? null;

            const bv =
              x.bid_velocity ?? null;

            const av =
              x.ask_velocity ?? null;

            const spread =
              x.spread_bps ?? null;

            const path =
              x.clean_path == null
                ? (x.quote_path ?? "--")
                : (x.clean_path ? "CLEAN ✓" : "REJECT");

            return `
              <tr>
                <td><strong>${esc(x.symbol ?? "--")}</strong></td>
                <td>${num(x.ltp ?? x.last_price,2)}</td>
                <td class="green">${bp == null ? "--" : bp.toFixed(0)+"%"}</td>
                <td class="red">${sp == null ? "--" : sp.toFixed(0)+"%"}</td>
                <td class="${flowCls}">${esc(flow)}</td>
                <td class="${sideClass(micro)}">${arrow(micro)} ${num(micro,2)}</td>
                <td class="${sideClass(l1)}">${num(l1,2)}</td>
                <td class="${sideClass(obi)}">${num(obi,2)}</td>
                <td class="${sideClass(bv)}">${arrow(bv)}</td>
                <td class="${sideClass(av)}">${arrow(av)}</td>
                <td>${num(spread,2)}</td>
                <td>${esc(path)}</td>
              </tr>
            `;
        }).join("");
    }

    function renderMicro(stocks) {
        const root = document.getElementById("micro-list");
        const score = document.getElementById("micro-score");

        if (!stocks.length) {
            score.textContent = "--";
            root.innerHTML =
                '<div class="awaiting">Waiting for live microstructure feed.</div>';
            return;
        }

        const x = stocks[0] || {};

        const fields = [
            ["Microprice Bias", x.microprice_bias ?? x.micro_bias],
            ["L1 Imbalance", x.l1_imbalance ?? x.l1_obi],
            ["5-Level OBI", x.weighted5_imbalance ?? x.obi5 ?? x.five_level_obi],
            ["Bid Velocity", x.bid_velocity],
            ["Ask Velocity", x.ask_velocity],
            ["Spread (bps)", x.spread_bps],
            ["Bid Depth (5L)", x.bid_depth_5l ?? x.depth_bid_qty],
            ["Ask Depth (5L)", x.ask_depth_5l ?? x.depth_ask_qty],
            ["Last Quantity", x.last_quantity],
            ["Volume Traded", x.volume_traded]
        ];

        const bp = buyerPct(x);
        score.textContent =
            bp == null ? "--" : ((bp-50)/50).toFixed(2);

        root.innerHTML = fields.map(([label,v]) => `
          <div class="micro-row">
            <span>${esc(label)}</span>
            <strong>${num(v,2)}</strong>
            <span class="${sideClass(v)}">${arrow(v)}</span>
          </div>
        `).join("");
    }

    async function refreshNeural() {
        try {
            const r = await fetch(
                "/api/monitor-data",
                {
                    cache:"no-store",
                    credentials:"same-origin"
                }
            );

            if (!r.ok) return;

            const data = await r.json();

            // Prefer explicit microstructure payload.
            // Fall back to health key if status publisher places it there.
            const micro =
                data.microstructure ??
                data.health?.microstructure ??
                {};

            const stocks = normalizeStocks(micro);

            renderMarket(stocks);
            renderStocks(stocks);
            renderMicro(stocks);

        } catch (_) {
            // Fail closed: preserve previous values.
        }
    }

    refreshNeural();

    const neuralTimer = setInterval(
        refreshNeural,
        3000
    );

    window.addEventListener(
        "pagehide",
        () => clearInterval(neuralTimer),
        {once:true}
    );
})();
</script>

<div class="faith-footer">
  <span>🗝 Your treasure is in heaven · Matthew 6:20</span>
  <span>Trade with Wisdom · Manage with Stewardship · Glory to God.</span>
  <span>✝ Soli Deo Gloria</span>
</div>

</body>
</html>
"""
