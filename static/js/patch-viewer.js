/**
 * patch-viewer.js — renders unified diffs / git patches (.patch, .diff) as
 * per-file cards with old/new line-number gutters, red/green row bands and
 * word-level change highlighting. Styles live in css/patch-viewer.css.
 *
 *   EvPatchViewer.render(text) -> HTML string (all patch content is escaped)
 */
(function () {
    const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s?(.*)$/;

    function esc(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function stripPrefix(path) {
        return path.replace(/^"|"$/g, '').replace(/^[ab]\//, '');
    }

    // Splits patch text into { preamble, files: [{ name, meta, hunks, add, del }] }.
    function parse(text) {
        const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
        if (lines.length && lines[lines.length - 1] === '') lines.pop();

        const preamble = [];
        const files = [];
        let file = null;
        let hunk = null;
        let oldRem = 0, newRem = 0, oldNo = 0, newNo = 0;

        const startFile = (name) => {
            file = { name: name || '', meta: [], hunks: [], add: 0, del: 0 };
            files.push(file);
            hunk = null;
        };

        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];

            if (hunk && (oldRem > 0 || newRem > 0)) {
                const c = line.charAt(0);
                if (c === '\\') { hunk.rows.push({ type: 'meta', text: line }); continue; }
                if (c === '+') {
                    hunk.rows.push({ type: 'add', text: line.slice(1), newNo: newNo++ });
                    newRem--; file.add++; continue;
                }
                if (c === '-') {
                    hunk.rows.push({ type: 'del', text: line.slice(1), oldNo: oldNo++ });
                    oldRem--; file.del++; continue;
                }
                if (c === ' ' || line === '') {
                    hunk.rows.push({ type: 'ctx', text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
                    oldRem--; newRem--; continue;
                }
                oldRem = newRem = 0; // malformed counts: fall through and re-read the line
            }
            if (hunk && line.charAt(0) === '\\') { hunk.rows.push({ type: 'meta', text: line }); continue; }

            const m = HUNK_RE.exec(line);
            if (m && file) {
                oldNo = parseInt(m[1], 10);
                newNo = parseInt(m[3], 10);
                oldRem = m[2] === undefined ? 1 : parseInt(m[2], 10);
                newRem = m[4] === undefined ? 1 : parseInt(m[4], 10);
                hunk = { header: line, rows: [] };
                file.hunks.push(hunk);
                continue;
            }

            const g = /^diff --git (?:"?a\/)?(.+?)"? "?b\/(.+?)"?$/.exec(line);
            if (g) { startFile(g[2]); continue; }

            if (line.startsWith('--- ') && !(file && file.hunks.length === 0 && file.meta.length)) {
                // plain unified diff without a "diff --git" line
                if (!file || file.hunks.length) startFile('');
            }
            if (line.startsWith('--- ') || line.startsWith('+++ ')) {
                if (!file) startFile('');
                const p = line.slice(4).split('\t')[0];
                if (p !== '/dev/null' && (line.startsWith('+++ ') || !file.name)) file.name = stripPrefix(p);
                continue;
            }

            if (file) file.meta.push(line);
            else preamble.push(line);
        }
        return { preamble, files };
    }

    function tokens(text) {
        return text.match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) || [];
    }

    // Word-level diff between two lines -> [oldHtml, newHtml].
    function intraline(oldText, newText) {
        const a = tokens(oldText), b = tokens(newText);
        if (a.length * b.length > 60000) return [esc(oldText), esc(newText)];
        const dp = Array.from({ length: a.length + 1 }, () => new Int32Array(b.length + 1));
        for (let i = 1; i <= a.length; i++) {
            for (let j = 1; j <= b.length; j++) {
                dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
            }
        }
        const keepA = new Set(), keepB = new Set();
        for (let i = a.length, j = b.length; i > 0 && j > 0;) {
            if (a[i - 1] === b[j - 1]) { keepA.add(--i); keepB.add(--j); }
            else if (dp[i - 1][j] >= dp[i][j - 1]) i--;
            else j--;
        }
        const out = (toks, keep) => toks.map((t, k) =>
            keep.has(k) ? esc(t) : `<mark class="pv-chg">${esc(t)}</mark>`).join('');
        return [out(a, keepA), out(b, keepB)];
    }

    function row(type, oldNo, newNo, sign, html) {
        return `<tr class="pv-${type}"><td class="pv-ln">${oldNo == null ? '' : oldNo}</td>` +
            `<td class="pv-ln">${newNo == null ? '' : newNo}</td>` +
            `<td class="pv-sign">${sign}</td><td class="pv-code">${html || ' '}</td></tr>`;
    }

    function renderHunk(hunk) {
        const m = HUNK_RE.exec(hunk.header);
        const label = m ? `@@ -${m[1]}${m[2] ? ',' + m[2] : ''} +${m[3]}${m[4] ? ',' + m[4] : ''} @@` : hunk.header;
        const ctx = m && m[5] ? `<span class="pv-hunk-ctx">${esc(m[5])}</span>` : '';
        const out = [`<tr class="pv-hunk"><td colspan="4"><span class="pv-hunk-range">${esc(label)}</span>${ctx}</td></tr>`];

        const rows = hunk.rows;
        for (let i = 0; i < rows.length;) {
            if (rows[i].type === 'del') {
                const dels = [], adds = [];
                while (i < rows.length && rows[i].type === 'del') dels.push(rows[i++]);
                while (i < rows.length && rows[i].type === 'add') adds.push(rows[i++]);
                const pairs = Math.min(dels.length, adds.length);
                const delHtml = [], addHtml = [];
                for (let k = 0; k < dels.length; k++) {
                    delHtml.push(k < pairs ? intraline(dels[k].text, adds[k].text)[0] : esc(dels[k].text));
                }
                for (let k = 0; k < adds.length; k++) {
                    addHtml.push(k < pairs ? intraline(dels[k].text, adds[k].text)[1] : esc(adds[k].text));
                }
                dels.forEach((r, k) => out.push(row('del', r.oldNo, null, '−', delHtml[k])));
                adds.forEach((r, k) => out.push(row('add', null, r.newNo, '+', addHtml[k])));
                continue;
            }
            const r = rows[i++];
            if (r.type === 'add') out.push(row('add', null, r.newNo, '+', esc(r.text)));
            else if (r.type === 'ctx') out.push(row('ctx', r.oldNo, r.newNo, '', esc(r.text)));
            else out.push(`<tr class="pv-note"><td colspan="4">${esc(r.text)}</td></tr>`);
        }
        return out.join('');
    }

    function render(text) {
        const { preamble, files } = parse(text);
        if (!files.length) return `<pre class="pv-plain">${esc(text)}</pre>`;

        let html = '<div class="pv">';
        const pre = preamble.join('\n').trim();
        if (pre) html += `<pre class="pv-preamble">${esc(pre)}</pre>`;

        const total = files.reduce((s, f) => [s[0] + f.add, s[1] + f.del], [0, 0]);
        if (files.length > 1) {
            html += `<div class="pv-summary">${files.length} files changed ` +
                `<b class="pv-stat-add">+${total[0]}</b> <b class="pv-stat-del">−${total[1]}</b></div>`;
        }
        for (const f of files) {
            const meta = f.meta.filter(l => l.trim()).join('\n');
            html += '<div class="pv-file"><div class="pv-file-head">' +
                `<span class="pv-fname" title="${esc(f.name)}">${esc(f.name || '(unnamed)')}</span>` +
                `<span class="pv-stat"><b class="pv-stat-add">+${f.add}</b><b class="pv-stat-del">−${f.del}</b></span></div>`;
            if (meta) html += `<div class="pv-meta">${esc(meta)}</div>`;
            if (f.hunks.length) {
                html += '<div class="pv-scroll"><table class="pv-table"><colgroup>' +
                    '<col class="pv-col-ln"><col class="pv-col-ln"><col class="pv-col-sign"><col></colgroup><tbody>' +
                    f.hunks.map(renderHunk).join('') + '</tbody></table></div>';
            }
            html += '</div>';
        }
        return html + '</div>';
    }

    window.EvPatchViewer = { render, parse };
})();
