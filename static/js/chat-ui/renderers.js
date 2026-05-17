/**
 * renderers.js — message + timeline + tool renderers, sanitizer, theme tokens.
 *
 * Each renderer takes jQuery context and item data, returns a jQuery element.
 * All user-controlled strings go through $.text() or escape() — never raw interpolation.
 * Markdown output goes through sanitize() before being set via .html().
 */

// ── Sanitizer ─────────────────────────────────────────────────────────────────

const ALLOWED_TAGS = new Set([
    'a','b','i','em','strong','code','pre','blockquote',
    'ul','ol','li','p','br','hr','h1','h2','h3','h4','h5','h6',
    'table','thead','tbody','tr','th','td','span','div','img',
]);
const ALLOWED_ATTRS = {
    a:    ['href', 'title', 'target'],
    code: ['class'],
    pre:  ['class'],
    img:  ['src', 'alt', 'class'],
};

function _walkSanitize(node) {
    const children = [...node.childNodes];
    for (const child of children) {
        if (child.nodeType === Node.ELEMENT_NODE) {
            const tag = child.tagName.toLowerCase();
            if (!ALLOWED_TAGS.has(tag)) {
                // Replace with its children (keep text content)
                while (child.firstChild) node.insertBefore(child.firstChild, child);
                node.removeChild(child);
                continue;
            }
            // Strip disallowed attributes
            const allowed = (ALLOWED_ATTRS[tag] || []).concat(ALLOWED_ATTRS['*'] || []);
            const attrsToRemove = [...child.attributes].filter(a => !allowed.includes(a.name));
            attrsToRemove.forEach(a => child.removeAttribute(a.name));
            // Sanitize hrefs
            if (tag === 'a') {
                const href = child.getAttribute('href') || '';
                if (/^javascript:/i.test(href) || /^data:/i.test(href)) {
                    child.setAttribute('href', '#');
                }
                child.setAttribute('rel', 'noopener noreferrer');
            }
            _walkSanitize(child);
        }
    }
}

export function sanitize(html) {
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    _walkSanitize(tpl.content);
    return tpl.innerHTML;
}

// ── Escaping ──────────────────────────────────────────────────────────────────

function escape(text) {
    const div = document.createElement('div');
    div.textContent = String(text == null ? '' : text);
    return div.innerHTML;
}

function truncateLine(text, max) {
    const first = (text || '').split('\n')[0].trim();
    return first.length > max ? first.slice(0, max) + '\u2026' : first;
}

// ── Syntax highlighters ───────────────────────────────────────────────────────

export function highlightPython(code) {
    if (!code) return '';
    const patterns = [
        { type: 'fstring', regex: /f"(?:[^"\\]|\\.)*"/g },
        { type: 'fstring', regex: /f'(?:[^'\\]|\\.)*'/g },
        { type: 'string', regex: /"""(?:[^"]|\\.)*"""/g },
        { type: 'string', regex: /'''(?:[^']|\\.)*'''/g },
        { type: 'string', regex: /"(?:[^"\\]|\\.)*"/g },
        { type: 'string', regex: /'(?:[^'\\]|\\.)*'/g },
        { type: 'comment', regex: /#.*$/gm },
        { type: 'decorator', regex: /@\w+(?:\.\w+)*/g },
        { type: 'builtin', regex: /\b(print|len|range|list|dict|str|int|float|type|isinstance|enumerate|zip|map|filter|sorted|reversed|open|input|super|set|tuple|abs|max|min|sum|round|any|all|hasattr|getattr|setattr|delattr|callable|repr|format|hash|id|dir|vars|help|slice|staticmethod|classmethod|property|issubclass|iter|next|bin|oct|hex|chr|ord|pow|divmod|compile|eval|exec|globals|locals|breakpoint|memoryview|frozenset|complex|ascii)\b/g },
        { type: 'keyword', regex: /\b(def|class|if|elif|else|for|while|return|import|from|as|try|except|finally|with|raise|pass|break|continue|lambda|yield|assert|del|global|nonlocal|async|await)\b/g },
        { type: 'boolean', regex: /\b(True|False)\b/g },
        { type: 'none', regex: /\bNone\b/g },
        { type: 'self', regex: /\b(self|cls)\b/g },
        { type: 'number', regex: /\b(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)\b/g },
        { type: 'function', regex: /\b([a-zA-Z_]\w*)\s*(?=\()/g },
        { type: 'operator', regex: /(?:==|!=|<=|>=|<<|>>|\*\*|\/\/|&&|\|\||[+\-*/%=<>!&|^~])/g },
    ];
    const matches = [];
    for (const p of patterns) {
        const re = new RegExp(p.regex.source, p.regex.flags);
        let m;
        while ((m = re.exec(code)) !== null) {
            matches.push({ type: p.type, start: m.index, end: m.index + m[0].length, text: m[0] });
            if (m[0].length === 0) re.lastIndex++;
        }
    }
    matches.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
    const filtered = [];
    let lastEnd = -1;
    for (const m of matches) {
        if (m.start >= lastEnd) { filtered.push(m); lastEnd = m.end; }
    }
    let result = '', pos = 0;
    for (const m of filtered) {
        if (m.start > pos) result += escape(code.slice(pos, m.start));
        result += `<span class="hl-${m.type}">${escape(m.text)}</span>`;
        pos = m.end;
    }
    if (pos < code.length) result += escape(code.slice(pos));
    return result;
}

export function highlightDiff(patch) {
    if (!patch) return '';
    return escape(patch).split('\n').map(line => {
        if (line.startsWith('@@'))   return `<span class="hl-diff-header">${line}</span>`;
        if (line.startsWith('--- ') || line.startsWith('+++ ')) return `<span class="hl-diff-filename">${line}</span>`;
        if (line.startsWith('+'))   return `<span class="hl-diff-add">${line}</span>`;
        if (line.startsWith('-'))   return `<span class="hl-diff-remove">${line}</span>`;
        if (line.startsWith('\\')) return `<span class="hl-diff-meta">${line}</span>`;
        return `<span class="hl-diff-context">${line}</span>`;
    }).join('\n');
}

// ── Tool result rendering helpers ─────────────────────────────────────────────

function summarizeToolResult(result) {
    if (result === null || result === undefined) return 'OK';
    if (Array.isArray(result)) return `${result.length} item${result.length !== 1 ? 's' : ''}`;
    if (typeof result === 'object') {
        const keys = Object.keys(result);
        if (!keys.length) return 'OK';
        if ('status' in result) {
            const s = String(result.status);
            return ('message' in result && String(result.message).length < 100)
                ? `${s}: ${String(result.message)}` : s;
        }
        if ('message' in result && keys.length === 1) return String(result.message).slice(0, 120);
        if ('count'   in result && typeof result.count === 'number') return `${result.count} item${result.count !== 1 ? 's' : ''}`;
        const parts = [];
        for (const k of keys.slice(0, 3)) {
            const v = result[k];
            if (v !== null && v !== undefined && typeof v !== 'object') parts.push(`${k}: ${String(v)}`);
        }
        if (parts.length) return parts.join(' · ');
        return `${keys.length} field${keys.length !== 1 ? 's' : ''}`;
    }
    const s = String(result);
    return s.length > 120 ? s.slice(0, 117) + '...' : s;
}

function _renderRunpyResult(r) {
    const hasStdout = r.stdout && r.stdout.trim().length > 0;
    const hasStderr = r.stderr && r.stderr.trim().length > 0;
    const hasError  = r.exit_code !== 0;
    const statusColor = hasError ? 'text-red-600' : 'text-green-600';
    const statusBg    = hasError ? 'bg-red-50 border-red-200' : 'bg-green-50 border-green-200';
    const statusIcon  = hasError ? '&#10060;' : '&#9989;';
    const $wrap = $('<div>');
    const $badge = $(`<div class="flex items-center gap-2 mb-1.5 text-[10px] font-mono border rounded px-2 py-1">`).addClass(statusColor).addClass(statusBg);
    $badge.append(
        $('<span>').html(`${statusIcon} exit: ${escape(String(r.exit_code))}`),
        $('<span class="text-gray-400">').text('|'),
        $('<span>').text(`time: ${r.execution_time}s`)
    );
    if (r.available_helpers) $badge.append($('<span class="text-gray-400">').text('|'), $('<span class="text-gray-500">').text(Object.keys(r.available_helpers).length + ' helpers'));
    $wrap.append($badge);
    if (hasStdout) $wrap.append($('<div class="text-[10px] font-semibold text-gray-500 mb-0.5">').text('stdout'), $('<pre class="text-xs bg-gray-50 border border-gray-200 rounded p-2 overflow-x-auto font-mono text-gray-800 max-h-[200px]">').text(r.stdout));
    if (hasStderr) $wrap.append($('<div class="text-[10px] font-semibold text-red-500 mb-0.5 mt-1">').text('stderr'), $('<pre class="text-xs bg-red-50 border border-red-200 rounded p-2 overflow-x-auto font-mono text-red-700 max-h-[200px]">').text(r.stderr));
    if (!hasStdout && !hasStderr) $wrap.append($('<div class="text-xs text-gray-400 italic">').text('No output'));
    return $wrap;
}

function _renderBashResult(r) {
    let stdout = r.stdout, stderr = r.stderr;
    if (r.data && !stdout && !stderr) {
        try { const p = JSON.parse(r.data); stdout = p.stdout || ''; stderr = p.stderr || ''; } catch(e) {}
    }
    const hasStdout = stdout && stdout.trim().length > 0;
    const hasStderr = stderr && stderr.trim().length > 0;
    const hasError  = r.exit_code !== 0;
    const statusColor = hasError ? 'text-red-600' : 'text-green-600';
    const statusBg    = hasError ? 'bg-red-50 border-red-200' : 'bg-green-50 border-green-200';
    const statusIcon  = hasError ? '&#10060;' : '&#9989;';
    const $wrap = $('<div>');
    const $badge = $(`<div class="flex items-center gap-2 mb-1.5 text-[10px] font-mono border rounded px-2 py-1">`).addClass(statusColor).addClass(statusBg);
    $badge.append(
        $('<span>').html(`${statusIcon} exit: ${escape(String(r.exit_code))}`),
        $('<span class="text-gray-400">').text('|'),
        $('<span>').text(`time: ${r.execution_time}s`)
    );
    $wrap.append($badge);
    if (hasStdout) $wrap.append($('<div class="text-[10px] font-semibold text-gray-500 mb-0.5">').text('stdout'), $('<pre class="text-xs border rounded p-2 overflow-x-auto font-mono max-h-[200px] whitespace-pre-wrap">').css({'background-color':'#0a0b0c','color':'#c8d0d8','border-color':'#1a1b1c'}).text(stdout));
    if (hasStderr) $wrap.append($('<div class="text-[10px] font-semibold text-red-500 mb-0.5 mt-1">').text('stderr'), $('<pre class="text-xs bg-red-50 border border-red-200 rounded p-2 overflow-x-auto font-mono text-red-700 max-h-[200px] whitespace-pre-wrap">').text(stderr));
    if (!hasStdout && !hasStderr) {
        if (r.data && String(r.data).trim().length > 0) {
            $wrap.append($('<pre class="text-xs border rounded p-2 overflow-x-auto font-mono max-h-[200px] whitespace-pre-wrap">').css({'background-color':'#0a0b0c','color':'#c8d0d8','border-color':'#1a1b1c'}).text(String(r.data)));
        } else {
            $wrap.append($('<div class="text-xs text-gray-400 italic">').text('No output'));
        }
    }
    return $wrap;
}

/**
 * Build a jQuery element for the tool result detail section.
 * Returns jQuery element.
 */
export function buildToolResultDetail(ev) {
    if (ev.error) {
        let msg = typeof ev.error === 'string' && ev.error.length > 1 ? ev.error : null;
        if (!msg && typeof ev.result === 'string') msg = ev.result;
        if (!msg && ev.result && typeof ev.result === 'object') msg = ev.result.error || ev.result.message || null;
        if (!msg) msg = 'Tool error';
        return $('<div class="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1 font-mono whitespace-pre-wrap">').text(msg);
    }

    // runpy
    if (ev.tool === 'runpy' && typeof ev.result === 'object' && ev.result && ev.result.exit_code !== undefined) {
        return _renderRunpyResult(ev.result);
    }
    // bash
    if ((ev.tool === 'bash' || ev.result?.exit_code !== undefined) && typeof ev.result === 'object' && ev.result && ev.result.exit_code !== undefined) {
        return _renderBashResult(ev.result);
    }
    // plain string
    if (typeof ev.result === 'string') {
        return $('<pre class="text-xs bg-gray-50 dark:bg-gray-900 dark:text-gray-300 border border-gray-200 rounded p-2 overflow-x-auto font-mono text-gray-700 max-h-[300px] whitespace-pre-wrap">').text(ev.result);
    }
    // single-key data wrapper
    if (typeof ev.result === 'object' && ev.result !== null && Object.keys(ev.result).length === 1 && 'data' in ev.result && typeof ev.result.data === 'string') {
        return $('<pre class="text-xs bg-gray-50 dark:bg-gray-900 dark:text-gray-300 border border-gray-200 rounded p-2 overflow-x-auto font-mono text-gray-700 max-h-[300px] whitespace-pre-wrap">').text(ev.result.data);
    }
    return $('<div class="text-xs text-green-700 bg-green-50 border border-green-200 rounded px-2 py-1">').text(summarizeToolResult(ev.result));
}

// ── Tool call args rendering ───────────────────────────────────────────────────

function _computeLineDiff(oldLines, newLines) {
    const m = oldLines.length, n = newLines.length;
    if (m * n > 60000) return [...oldLines.map(l => ({type:'remove',line:l})), ...newLines.map(l => ({type:'add',line:l}))];
    const dp = Array.from({length: m+1}, () => new Int32Array(n+1));
    for (let i = 1; i <= m; i++) for (let j = 1; j <= n; j++) {
        dp[i][j] = oldLines[i-1] === newLines[j-1] ? dp[i-1][j-1]+1 : Math.max(dp[i-1][j], dp[i][j-1]);
    }
    const ops = []; let i = m, j = n;
    while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && oldLines[i-1] === newLines[j-1]) { ops.push({type:'context', line:oldLines[i-1]}); i--; j--; }
        else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) { ops.push({type:'add', line:newLines[j-1]}); j--; }
        else { ops.push({type:'remove', line:oldLines[i-1]}); i--; }
    }
    return ops.reverse();
}

function _renderStrReplaceDiff(oldStr, newStr, filePath) {
    const oldLines = String(oldStr).split('\n'), newLines = String(newStr).split('\n');
    const ops = _computeLineDiff(oldLines, newLines);
    const changed = ops.map((op,i) => op.type !== 'context' ? i : -1).filter(i => i !== -1);
    if (!changed.length) return $('<div class="text-xs text-gray-400 italic mt-1">').text('No changes detected');
    const CTX = 3;
    const hunks = [];
    let hs = -1, he = -1;
    for (const idx of changed) {
        const lo = Math.max(0, idx-CTX), hi = Math.min(ops.length-1, idx+CTX);
        if (hs === -1 || lo > he+1) { if (hs !== -1) hunks.push([hs,he]); hs=lo; he=hi; }
        else he = Math.max(he, hi);
    }
    if (hs !== -1) hunks.push([hs,he]);

    let html = '';
    if (filePath) html += `<span class="hl-diff-filename">--- ${escape(String(filePath))}</span>\n<span class="hl-diff-filename">+++ ${escape(String(filePath))}</span>\n`;
    for (const [lo, hi] of hunks) {
        let oldLn=1,newLn=1;
        for (let k=0;k<lo;k++) { if(ops[k].type!=='add') oldLn++; if(ops[k].type!=='remove') newLn++; }
        let oldC=0,newC=0;
        for (let k=lo;k<=hi;k++) { if(ops[k].type!=='add') oldC++; if(ops[k].type!=='remove') newC++; }
        html += `<span class="hl-diff-header">@@ -${oldLn},${oldC} +${newLn},${newC} @@</span>\n`;
        for (let k=lo;k<=hi;k++) {
            const {type,line} = ops[k];
            const esc = escape(line);
            if (type==='add') html += `<span class="hl-diff-add">+${esc}</span>\n`;
            else if (type==='remove') html += `<span class="hl-diff-remove">-${esc}</span>\n`;
            else html += `<span class="hl-diff-context"> ${esc}</span>\n`;
        }
    }
    return $('<pre class="diff-code-block mt-0.5" style="max-height:400px;overflow-y:auto">').html(html);
}

function _buildToolCallDetail(tool, args, paramTypes) {
    const pt = paramTypes || {};
    const $wrap = $('<div>');

    const oldKey = 'old_string' in args ? 'old_string' : 'old_str' in args ? 'old_str' : null;
    const newKey = 'new_string' in args ? 'new_string' : 'new_str' in args ? 'new_str' : null;
    if (oldKey && newKey) {
        const filePath = args.file_path || args.path || null;
        const meta = {};
        for (const [k,v] of Object.entries(args)) { if (k!==oldKey && k!==newKey) meta[k]=v; }
        if (Object.keys(meta).length) $wrap.append(_renderParamTable(meta));
        $wrap.append($('<div class="text-[10px] uppercase tracking-wide text-green-500 font-semibold mt-1.5">').text('changes'));
        $wrap.append(_renderStrReplaceDiff(args[oldKey], args[newKey], filePath));
        return $wrap;
    }

    const inlineParams = {}, blockParams = [];
    for (const [key, value] of Object.entries(args)) {
        const view = pt[key] || null;
        if (view) blockParams.push({key, value, view});
        else inlineParams[key] = value;
    }
    if (Object.keys(inlineParams).length) $wrap.append(_renderParamTable(inlineParams));
    for (const {key, value, view} of blockParams) {
        $wrap.append($('<div class="text-[10px] uppercase tracking-wide text-blue-400 font-semibold mt-1.5">').text(key));
        if (view === 'code') $wrap.append($('<pre class="runpy-code-block mt-0.5">').html(highlightPython(String(value))));
        else if (view === 'diff') $wrap.append($('<pre class="diff-code-block mt-0.5">').html(highlightDiff(String(value))));
        else $wrap.append($('<pre class="text-xs bg-gray-50 dark:bg-gray-900 border border-gray-200 rounded p-2 overflow-x-auto font-mono text-gray-700 max-h-[300px] whitespace-pre-wrap">').text(String(value)));
    }
    return $wrap;
}

function _renderParamTable(params) {
    const $grid = $('<div class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs mt-1">');
    for (const [k, v] of Object.entries(params)) {
        const display = v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
        $grid.append(
            $('<span class="text-blue-400 font-semibold truncate">').text(k),
            $('<span class="text-blue-600 break-all">').text(display)
        );
    }
    return $grid;
}

// ── Thinking content ──────────────────────────────────────────────────────────

const THINKING_MAX_LINES = 25;

function _buildThinkingContent(content) {
    const lines = content.split('\n');
    if (lines.length <= THINKING_MAX_LINES) {
        return $('<pre class="p-2 dark:text-purple-300 whitespace-pre-wrap break-words overflow-x-auto max-w-full text-purple-800 text-[10px] leading-relaxed">').text(content);
    }
    const uid = 'tc-' + Math.random().toString(36).substr(2,8);
    const $pre = $('<pre class="whitespace-pre-wrap p-2 dark:text-purple-300 break-words overflow-x-auto max-w-full text-purple-800 text-[10px] leading-relaxed">').attr('id', uid).css({'max-height': THINKING_MAX_LINES*16+'px', overflow:'hidden', position:'relative'});
    $pre.text(lines.slice(0, THINKING_MAX_LINES).join('\n'));
    const remaining = lines.length - THINKING_MAX_LINES;
    const $fade = $('<span class="thinking-trim-fade absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-purple-50 to-transparent flex items-end justify-center pb-1">');
    const $btn = $('<button type="button" class="text-[10px] text-purple-500 hover:text-purple-700 font-medium cursor-pointer">').text(remaining + ' more lines…');
    $btn.on('click', function() {
        const el = document.getElementById(uid);
        if (el) { el.style.maxHeight = 'none'; el.style.overflow = ''; }
        $fade.remove();
    });
    $fade.append($btn);
    $pre.append($fade);
    return $pre;
}

// ── Timeline entry builder ────────────────────────────────────────────────────

/**
 * Build a jQuery timeline-entry element.
 * @param {object} ev      - { type, content?, tool?, args?, param_types?, ... }
 * @param {boolean} isActive - whether to show the active border-spinner
 * @returns {jQuery|null}
 */
export function buildTimelineEntry(ev, isActive) {
    let borderClass, icon, label, labelClass, $summary, $detail, spinnerColor, extraAttrs = {};

    if (ev.type === 'thinking') {
        borderClass = 'border-purple-300'; icon = '&#129504;'; label = 'Thinking'; labelClass = 'text-purple-500'; spinnerColor = '#a855f7';
        $summary = $('<span class="text-[11px] text-gray-400 truncate max-w-[780px]">').text(truncateLine(ev.content, 80));
        $detail = _buildThinkingContent(ev.content);

    } else if (ev.type === 'tool_call') {
        borderClass = 'border-blue-300'; icon = '&#128295;'; label = 'Tool Call'; labelClass = 'text-blue-500'; spinnerColor = '#3b82f6';
        extraAttrs['data-tool-type'] = 'tool_call';
        extraAttrs['data-tool-name'] = ev.tool;
        $summary = $('<span class="text-[11px] text-gray-400 truncate max-w-[780px]">').text(ev.tool + '(' + truncateLine(JSON.stringify(ev.args), 60) + ')');
        $detail = _buildToolCallDetail(ev.tool, ev.args || {}, ev.param_types || {});

    } else if (ev.type === 'response') {
        borderClass = 'border-gray-300'; icon = '&#128172;'; label = 'Response'; labelClass = 'text-gray-500'; spinnerColor = '#6b7280';
        $summary = $('<span class="text-[11px] text-gray-400 truncate max-w-[780px]">').text(truncateLine(ev.content, 80));
        $detail = $('<pre class="whitespace-pre-wrap dark:text-gray-200 break-words overflow-x-auto max-w-full text-[11px] text-gray-700">').text(ev.content);

    } else if (ev.type === 'retry') {
        borderClass = 'border-yellow-300'; icon = '&#128260;'; label = 'Mencoba Ulang'; labelClass = 'text-yellow-600'; spinnerColor = '#f59e0b';
        $summary = $('<span class="text-[11px] text-gray-400 truncate max-w-[780px]">').text(ev.message || `Mencoba ulang... (${ev.retry_count}/${ev.max_retries})`);
        $detail = null;

    } else {
        return null;
    }

    const activeBorder = isActive ? 'border-transparent' : borderClass;
    const $entry = $('<div class="timeline-entry border-l-2 pl-3 py-1 relative">').addClass(activeBorder).attr('data-border', borderClass);
    for (const [k,v] of Object.entries(extraAttrs)) $entry.attr(k, v);

    if (isActive) {
        const $borderSpinner = $('<span class="tl-border-spinner">').append(
            $('<span class="tool-spinner">').css({'border-color': 'rgba(0,0,0,0.08)', 'border-top-color': spinnerColor})
        );
        $entry.append($borderSpinner);
    }

    const $headerRow = $('<div class="flex items-center gap-1 cursor-pointer select-none">');
    const $chev = $('<span class="tl-chev tool-trace-chevron text-[9px] text-gray-300">').html('&#9656;');
    const $iconSpan = $('<span class="text-[10px] font-semibold">').addClass(labelClass).html(icon);
    $headerRow.append($chev, $iconSpan);

    if (ev.type === 'tool_call') {
        $headerRow.append($('<span class="tl-status inline-flex items-center">'));
    }
    $headerRow.append($summary);

    const $detailWrap = $('<div class="tl-detail ml-5 hidden mt-1 overflow-x-hidden max-w-full">');
    if ($detail) $detailWrap.append($detail);

    $headerRow.on('click', function() {
        $detailWrap.toggleClass('hidden');
        $chev.toggleClass('rotated');
    });

    $entry.append($headerRow, $detailWrap);
    return $entry;
}

// ── Message bubble builders ───────────────────────────────────────────────────

const SYSTEM_BALLOON_TOGGLE = 'toggleSysBalloon'; // global fn exposed by index.js

function _buildSysBalloon(tag, content, tagColorClass, fullColorClass, truncateLen) {
    const previewText = content.replace(/\n/g, ' ').trim();
    const truncated = previewText.length > truncateLen ? previewText.substring(0, truncateLen) + '\u2026' : previewText;
    const needsCollapse = previewText.length > truncateLen;
    const sysId = 'sys-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);

    const $balloon = $('<div class="sys-balloon">').attr('data-sys-id', sysId);
    const $header = $('<div class="sys-balloon-header cursor-pointer flex items-start gap-1.5 whitespace-pre-wrap">');
    const $tagSpan = $('<span class="text-xs font-semibold mr-1.5">').addClass(tagColorClass).text(tag);
    const $preview = $('<span class="sys-balloon-content block">').append($tagSpan, document.createTextNode(truncated));
    $header.append($preview);
    if (needsCollapse) $header.append($('<span class="sys-chevron text-[10px] flex-shrink-0 mt-0.5">').addClass(fullColorClass).html('&#9660;'));

    const $full = $('<div class="sys-balloon-full whitespace-pre-wrap">').css('display','none').append(
        $('<span class="text-xs font-semibold mr-1.5">').addClass(tagColorClass).text(tag),
        document.createTextNode(content)
    );

    $header.on('click', () => {
        if (window.toggleSysBalloon) window.toggleSysBalloon(sysId);
    });

    $balloon.append($header, $full);
    return $balloon;
}

/**
 * Build a message bubble jQuery element.
 * @returns {jQuery}  the wrapper div with data-msg-role
 */
export function buildMessageBubble(role, content, opts = {}, cfg = {}) {
    const {
        userAlign = 'right',
        assistantAlign = 'left',
        userBubbleClass = 'bg-indigo-500 text-white dark:bg-indigo-800',
        assistantBubbleClass = 'bg-gray-200 text-gray-800 dark:bg-[#152A3A] border dark:border-[#1B394F] dark:text-gray-100',
        agentAvatarUrl = null,
        showTimestamps = false,
        formatTimestamp = null,
    } = cfg;

    const isUser      = role === 'user';
    const isError     = role === 'error';
    const isSystem    = !isUser && !isError && role !== 'assistant' && /^\[system/i.test(content);
    const isSystemUser = isUser && /^\[system(?:\/[^\]]*)?\]/i.test(content);
    const isAgentUser  = isUser && /^\[AGENT\/[^\]]+\]/i.test(content);

    // In flex-col (mobile) mode the cross axis is horizontal, so use items-end/start.
    // In md:flex-row (desktop) mode the main axis is horizontal, so use justify-end/start.
    const isRight = isUser ? (userAlign === 'right') : (assistantAlign === 'right');
    const alignClass = isRight
        ? 'items-end md:items-start md:justify-end'
        : 'items-start md:justify-start';

    const avatarHtml = (!isUser && !isError && agentAvatarUrl)
        ? `<img src="${escape(agentAvatarUrl)}" alt="" class="w-7 h-7 rounded-full object-cover flex-shrink-0 mt-1 bg-indigo-50 dark:bg-indigo-900/20" onerror="this.onerror=null;this.style.display='none'">`
        : '';

    const $wrapper = $('<div>').addClass('flex flex-col md:flex-row').addClass(alignClass).attr('data-msg-role', role);
    if (avatarHtml) $wrapper.addClass('items-start gap-2').append($(avatarHtml));

    let $bubble;

    if (isAgentUser) {
        const agentMatch = content.match(/^(\[AGENT\/[^\]]+\])\s*/i);
        const agentTag = agentMatch ? agentMatch[1] : '';
        const agentContent = agentTag ? content.slice(agentMatch[0].length) : content;
        $bubble = $('<div class="bg-blue-100 text-blue-900 border border-blue-300 rounded-2xl px-4 py-2.5 text-sm break-words">');
        if (agentTag) $bubble.append($('<span class="text-xs font-semibold text-blue-500 mr-1.5">').text(agentTag));
        $bubble.append(document.createTextNode(agentContent));

    } else if (isSystemUser) {
        const sysMatch = content.match(/^(\[(?:SYSTEM(?:\/[^\]]*)?|System\/[^\]]*)\])\s*/);
        const sysTag = sysMatch ? sysMatch[1] : '';
        const sysContent = sysTag ? content.slice(sysMatch[0].length) : content;
        $bubble = $('<div class="bg-orange-100 text-orange-900 border border-orange-300 rounded-2xl px-4 py-2.5 text-sm break-words">');
        $bubble.append(_buildSysBalloon(sysTag, sysContent, 'text-orange-500', 'text-orange-400', 120));

    } else if (isUser) {
        $bubble = $('<div class="rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap break-words">').addClass(userBubbleClass);
        // Render image attachment if present
        const meta = opts.metadata || {};
        if (meta.image_url) {
            const $img = $('<img>').attr('src', meta.image_url)
                .addClass('max-w-[240px] max-h-[240px] rounded-lg mb-1 cursor-pointer')
                .on('click', function() { window.open(meta.image_url, '_blank'); });
            $bubble.append($img);
        }
        // Render non-image file badge
        if (meta.attachment_info && !meta.attachment_info.is_image) {
            const info = meta.attachment_info;
            const $badge = $('<div class="flex items-center gap-1.5 mb-1 px-2 py-1 bg-white/20 rounded text-xs">')
                .append($('<svg class="w-3.5 h-3.5 flex-shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor"><path d="M3 3.5A1.5 1.5 0 0 1 4.5 2h6.879a1.5 1.5 0 0 1 1.06.44l4.122 4.12A1.5 1.5 0 0 1 17 7.622V16.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 16.5v-13Z"/></svg>'))
                .append($('<span class="truncate">').text(info.filename));
            $bubble.append($badge);
        }
        if (content) $bubble.append(document.createTextNode(content));

    } else if (isError) {
        const $icon = $('<svg class="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-8-5a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 10 5Zm0 10a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clip-rule="evenodd"/></svg>');
        $bubble = $('<div class="bg-red-50 text-red-700 border border-red-200 rounded-lg px-4 py-2 text-sm flex items-start gap-2">').append($icon, $('<span>').text(content));

    } else if (isSystem) {
        const sysMatch = content.match(/^\[system[^\]]*\]\s*/i);
        const sysTag = sysMatch ? sysMatch[0].trim() : '[SYSTEM]';
        const sysContent = content.replace(/^\[system[^\]]*\]\s*/i, '');
        $bubble = $('<div class="bg-orange-200 text-gray-600 border-gray-400 rounded-2xl px-4 py-2.5 text-sm break-words">');
        $bubble.append(_buildSysBalloon(sysTag, sysContent, 'text-gray-500', 'text-gray-400', 120));

    } else {
        // assistant: markdown with sanitizer
        const rendered = typeof marked !== 'undefined'
            ? sanitize(marked.parse(content || '')).replace(/<table/g, '<div class="table-wrapper"><table').replace(/<\/table>/g, '</table></div>')
            : escape(content);
        $bubble = $('<div class="chat-prose rounded-2xl px-4 py-2.5 border-gray-300 text-sm break-words">').addClass(assistantBubbleClass);
        $bubble.attr('role', 'article');
        $bubble.html(rendered);
    }

    const $inner = $('<div class="max-w-[80%] min-w-0">').append($bubble);

    if (showTimestamps && opts.timestamp) {
        let tsStr = '';
        try {
            tsStr = formatTimestamp
                ? formatTimestamp(opts.timestamp)
                : new Date(opts.timestamp).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        } catch(e) {}
        if (tsStr) {
            const tsAlign = isUser
                ? (userAlign === 'left' ? 'text-left' : 'text-right')
                : (assistantAlign === 'left' ? 'text-left' : 'text-right');
            $inner.append($('<div class="text-[10px] text-gray-300 mt-0.5 px-1">').addClass(tsAlign).text(tsStr));
        }
    }

    $wrapper.append($inner);
    return $wrapper;
}

// ── Default renderer registry ─────────────────────────────────────────────────

export const DEFAULT_RENDERERS = {
    buildTimelineEntry,
    buildToolResultDetail,
    buildMessageBubble,
    sanitize,
    highlightPython,
    highlightDiff,
};
