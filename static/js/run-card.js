// One self-contained card for a finished run: who, how many points, how each domain did,
// the written summary and the speed numbers. Built to be screenshotted and shared as is.
(function () {
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function fmtPts(n) { return String(Math.round(n * 10) / 10); }

    function fmtDate(value) {
        if (!value) return '';
        var d = new Date(String(value).replace(' ', 'T'));
        return isNaN(d) ? '' : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
    }

    // Attainment only picks the verdict; the card never prints it
    function verdictOf(ratio) {
        if (ratio >= 0.8) return { key: 'good', label: 'Excellent' };
        if (ratio >= 0.6) return { key: 'ok', label: 'Good' };
        return { key: 'low', label: 'Needs improvement' };
    }

    function render(data) {
        var domains = data.by_domain || {};
        var names = data.domain_names || {};
        var total = data.total_points || 0;
        var ratio = data.max_points ? total / data.max_points : 0;
        var verdict = verdictOf(ratio);

        var passed = 0, tests = 0;
        Object.keys(domains).forEach(function (d) { passed += domains[d].passed; tests += domains[d].count; });

        // ring: circumference of r=42 is 263.9
        var dash = (263.9 * Math.min(1, ratio)).toFixed(1);

        var stats = [];
        if (data.total_tokens) stats.push(['Tokens', data.total_tokens.toLocaleString(), '']);
        if (data.tok_per_sec) stats.push(['Speed', data.tok_per_sec, 'tok/s']);
        if (data.total_duration_ms) stats.push(['Duration', (data.total_duration_ms / 1000).toFixed(1), 's']);
        var statsHtml = stats.map(function (s) {
            return '<div class="rc-stat"><span class="rc-label">' + s[0] + '</span><span class="rc-stat-value">' +
                esc(s[1]) + (s[2] ? '<small>' + s[2] + '</small>' : '') + '</span></div>';
        }).join('');

        // heatmap: one row per domain (percentage box + name), one square per test; same four tones as the matrix
        var testList = data.tests || [];
        function dotOf(t) {
            var skipped = t.st === 'skipped', failed = !!t.e;
            var score = t.s || 0;
            var tone = skipped || failed ? 'grey' : EvMatrix.toneOf(score);
            var result = failed ? 'error' : skipped ? 'skipped' : Math.round(score * 100) + '%';
            var tip = (names[t.d] || t.d) + ' L' + t.l + ' · ' + t.id + ' · ' + result;
            return '<span class="rc-dot" data-tone="' + tone + '" title="' + esc(tip) + '"></span>';
        }
        var rows = Object.keys(domains).map(function (d) {
            var v = domains[d];
            var r = v.max_points ? v.points / v.max_points : 0;
            var dots = testList.filter(function (t) { return t.d === d; }).map(dotOf).join('');
            return '<div class="rc-hrow"><div class="rc-hlabel"><span class="mx-box" data-tone="' + EvMatrix.toneOf(r) + '">' +
                Math.round(r * 100) + '%</span><span class="rc-domain-name" title="' + esc(names[d] || d) + '">' +
                esc(names[d] || d.replace(/_/g, ' ')) + '</span></div><div class="rc-dots">' + dots + '</div></div>';
        }).join('');
        var legend = [['grey', 'skipped / error'], ['red', '< 25%'], ['yellow', '25–75%'], ['green', '> 75%']].map(function (l) {
            return '<span class="rc-key"><span class="rc-dot" data-tone="' + l[0] + '"></span>' + l[1] + '</span>';
        }).join('');
        var heat = testList.length ? '<div class="rc-heat">' + rows + '<div class="rc-legend">' + legend + '</div></div>' : '';

        var date = fmtDate(data.completed_at);
        return '<div class="rc rc-' + verdict.key + '">' +
            '<div class="rc-top"><div class="rc-who"><span class="rc-model" title="' + esc(data.model_name) + '">' + esc(data.model_name || 'default') +
            '</span><span class="rc-meta">' + [data.run_id ? 'Run #' + esc(data.run_id) : '', date].filter(Boolean).join(' · ') + '</span></div>' +
            '<span class="rc-verdict">' + verdict.label + '</span></div>' +
            '<div class="rc-hero"><div><span class="rc-label">Total points</span><span class="rc-points">' + fmtPts(data.score || 0) + '</span></div>' +
            '<div class="rc-ring"><svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="42" class="rc-ring-bg"/>' +
            '<circle cx="50" cy="50" r="42" class="rc-ring-fg" stroke-dasharray="' + dash + ' 263.9"/></svg>' +
            '<span class="rc-ring-text"><b>' + passed + '/' + tests + '</b><small>passed</small></span></div></div>' +
            (data.summary ? '<p class="rc-summary">' + esc(data.summary) + '</p>' : '') +
            (statsHtml ? '<div class="rc-stats">' + statsHtml + '</div>' : '') +
            heat +
            '</div>';
    }

    window.EvRunCard = { render: render };
})();
