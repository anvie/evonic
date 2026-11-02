// Dashboard lazy loading — fetches data from /api/dashboard/data and populates UI

$(function() {
    loadDashboard();
});

function loadDashboard() {
    $.getJSON('/api/dashboard/data', function(data) {
        renderStats(data.stats);
        renderSecondaryStats(data.skill_stats, data.plugin_stats, data.schedule_stats);
        chartLeaderboard(data.leaderboard);
        renderPluginCards(data.plugin_cards);
        chartModelUsage(data.model_usage);
        renderAttention(data.attention);
    }).fail(function() {
        $('.dash-chart')
            .html('<p class="p-6 text-sm text-red-500 dark:text-red-400">Failed to load data. Please try refreshing the page.</p>');
    });
}

function emptyState(message) {
    return '<div class="p-8 text-center text-sm text-gray-400 dark:text-gray-500">' + message + '</div>';
}

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
}

function truncate(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.substring(0, n) + '…' : s;
}

// ── Hero + compact strip stats ────────────────────────────────────────────────
function renderStats(stats) {
    $('#stat-agent-count').text(stats.agent_count);
    $('#stat-session-count').text(stats.session_count);
    $('#stat-tool-count').text(stats.tool_count);
    $('#stat-active-channel-count').text(stats.active_channel_count);
    $('#stat-channel-count').text(stats.channel_count);
    $('#stat-channel-sub').text(stats.channel_count === 0 ? 'none connected yet' : '\u00a0');
}

function renderSecondaryStats(skillStats, pluginStats, scheduleStats) {
    $('#stat-skill-enabled').text(skillStats.enabled);
    $('#stat-skill-total').text(skillStats.total);
    var setWord = skillStats.skillset_count === 1 ? 'skillset' : 'skillsets';
    $('#stat-skill-skillsets').text(skillStats.skillset_count == null ? '\u00a0' : skillStats.skillset_count + ' ' + setWord);
    $('#stat-plugin-enabled').text(pluginStats.enabled);
    $('#stat-plugin-total').text(pluginStats.total);
    $('#stat-schedule-active').text(scheduleStats.active);
    $('#stat-schedule-total').text(scheduleStats.total);
    $('#stat-schedule-sub').text(scheduleStats.total > scheduleStats.active ? (scheduleStats.total - scheduleStats.active) + ' paused' : '\u00a0');
}

function formatDate(v) {
    if (!v) return '';
    try {
        var d = new Date(typeof v === 'number' ? v * 1000 : v);
        if (isNaN(d.getTime())) return '';
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch (e) { return ''; }
}

// ── Plugin cards (right column; reflows main grid when present) ────────────────
function renderPluginCards(pluginCards) {
    var $container = $('#plugin-cards-container');
    $container.empty();
    if (!pluginCards || pluginCards.length === 0) {
        $container.hide();
        return;
    }

    var cardsHtml = [], tokenIdx = -1;
    for (var i = 0; i < pluginCards.length; i++) {
        var html = '';
        var card = pluginCards[i];
        if (tokenIdx < 0 && /token/i.test(card.title || '')) tokenIdx = i;
        var items = card.items || [];
        var fc = card.feature_card;
        html += '<div class="dash-card">'
              +   '<div class="flex justify-between items-center dash-card-h">'
              +     '<h3 class="text-base font-semibold text-gray-800 dark:text-gray-100 m-0">' + escapeHtml(card.title || 'Plugin Card') + '</h3>'
              +     (card.link ? '<a href="' + escapeHtml(card.link) + '" class="text-xs text-indigo-600 dark:text-indigo-400 hover:underline no-underline">View all</a>' : '')
              +   '</div>';
        if (fc) {
            html += '<div class="dash-feature"><div class="dash-feature-v">' + escapeHtml(fc.count) + '</div>'
                  + (fc.detail ? '<div class="dash-feature-d">' + escapeHtml(fc.detail) + '</div>' : '')
                  + sparkline(fc.series) + '</div>';
        }
        if (items.length === 0) {
            if (!fc) html += emptyState('No items');
        } else {
            html += '<div class="divide-y divide-gray-100 dark:divide-gray-700/50' + (fc ? ' border-t border-gray-100 dark:border-gray-700/50' : '') + '">';
            var maxShow = Math.min(items.length, 5);
            for (var j = 0; j < maxShow; j++) {
                var it = items[j];
                var desc = it.description ? truncate(it.description, 60) : '';
                var created = formatDate(it.created_at);
                html += '<a href="' + escapeHtml(card.link || '#') + '" class="block px-5 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors no-underline text-inherit">'
                      +   '<div class="flex items-start gap-3">'
                      +     '<span class="w-2 h-2 rounded-full bg-amber-400 dark:bg-amber-500 mt-1.5 flex-shrink-0"></span>'
                      +     '<div class="min-w-0">'
                      +       '<div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">' + escapeHtml(it.title || 'Untitled') + '</div>'
                      +       (desc ? '<div class="text-xs text-gray-400 dark:text-gray-500 mt-0.5">' + escapeHtml(desc) + '</div>' : '')
                      +       (created ? '<div class="text-xs text-gray-400 dark:text-gray-500 mt-1">Created ' + created + '</div>' : '')
                      +     '</div>'
                      +   '</div>'
                      + '</a>';
            }
            html += '</div>';
        }
        html += '</div>';
        cardsHtml.push(html);
    }
    // The token card takes the top-right slot; Model distribution moves down to the plugin row.
    if (tokenIdx >= 0) {
        var $donut = $('#model-usage-card').detach();
        $('#top-right').html(cardsHtml[tokenIdx]);
        cardsHtml.splice(tokenIdx, 1);
        $container.html(cardsHtml.join('')).prepend($donut).show();
    } else {
        $container.html(cardsHtml.join('')).show();
    }

}

// ── Sparkline: one bar per bucket (24 hourly buckets for token usage) ─────────
function sparkline(series) {
    if (!series || !series.length || !series.some(function (v) { return v > 0; })) return '';
    var max = Math.max.apply(null, series), n = series.length, w = 100 / n, bars = '';
    for (var i = 0; i < n; i++) {
        var h = Math.max(v2h(series[i], max), series[i] > 0 ? 6 : 2);
        bars += '<rect class="bar' + (i === n - 1 ? ' is-last' : '') + '" x="' + (i * w + w * 0.12).toFixed(2) + '" y="' + (40 - h * 0.4).toFixed(2) + '" width="' + (w * 0.76).toFixed(2) + '" height="' + (h * 0.4).toFixed(2) + '" rx="1.2"/>';
    }
    return '<svg class="dash-spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="Last 24 hours">' + bars + '</svg>'
         + '<div class="dash-spark-axis"><span>24 h ago</span><span>now</span></div>';
}
function v2h(v, max) { return max > 0 ? v / max * 100 : 0; }

// ── Needs attention ───────────────────────────────────────────────────────────
function renderAttention(items) {
    var $sec = $('#attention');
    if (!items || !items.length) { $sec.addClass('hidden'); return; }
    var tone = { danger: 'ag-alert-danger', warn: 'ag-alert-warn', info: 'ag-alert-info' };
    var icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
    $('#attention-count').text(items.length);
    $('#attention-list').html(items.map(function (it) {
        return '<a href="' + escapeHtml(it.href || '#') + '" class="ag-alert ' + (tone[it.level] || tone.warn) + '">'
             +   '<span class="ag-alert-ic">' + icon + '</span>'
             +   '<div class="ag-alert-body"><p class="ag-alert-t">' + escapeHtml(it.title) + '</p>'
             +   (it.detail ? '<p class="ag-alert-s">' + escapeHtml(it.detail) + '</p>' : '') + '</div></a>';
    }).join(''));
    $sec.removeClass('hidden');
}

// ── Charts (ApexCharts) ───────────────────────────────────────────────────────
var CHART_COLORS = ['#6366f1', '#14b8a6', '#f59e0b', '#ec4899', '#0ea5e9', '#84cc16', '#a855f7', '#f97316'];
var _charts = {}, _chartData = {};

function isDark() { return document.documentElement.classList.contains('dark'); }
function chartEmpty(sel, msg) { destroyChart(sel); $(sel).html('<div class="dash-chart-empty">' + msg + '</div>'); }
function destroyChart(sel) { if (_charts[sel]) { try { _charts[sel].destroy(); } catch (e) {} delete _charts[sel]; } }
function mountChart(sel, options) {
    destroyChart(sel);
    var el = document.querySelector(sel);
    if (!el || typeof ApexCharts === 'undefined') { chartEmpty(sel, 'Chart library failed to load'); return; }
    el.innerHTML = '';
    var dark = isDark();
    options.chart = $.extend(true, { fontFamily: 'inherit', background: 'transparent', toolbar: { show: false },
        animations: { enabled: true, easing: 'easeinout', speed: 600 } }, options.chart || {});
    options.theme = { mode: dark ? 'dark' : 'light' };
    options.grid = $.extend({ borderColor: dark ? 'rgba(148,163,184,.18)' : '#e2e8f0', strokeDashArray: 3 }, options.grid || {});
    options.tooltip = $.extend({ theme: dark ? 'dark' : 'light' }, options.tooltip || {});
    var c = new ApexCharts(el, options);
    _charts[sel] = c;
    c.render();
}
// Re-theme charts when the app toggles light/dark
new MutationObserver(function () {
    if (_chartData.usage) chartModelUsage(_chartData.usage);
    if (_chartData.lb) chartLeaderboard(_chartData.lb);
}).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });

// Model distribution: donut + our own legend as one centered group. The long tail beyond
// MODEL_TOP_N is folded into one gray "Others" slice (hover its row to see what is inside).
var MODEL_TOP_N = 7;
function chartModelUsage(rows) {
    destroyChart('#donut-chart');
    if (!rows || !rows.length) return chartEmpty('#chart-model-usage', 'No model usage data yet');
    _chartData.usage = rows;
    var total = rows.reduce(function (n, m) { return n + m.agent_count; }, 0);
    var sorted = rows.slice().sort(function (a, b) { return b.agent_count - a.agent_count || String(a.model).localeCompare(String(b.model)); });
    var fold = sorted.length > MODEL_TOP_N + 1;   // 8 models still get their own color
    var shown = sorted.slice(0, fold ? MODEL_TOP_N : sorted.length), rest = fold ? sorted.slice(MODEL_TOP_N) : [];
    var colors = CHART_COLORS.slice(0, shown.length);
    if (fold) {
        shown.push({ model: 'Others (' + rest.length + ' models)', agent_count: rest.reduce(function (n, m) { return n + m.agent_count; }, 0),
                     _tip: rest.slice(0, 25).map(function (m) { return m.model + ' (' + m.agent_count + ')'; }).join('\n') + (rest.length > 25 ? '\n\u2026' : '') });
        colors.push('#94a3b8');
    }
    var legend = shown.map(function (m, i) {
        var pct = Math.round(m.agent_count / total * 100);
        return '<li><i style="background:' + colors[i] + '"></i>'
             + '<span class="dash-lg-n" title="' + escapeHtml(m._tip || m.model) + '">' + escapeHtml(m.model) + '</span>'
             + '<span class="dash-lg-v">' + m.agent_count + ' <em>' + (pct < 1 ? '<1' : pct) + '%</em></span></li>';
    }).join('');
    $('#chart-model-usage').html('<div class="dash-donut-row"><div id="donut-chart"></div><ul class="dash-donut-legend">' + legend + '</ul></div>');
    mountChart('#donut-chart', {
        chart: { type: 'donut', width: 420, height: 420 },
        series: shown.map(function (m) { return m.agent_count; }),
        labels: shown.map(function (m) { return truncate(m.model, 40); }),
        colors: colors,
        stroke: { width: 2, colors: [isDark() ? '#111827' : '#fff'] },
        dataLabels: { enabled: false },
        legend: { show: false },
        plotOptions: { pie: { donut: { size: '70%', labels: { show: true,
            name: { fontSize: '14px', offsetY: -12 }, value: { fontSize: '44px', fontWeight: 700, offsetY: 10 },
            total: { show: true, label: 'agents', fontSize: '12px', formatter: function () { return total; } } } } } },
        tooltip: { y: { formatter: function (v) { return v + (v === 1 ? ' agent' : ' agents'); } } }
    });
}

// Plain horizontal bars: solid fill, score at the bar end, top model in the strong tone.
function chartLeaderboard(lb) {
    if (!lb || !lb.length) return chartEmpty('#chart-leaderboard', 'No model scores yet');
    _chartData.lb = lb;
    var dark = isDark();
    var scores = lb.map(function (m) { return Math.round(m.best_score * 1000) / 10; });
    var strong = dark ? '#818cf8' : '#4f46e5', soft = dark ? 'rgba(129,140,248,.4)' : 'rgba(99,102,241,.35)';
    mountChart('#chart-leaderboard', {
        chart: { type: 'bar', height: Math.max(150, lb.length * 40 + 36), animations: { enabled: false },
            events: { dataPointSelection: function (e, ctx, o) {
                var m = lb[o.dataPointIndex];
                window.location.href = m && m.best_run_id ? '/history/' + encodeURIComponent(m.best_run_id) : '/history';
            } } },
        series: [{ name: 'Best score', data: scores }],
        colors: [function (o) { return o.dataPointIndex === 0 ? strong : soft; }],
        fill: { type: 'solid', opacity: 1 },
        plotOptions: { bar: { horizontal: true, barHeight: lb.length < 3 ? '28%' : '50%', borderRadius: 4, borderRadiusApplication: 'end', dataLabels: { position: 'top' } } },
        stroke: { show: false },
        dataLabels: { enabled: true, textAnchor: 'start', offsetX: 6, formatter: function (v, o) { var m = lb[o.dataPointIndex]; return v.toFixed(0) + '%  \u00b7  ' + m.run_count + (m.run_count === 1 ? ' run' : ' runs'); },
            style: { fontSize: '12px', fontWeight: 600, colors: [dark ? '#e2e8f0' : '#334155'] }, background: { enabled: false }, dropShadow: { enabled: false } },
        xaxis: { categories: lb.map(function (m, i) { return (i + 1) + '. ' + truncate(m.model_name, 30); }),
            min: 0, max: 125, tickAmount: 5, labels: { formatter: function (v) { return v > 100 ? '' : Math.round(v) + '%'; }, style: { fontSize: '11px' } },
            axisBorder: { show: false }, axisTicks: { show: false }, tooltip: { enabled: false } },
        yaxis: { labels: { maxWidth: 260, style: { fontSize: '12px' } } },
        grid: { xaxis: { lines: { show: true } }, yaxis: { lines: { show: false } }, padding: { right: 8 } },
        legend: { show: false },
        tooltip: { theme: 'dark', shared: false, intersect: true,
            custom: function (o) {
                // Inline styles on purpose: the tooltip must look right even if dashboard.css is cached/stale.
                var i = o.dataPointIndex, m = lb[i];
                var row = 'display:flex;align-items:center;gap:8px;font-size:12px;line-height:1.7;color:#cbd5e1;';
                var dot = function (c) { return '<span style="flex:none;width:8px;height:8px;border-radius:9999px;background:' + c + ';"></span>'; };
                return '<div style="min-width:170px;padding:10px 14px;background:#0f172a;border:1px solid rgba(148,163,184,.25);border-radius:10px;box-shadow:0 10px 30px rgba(2,6,23,.45);font-family:inherit;">'
                     + '<div style="font-size:13px;font-weight:700;color:#fff;margin-bottom:4px;">' + escapeHtml(m.model_name) + '</div>'
                     + '<div style="' + row + '">' + dot(i === 0 ? '#818cf8' : '#a5b4fc') + '<span>Best score</span><b style="margin-left:auto;padding-left:16px;color:#fff;">' + scores[i].toFixed(1) + '%</b></div>'
                     + '<div style="' + row + '">' + dot('#64748b') + '<span>Runs</span><b style="margin-left:auto;padding-left:16px;color:#fff;">' + m.run_count + '</b></div>'
                     + '<div style="' + row + '">' + dot('#64748b') + '<span>Rank</span><b style="margin-left:auto;padding-left:16px;color:#fff;">#' + (i + 1) + '</b></div>'
                     + '</div>';
            } }
    });
}
