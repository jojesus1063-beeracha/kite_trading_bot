WATCHLIST_SECTION = """
    <div class="section" id="watchlist-analysis">
        <h2>Watchlist Daily High/Low Analysis</h2>

        {% if freshness.status == 'NO_REPORT_AVAILABLE' %}
        <div class="empty">No watchlist report available. Run the end-of-day analytics process to generate one.</div>
        {% else %}

        <div id="wl-header-cards">
        <div class="grid" style="margin-bottom: 12px;">
            <div class="metric">
                <div class="label">Report Status</div>
                <div class="value {{ 'green' if freshness.status == 'REPORT_READY' else ('yellow' if freshness.status in ('REPORT_PARTIAL','REPORT_PROCESSING') else 'red') }}">
                    {{ freshness.status }}
                </div>
            </div>
            <div class="metric"><div class="label">Trading Session</div><div class="value">{{ freshness.report_session_date or 'N/A' }}</div></div>
            <div class="metric"><div class="label">Symbols Analysed</div><div class="value">{{ watchlist_snapshot.watchlist_size if watchlist_snapshot else 'N/A' }}</div></div>
            <div class="metric"><div class="label">Completed</div><div class="value green">{{ watchlist_snapshot.complete_count if watchlist_snapshot else 'N/A' }}</div></div>
            <div class="metric"><div class="label">Errors</div><div class="value {{ 'red' if (watchlist_snapshot.error_count or 0) > 0 else 'green' }}">{{ watchlist_snapshot.error_count if watchlist_snapshot else 'N/A' }}</div></div>
            <div class="metric"><div class="label">Snapshot ID</div><div class="value" style="font-size:11px;">{{ (watchlist_snapshot.snapshot_id or 'N/A')[:8] }}</div></div>
        </div>
        <div class="subtitle" style="margin-bottom:16px;">{{ freshness.reason }}</div>

        <div class="grid" style="margin-bottom: 16px;">
            <div class="metric">
                <div class="label">Largest Intraday Mover</div>
                {% if summary_cards.largest_mover %}
                <div class="value green">{{ summary_cards.largest_mover.symbol }} +{{ "%.2f"|format(summary_cards.largest_mover.low_to_high_pct) }}%</div>
                {% else %}<div class="value">N/A</div>{% endif %}
            </div>
            <div class="metric">
                <div class="label">Strongest Close</div>
                {% if summary_cards.strongest_close %}
                <div class="value green">{{ summary_cards.strongest_close.symbol }} +{{ "%.2f"|format(summary_cards.strongest_close.close_change_pct) }}%</div>
                {% else %}<div class="value">N/A</div>{% endif %}
            </div>
            <div class="metric">
                <div class="label">Weakest Close</div>
                {% if summary_cards.weakest_close %}
                <div class="value red">{{ summary_cards.weakest_close.symbol }} {{ "%.2f"|format(summary_cards.weakest_close.close_change_pct) }}%</div>
                {% else %}<div class="value">N/A</div>{% endif %}
            </div>
            <div class="metric"><div class="label">Average Range</div><div class="value">{{ "%.2f"|format(summary_cards.average_range_pct) if summary_cards.average_range_pct is not none else 'N/A' }}%</div></div>
            <div class="metric"><div class="label">Median Range</div><div class="value">{{ "%.2f"|format(summary_cards.median_range_pct) if summary_cards.median_range_pct is not none else 'N/A' }}%</div></div>
            <div class="metric"><div class="label">Most Common High Hour</div><div class="value">{{ "%02d:00"|format(summary_cards.most_common_high_hour) if summary_cards.most_common_high_hour is not none else 'N/A' }}</div></div>
            <div class="metric"><div class="label">Most Common Low Hour</div><div class="value">{{ "%02d:00"|format(summary_cards.most_common_low_hour) if summary_cards.most_common_low_hour is not none else 'N/A' }}</div></div>
            <div class="metric"><div class="label">High Retested</div><div class="value yellow">{{ summary_cards.high_retest_count }} stocks</div></div>
        </div>
        <div class="subtitle" id="wl-last-refresh" style="margin-top:8px;"></div>
        </div>

        <div style="display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap; align-items:center;">
            <input type="text" id="wl-search" placeholder="Search symbol..." style="padding:6px 10px; border-radius:6px; border:1px solid #2d3142; background:#14161f; color:#e5e7eb;">
            <select id="wl-sort" style="padding:6px 10px; border-radius:6px; border:1px solid #2d3142; background:#14161f; color:#e5e7eb;">
                <option value="low_to_high_pct_desc">Low→High % (highest first)</option>
                <option value="close_change_pct_desc">Day Change % (highest gain)</option>
                <option value="close_change_pct_asc">Day Change % (largest loss)</option>
                <option value="intraday_range_inr_desc">Range ₹ (largest)</option>
                <option value="high_first_reached_at_asc">Earliest High</option>
                <option value="high_first_reached_at_desc">Latest High</option>
                <option value="low_first_reached_at_asc">Earliest Low</option>
                <option value="low_first_reached_at_desc">Latest Low</option>
                <option value="high_volume_desc">Highest Volume</option>
                <option value="symbol_asc">Symbol Name</option>
            </select>
            <select id="wl-filter" style="padding:6px 10px; border-radius:6px; border:1px solid #2d3142; background:#14161f; color:#e5e7eb;">
                <option value="">No filter</option>
                <option value="range_gt_1">Range above 1%</option>
                <option value="range_gt_2">Range above 2%</option>
                <option value="range_gt_3">Range above 3%</option>
                <option value="positive_close">Positive closing day</option>
                <option value="negative_close">Negative closing day</option>
                <option value="high_before_10">High reached before 10:00 AM</option>
                <option value="high_after_14">High reached after 2:00 PM</option>
                <option value="low_before_10">Low reached before 10:00 AM</option>
                <option value="low_after_14">Low reached after 2:00 PM</option>
                <option value="high_retested">High retested</option>
                <option value="low_retested">Low retested</option>
                <option value="status_complete">Status = COMPLETE</option>
                <option value="status_error">Status = ERROR</option>
            </select>
            <button onclick="exportWatchlistCSV()" style="padding:6px 14px; border-radius:6px; border:none; background:var(--accent, #22c55e); color:#0f1117; font-weight:600; cursor:pointer;">Export CSV</button>
        </div>

        <div class="table-wrap"><table id="wl-table">
            <thead>
            <tr>
                <th>Symbol</th><th>Open</th><th>Low</th><th>Low Time</th><th>High</th><th>High Time</th>
                <th>Close</th><th>Low→High %</th><th>Day Change %</th><th>Range ₹</th><th>Retest</th><th>Labels</th><th>Status</th>
            </tr>
            </thead>
            <tbody id="wl-tbody"></tbody>
        </table></div>
        <div id="wl-empty" class="empty" style="display:none;">No symbols match the current search/filter.</div>

        <script>
        const WATCHLIST_DATA = {{ watchlist_symbols_json|safe }};

        function fmtTime(iso) {
            if (!iso) return 'N/A';
            const d = new Date(iso);
            return d.toLocaleTimeString('en-IN', {hour: '2-digit', minute: '2-digit', hour12: true, timeZone: 'Asia/Kolkata'});
        }
        function fmtNum(v, decimals) {
            return (v === null || v === undefined) ? 'N/A' : Number(v).toFixed(decimals);
        }
        function hourOf(iso) {
            if (!iso) return null;
            return new Date(iso).getUTCHours() + 5;  // approx IST hour for filtering (UTC+5:30, close enough for hour-level filters)
        }
        function labelsFor(row) {
            const labels = [];
            const highFirstHour = hourOf(row.high_first_reached_at);
            if (highFirstHour !== null && highFirstHour >= 9 && highFirstHour < 10) labels.push('OPENING HIGH');
            if (row.high_last_touched_at) {
                const d = new Date(row.high_last_touched_at);
                if (d.getUTCHours() + 5 >= 15) labels.push('CLOSING STRENGTH');
            }
            if (row.high_first_reached_at && row.high_last_touched_at && row.high_first_reached_at !== row.high_last_touched_at) labels.push('HIGH RETESTED');
            if (row.low_first_reached_at && row.low_last_touched_at && row.low_first_reached_at !== row.low_last_touched_at) labels.push('LOW RETESTED');
            if (row.low_to_high_pct !== null && row.distance_below_high_pct !== null && row.low_to_high_pct > 3 && row.distance_below_high_pct < 0.5) labels.push('STRONG RECOVERY');
            if (row.distance_below_high_pct !== null && row.distance_below_high_pct < 0.5) labels.push('CLOSED NEAR HIGH');
            if (row.distance_above_low_pct !== null && row.distance_above_low_pct < 0.5) labels.push('CLOSED NEAR LOW');
            return labels;
        }

        function applyFilter(row, filterVal) {
            if (!filterVal) return true;
            if (filterVal === 'range_gt_1') return row.low_to_high_pct !== null && row.low_to_high_pct > 1;
            if (filterVal === 'range_gt_2') return row.low_to_high_pct !== null && row.low_to_high_pct > 2;
            if (filterVal === 'range_gt_3') return row.low_to_high_pct !== null && row.low_to_high_pct > 3;
            if (filterVal === 'positive_close') return row.close_change_pct !== null && row.close_change_pct > 0;
            if (filterVal === 'negative_close') return row.close_change_pct !== null && row.close_change_pct < 0;
            if (filterVal === 'high_before_10') return hourOf(row.high_first_reached_at) !== null && hourOf(row.high_first_reached_at) < 10;
            if (filterVal === 'high_after_14') return hourOf(row.high_first_reached_at) !== null && hourOf(row.high_first_reached_at) >= 14;
            if (filterVal === 'low_before_10') return hourOf(row.low_first_reached_at) !== null && hourOf(row.low_first_reached_at) < 10;
            if (filterVal === 'low_after_14') return hourOf(row.low_first_reached_at) !== null && hourOf(row.low_first_reached_at) >= 14;
            if (filterVal === 'high_retested') return row.high_first_reached_at && row.high_last_touched_at && row.high_first_reached_at !== row.high_last_touched_at;
            if (filterVal === 'low_retested') return row.low_first_reached_at && row.low_last_touched_at && row.low_first_reached_at !== row.low_last_touched_at;
            if (filterVal === 'status_complete') return row.status === 'COMPLETE';
            if (filterVal === 'status_error') return row.status !== 'COMPLETE' && row.status !== 'PARTIAL';
            return true;
        }

        function sortRows(rows, sortVal) {
            const [field, dir] = [sortVal.replace(/_asc$|_desc$/, ''), sortVal.endsWith('_desc') ? -1 : 1];
            return rows.slice().sort((a, b) => {
                let av = a[field], bv = b[field];
                if (av === null || av === undefined) av = dir === 1 ? Infinity : -Infinity;
                if (bv === null || bv === undefined) bv = dir === 1 ? Infinity : -Infinity;
                if (field === 'symbol') return dir * String(av).localeCompare(String(bv));
                if (typeof av === 'string' && av.includes('T')) return dir * (new Date(av) - new Date(bv));
                return dir * (Number(av) - Number(bv));
            });
        }

        function renderWatchlistTable() {
            const search = document.getElementById('wl-search').value.trim().toUpperCase();
            const sortVal = document.getElementById('wl-sort').value;
            const filterVal = document.getElementById('wl-filter').value;

            let rows = WATCHLIST_DATA.filter(r => (!search || r.symbol.toUpperCase().includes(search)) && applyFilter(r, filterVal));
            rows = sortRows(rows, sortVal);

            const tbody = document.getElementById('wl-tbody');
            tbody.innerHTML = '';
            document.getElementById('wl-empty').style.display = rows.length === 0 ? 'block' : 'none';

            for (const r of rows) {
                const tr = document.createElement('tr');
                const changeClass = (r.close_change_pct === null) ? '' : (r.close_change_pct >= 0 ? 'green' : 'red');
                const labels = labelsFor(r).map(l => `<span class="badge badge-near" style="margin-right:4px;">${l}</span>`).join('');
                const retest = ((r.high_first_reached_at !== r.high_last_touched_at) || (r.low_first_reached_at !== r.low_last_touched_at)) ? 'Yes' : 'No';
                tr.innerHTML = `
                    <td>${r.symbol}</td>
                    <td>${fmtNum(r.day_open, 2)}</td>
                    <td>${fmtNum(r.day_low, 2)}</td>
                    <td>${fmtTime(r.low_first_reached_at)} → ${fmtTime(r.low_last_touched_at)}</td>
                    <td>${fmtNum(r.day_high, 2)}</td>
                    <td>${fmtTime(r.high_first_reached_at)} → ${fmtTime(r.high_last_touched_at)}</td>
                    <td>${fmtNum(r.close_price, 2)}</td>
                    <td>${fmtNum(r.low_to_high_pct, 2)}%</td>
                    <td class="${changeClass}">${fmtNum(r.close_change_pct, 2)}%</td>
                    <td>${fmtNum(r.intraday_range_inr, 2)}</td>
                    <td>${retest}</td>
                    <td>${labels}</td>
                    <td><span class="badge ${r.status === 'COMPLETE' ? 'badge-active' : 'badge-stale'}">${r.status}</span></td>
                `;
                tbody.appendChild(tr);
            }
        }

        function exportWatchlistCSV() {
            const fields = ['symbol','exchange','session_date','previous_close','day_open','day_low',
                'low_first_reached_at','low_last_touched_at','day_high','high_first_reached_at','high_last_touched_at',
                'close_price','intraday_range_inr','intraday_range_pct','low_to_high_pct','open_to_high_pct',
                'previous_close_to_high_pct','close_change_pct','high_volume','low_volume','status','error'];
            let csv = fields.join(',') + '\\n';
            for (const r of WATCHLIST_DATA) {
                csv += fields.map(f => {
                    let v = r[f];
                    if (v === null || v === undefined) v = '';
                    if (typeof v === 'string' && v.includes(',')) v = '"' + v + '"';
                    return v;
                }).join(',') + '\\n';
            }
            const blob = new Blob([csv], {type: 'text/csv'});
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = 'watchlist_daily_range.csv';
            link.click();
        }

        function saveControlState() {
            sessionStorage.setItem("watchlistSearch", document.getElementById("wl-search").value);
            sessionStorage.setItem("watchlistSort", document.getElementById("wl-sort").value);
            sessionStorage.setItem("watchlistFilter", document.getElementById("wl-filter").value);
        }
        function restoreControlState() {
            document.getElementById("wl-search").value = sessionStorage.getItem("watchlistSearch") || "";
            document.getElementById("wl-sort").value = sessionStorage.getItem("watchlistSort") || "low_to_high_pct_desc";
            document.getElementById("wl-filter").value = sessionStorage.getItem("watchlistFilter") || "";
        }

        function renderHeaderAndCards(fresh) {
            const wl = fresh.watchlist_snapshot || {};
            const fr = fresh.freshness || {};
            const sc = fresh.summary_cards || {};
            const statusClass = fr.status === "REPORT_READY" ? "green" : ((fr.status === "REPORT_PARTIAL" || fr.status === "REPORT_PROCESSING") ? "yellow" : "red");
            const pct = (v) => (v === null || v === undefined) ? "N/A" : Number(v).toFixed(2) + "%";
            const mover = sc.largest_mover ? (sc.largest_mover.symbol + " +" + pct(sc.largest_mover.low_to_high_pct).replace("%","") + "%") : "N/A";
            const strong = sc.strongest_close ? (sc.strongest_close.symbol + " +" + pct(sc.strongest_close.close_change_pct).replace("%","") + "%") : "N/A";
            const weak = sc.weakest_close ? (sc.weakest_close.symbol + " " + pct(sc.weakest_close.close_change_pct).replace("%","") + "%") : "N/A";
            const hh = (sc.most_common_high_hour === null || sc.most_common_high_hour === undefined) ? "N/A" : (String(sc.most_common_high_hour).padStart(2,"0") + ":00");
            const lh = (sc.most_common_low_hour === null || sc.most_common_low_hour === undefined) ? "N/A" : (String(sc.most_common_low_hour).padStart(2,"0") + ":00");
            document.getElementById("wl-header-cards").innerHTML =
                '<div class="grid" style="margin-bottom: 12px;">' +
                    '<div class="metric"><div class="label">Report Status</div><div class="value ' + statusClass + '">' + (fr.status || "N/A") + '</div></div>' +
                    '<div class="metric"><div class="label">Trading Session</div><div class="value">' + (fr.report_session_date || "N/A") + '</div></div>' +
                    '<div class="metric"><div class="label">Symbols Analysed</div><div class="value">' + (wl.watchlist_size ?? "N/A") + '</div></div>' +
                    '<div class="metric"><div class="label">Completed</div><div class="value green">' + (wl.complete_count ?? "N/A") + '</div></div>' +
                    '<div class="metric"><div class="label">Errors</div><div class="value ' + ((wl.error_count || 0) > 0 ? "red" : "green") + '">' + (wl.error_count ?? "N/A") + '</div></div>' +
                    '<div class="metric"><div class="label">Snapshot ID</div><div class="value" style="font-size:11px;">' + ((wl.snapshot_id || "N/A").slice(0,8)) + '</div></div>' +
                '</div>' +
                '<div class="subtitle" style="margin-bottom:16px;">' + (fr.reason || "") + '</div>' +
                '<div class="grid" style="margin-bottom: 16px;">' +
                    '<div class="metric"><div class="label">Largest Intraday Mover</div><div class="value green">' + mover + '</div></div>' +
                    '<div class="metric"><div class="label">Strongest Close</div><div class="value green">' + strong + '</div></div>' +
                    '<div class="metric"><div class="label">Weakest Close</div><div class="value red">' + weak + '</div></div>' +
                    '<div class="metric"><div class="label">Average Range</div><div class="value">' + pct(sc.average_range_pct) + '</div></div>' +
                    '<div class="metric"><div class="label">Median Range</div><div class="value">' + pct(sc.median_range_pct) + '</div></div>' +
                    '<div class="metric"><div class="label">Most Common High Hour</div><div class="value">' + hh + '</div></div>' +
                    '<div class="metric"><div class="label">Most Common Low Hour</div><div class="value">' + lh + '</div></div>' +
                    '<div class="metric"><div class="label">High Retested</div><div class="value yellow">' + (sc.high_retest_count ?? 0) + ' stocks</div></div>' +
                '</div>' +
                '<div class="subtitle" id="wl-last-refresh" style="margin-top:8px;"></div>';
        }

        function setLastRefreshIndicator(success) {
            const el = document.getElementById("wl-last-refresh");
            if (!el) return;
            const now = new Date().toLocaleTimeString("en-IN", {hour12: false, timeZone: "Asia/Kolkata"});
            el.textContent = success ? ("Last data refresh: " + now) : ("Refresh failed - displaying last successful snapshot (last attempt: " + now + ")");
            el.className = success ? "subtitle" : "subtitle red";
        }

        async function pollMonitorData() {
            const currentState = {
                search: document.getElementById("wl-search").value,
                sort: document.getElementById("wl-sort").value,
                filter: document.getElementById("wl-filter").value,
            };
            try {
                const resp = await fetch("/api/monitor-data", {cache: "no-store"});
                if (resp.ok === false) throw new Error("HTTP " + resp.status);
                const fresh = await resp.json();
                window.dispatchEvent(new CustomEvent("monitorData", {detail: fresh}));

                WATCHLIST_DATA.length = 0;
                WATCHLIST_DATA.push.apply(WATCHLIST_DATA, fresh.watchlist_symbols || []);

                renderHeaderAndCards(fresh);

                document.getElementById("wl-search").value = currentState.search;
                document.getElementById("wl-sort").value = currentState.sort;
                document.getElementById("wl-filter").value = currentState.filter;

                renderWatchlistTable();
                setLastRefreshIndicator(true);
            } catch (e) {
                setLastRefreshIndicator(false);
                console.warn("Monitor data refresh failed, keeping last successful snapshot:", e);
            }
        }

        restoreControlState();
        document.getElementById("wl-search").addEventListener("input", function() { renderWatchlistTable(); saveControlState(); });
        document.getElementById("wl-sort").addEventListener("change", function() { renderWatchlistTable(); saveControlState(); });
        document.getElementById("wl-filter").addEventListener("change", function() { renderWatchlistTable(); saveControlState(); });
        renderWatchlistTable();
        setLastRefreshIndicator(true);
        setInterval(pollMonitorData, 15000);
        </script>

        {% endif %}
    </div>
"""
