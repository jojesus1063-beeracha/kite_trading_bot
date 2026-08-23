PIPELINE_MONITOR_SECTION = r"""
    {% set pipeline = pipeline|default({
        'services': {'live_bot':'unknown','watchlist_timer':'unknown'},
        'selector': {'status':'awaiting','fresh_today':false,'selected_count':0,'top':[]},
        'strategy': {'market_policy':'Bearish→BUY; Bullish→raw; Sideways→SELL; Unknown→skip'},
        'limits': {'risk_per_trade_pct':2.0,'max_trades_per_day':10,'max_open_positions':1,'max_daily_loss_pct':0.5,'force_square_off':'15:08 IST'},
        'recent_decisions': []
    }, true) %}
    <div class="section" id="pipeline-dashboard">
        <h2>Clean Pipeline Integration</h2>
        <div class="grid" id="pipeline-metrics">
            <div class="metric"><div class="label">Live Bot</div><div class="value">{{ pipeline.services.live_bot }}</div></div>
            <div class="metric"><div class="label">Watchlist Timer</div><div class="value {{ 'green' if pipeline.services.watchlist_timer == 'active' else 'red' }}">{{ pipeline.services.watchlist_timer }}</div></div>
            <div class="metric"><div class="label">Selector</div><div class="value {{ 'green' if pipeline.selector.fresh_today else 'yellow' }}">{{ pipeline.selector.status }}</div></div>
            <div class="metric"><div class="label">Top-120 Loaded</div><div class="value">{{ pipeline.selector.selected_count }}/120</div></div>
            <div class="metric"><div class="label">Raw Signal</div><div class="value" style="font-size:13px;">EMA9/EMA21 · 3m</div></div>
            <div class="metric"><div class="label">Legacy Filters</div><div class="value green" style="font-size:13px;">Observational</div></div>
            <div class="metric"><div class="label">Risk / Trade</div><div class="value">{{ pipeline.limits.risk_per_trade_pct }}%</div></div>
            <div class="metric"><div class="label">Trades / Open</div><div class="value">{{ pipeline.limits.max_trades_per_day }} / {{ pipeline.limits.max_open_positions }}</div></div>
            <div class="metric"><div class="label">Daily Loss Stop</div><div class="value red">{{ pipeline.limits.max_daily_loss_pct }}%</div></div>
            <div class="metric"><div class="label">Square-off</div><div class="value">{{ pipeline.limits.force_square_off }}</div></div>
        </div>
        <div class="subtitle" style="margin:12px 0;">{{ pipeline.strategy.market_policy }}</div>

        <h2 style="margin-top:18px;">Recent Direction Decisions</h2>
        <div id="pipeline-decisions">
        {% if pipeline.recent_decisions %}
        <table>
            <tr><th>Time</th><th>Symbol</th><th>Market</th><th>Raw</th><th>Policy</th><th>Final</th><th>Status</th></tr>
            {% for row in pipeline.recent_decisions[:10] %}
            <tr>
                <td>{{ row.get('recorded_at','')[-14:-6] }}</td><td>{{ row.get('symbol','-') }}</td>
                <td>{{ row.get('market','-') }}</td><td>{{ row.get('raw_direction','-') }}</td>
                <td>{{ row.get('decision','-') }}</td><td>{{ row.get('final_direction') or '-' }}</td>
                <td>{{ row.get('status','-') }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}<div class="empty">No direction decisions yet. They will appear after the first live scan.</div>{% endif %}
        </div>
        {% if pipeline.selector.top %}
        <h2 style="margin-top:18px;">Current Top Momentum/RVOL Leaders</h2>
        <table>
            <tr><th>Rank</th><th>Symbol</th><th>Score</th><th>Momentum</th><th>RVOL</th><th>Sweet Distance</th></tr>
            {% for row in pipeline.selector.top %}
            <tr><td>{{ loop.index }}</td><td>{{ row.symbol }}</td><td>{{ row.score }}</td>
                <td>{{ "%.3f"|format(row.momentum_pct) }}%</td><td>{{ "%.3f"|format(row.relative_volume) }}</td>
                <td>{{ "%.3f"|format(row.sweet_spot_distance) }}</td></tr>
            {% endfor %}
        </table>
        {% endif %}
        <div class="subtitle" id="pipeline-last-refresh" style="margin-top:10px;">Dashboard telemetry is read-only.</div>
    </div>
    <script>
    window.addEventListener("monitorData", function(event) {
        const p = event.detail.pipeline || {};
        const s = p.selector || {};
        const services = p.services || {};
        const limits = p.limits || {};
        const metrics = document.getElementById("pipeline-metrics");
        if (metrics) {
            metrics.innerHTML =
                '<div class="metric"><div class="label">Live Bot</div><div class="value">' + (services.live_bot || 'unknown') + '</div></div>' +
                '<div class="metric"><div class="label">Watchlist Timer</div><div class="value ' + (services.watchlist_timer === 'active' ? 'green' : 'red') + '">' + (services.watchlist_timer || 'unknown') + '</div></div>' +
                '<div class="metric"><div class="label">Selector</div><div class="value ' + (s.fresh_today ? 'green' : 'yellow') + '">' + (s.status || 'awaiting') + '</div></div>' +
                '<div class="metric"><div class="label">Top-120 Loaded</div><div class="value">' + (s.selected_count || 0) + '/120</div></div>' +
                '<div class="metric"><div class="label">Raw Signal</div><div class="value" style="font-size:13px;">EMA9/EMA21 · 3m</div></div>' +
                '<div class="metric"><div class="label">Legacy Filters</div><div class="value green" style="font-size:13px;">Observational</div></div>' +
                '<div class="metric"><div class="label">Risk / Trade</div><div class="value">' + (limits.risk_per_trade_pct ?? 2) + '%</div></div>' +
                '<div class="metric"><div class="label">Trades / Open</div><div class="value">' + (limits.max_trades_per_day ?? 10) + ' / ' + (limits.max_open_positions ?? 1) + '</div></div>' +
                '<div class="metric"><div class="label">Daily Loss Stop</div><div class="value red">' + (limits.max_daily_loss_pct ?? 0.5) + '%</div></div>' +
                '<div class="metric"><div class="label">Square-off</div><div class="value">' + (limits.force_square_off || '15:08 IST') + '</div></div>';
        }
        const decisions = document.getElementById("pipeline-decisions");
        if (decisions) {
            const rows = (p.recent_decisions || []).slice(0, 10);
            decisions.innerHTML = rows.length ?
                '<table><tr><th>Time</th><th>Symbol</th><th>Market</th><th>Raw</th><th>Policy</th><th>Final</th><th>Status</th></tr>' +
                rows.map(r => '<tr><td>' + String(r.recorded_at || '').slice(-14,-6) + '</td><td>' + (r.symbol || '-') + '</td><td>' + (r.market || '-') + '</td><td>' + (r.raw_direction || '-') + '</td><td>' + (r.decision || '-') + '</td><td>' + (r.final_direction || '-') + '</td><td>' + (r.status || '-') + '</td></tr>').join('') + '</table>' :
                '<div class="empty">No direction decisions yet. They will appear after the first live scan.</div>';
        }
        const refreshed = document.getElementById("pipeline-last-refresh");
        if (refreshed) refreshed.textContent = 'Pipeline refreshed: ' + new Date().toLocaleTimeString('en-IN', {hour12:false, timeZone:'Asia/Kolkata'});
    });
    </script>
"""

PIPELINE_FORM_CARD = r"""
    <div class="card">
      <h2>Active Clean Pipeline</h2>
      <p style="color:var(--text-muted);font-size:13px;">Momentum/RVOL Top-120 → 3-minute EMA9/EMA21 → market direction policy → final-side rebuild → execution safety.</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:14px 0;">
        <span class="pill info">Bot: {{ pipeline.services.live_bot }}</span>
        <span class="pill {{ 'up' if pipeline.services.watchlist_timer == 'active' else 'warning' }}">Timer: {{ pipeline.services.watchlist_timer }}</span>
        <span class="pill neutral">Top-120: {{ pipeline.selector.selected_count }}/120</span>
        <span class="pill neutral">Legacy filters: observational</span>
        <span class="pill warning">Risk {{ pipeline.limits.risk_per_trade_pct }}%</span>
        <span class="pill neutral">10 trades · 1 open · square-off 15:08</span>
      </div>
      <p style="font-size:12px;color:var(--text-muted);margin:0;">{{ pipeline.strategy.market_policy }}</p>
    </div>
"""
