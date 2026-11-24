// Test matrix cells: a colored box holding the percentage, with the time under it.
// Shared by /evaluate and /history/<id>. Grey = skipped / error / no result,
// red < 25%, yellow 25-75%, green > 75%.
(function () {
    function secs(ms) { return (ms / 1000).toFixed(1) + 's'; }

    function toneOf(ratio) {
        if (ratio < 0.25) return 'red';
        if (ratio <= 0.75) return 'yellow';
        return 'green';
    }

    // The percentage lives in .mx-pct when the box also holds the time, else in the box itself
    function setBox(box, ratio, label) {
        if (!box) return;
        const text = box.querySelector('.mx-pct') || box;
        if (ratio === null) {
            box.dataset.tone = 'grey';
            text.textContent = label || '-';
        } else {
            box.dataset.tone = toneOf(ratio);
            text.textContent = Math.round(ratio * 100) + '%';
        }
    }

    function isError(d) {
        return !!d && (d.status === 'technical-error' || !!(d.details && d.details.llm_error));
    }

    // A level counts toward the domain percentage only if it produced a real result
    function isCounted(d) {
        return !!d && d.points != null && d.max_points > 0 && !isError(d)
            && d.status !== 'skipped' && d.status !== 'pending' && d.status !== 'running';
    }

    function emptyLabel(d) {
        if (isError(d)) return 'err';
        if (d && d.status === 'skipped') return 'skip';
        return '-';
    }

    function paintDomain(domain, levels) {
        let points = 0, max = 0, passed = 0, tests = 0, ms = 0, counted = 0;
        for (let level = 1; level <= 5; level++) {
            const cell = document.getElementById(`cell-${domain}-${level}`);
            if (!cell) continue;
            const d = levels[level];
            const dur = cell.querySelector('.mx-dur');
            if (d && d.status === 'running') {
                // being tested right now: a pulsing box until the level finishes
                const box = cell.querySelector('.mx-box');
                setBox(box, null, '…');
                if (box) box.dataset.tone = 'running';
            } else if (isCounted(d)) {
                setBox(cell.querySelector('.mx-box'), d.points / d.max_points);
                points += d.points; max += d.max_points; counted++;
            } else {
                setBox(cell.querySelector('.mx-box'), null, emptyLabel(d));
            }
            passed += (d && d.passed) || 0;
            tests += (d && d.count) || 0;
            ms += (d && d.duration_ms) || 0;
            if (dur) dur.textContent = d && d.duration_ms ? secs(d.duration_ms) : '';
            cell.title = d && d.count ? `${d.passed || 0}/${d.count} tests passed` : '';
        }
        const totalBox = document.getElementById(`total-score-${domain}`);
        setBox(totalBox, counted ? points / max : null);
        const totalDur = totalBox && totalBox.querySelector('.mx-dur');
        if (totalDur) totalDur.textContent = ms ? secs(ms) : '';
        const total = document.getElementById(`total-${domain}`);
        if (total) total.title = tests ? `${passed}/${tests} tests passed` + (ms ? ` · ${secs(ms)}` : '') : '';
    }

    function clear(domains) {
        for (const domain of domains) {
            paintDomain(domain, {});
        }
    }

    window.EvMatrix = { paintDomain, clear, toneOf };
})();
