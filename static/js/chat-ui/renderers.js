/**
 * renderers.js — message + timeline + tool renderers, sanitizer, theme tokens.
 *
 * Each renderer takes jQuery context and item data, returns a jQuery element.
 * All user-controlled strings go through $.text() or escape() — never raw interpolation.
 * Markdown output goes through sanitize() before being set via .html().
 */

import { Lightbox } from './lightbox.js';
import { setupImageForLazy, createLazyImage, retrofitImageForLazy } from './lazy-image.js';
import { buildAttachmentCard } from './artifacts.js';

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
    span: ['class', 'style', 'aria-hidden'],
    img:  ['src', 'alt', 'class', 'loading'],
    ol:   ['start'],
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
            // Sanitize hrefs / image sources: only web URLs, mailto:, anchors,
            // and same-origin /api/ paths are user-accessible. Local filesystem
            // paths and custom URI schemes (file:, sandbox:, ...) render as
            // broken links in the browser, so they are neutralized here.
            if (tag === 'a') {
                const href = child.getAttribute('href') || '';
                if (!/^(https?:\/\/|mailto:|#|\/api\/)/i.test(href)) {
                    child.setAttribute('href', '#');
                    child.setAttribute('title', 'Local path — not accessible via link');
                }
                child.setAttribute('rel', 'noopener noreferrer');
                // Open external links in a new tab
                if (/^https?:\/\//i.test(href)) {
                    child.setAttribute('target', '_blank');
                }
            }
            if (tag === 'img') {
                const src = child.getAttribute('src') || '';
                if (!/^(https?:\/\/|\/api\/|data:image\/)/i.test(src)) {
                    // Neutralize unsafe image sources (local paths, file:/sandbox:)
                    child.removeAttribute('src');
                    const alt = child.getAttribute('alt') || '';
                    child.setAttribute('alt', alt ? alt + ' (unavailable)' : '(unavailable)');
                }
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

function normalizeTimestamp(timestamp) {
    if (timestamp == null || timestamp === '') return null;
    let value = timestamp;
    if (typeof value === 'string') {
        const trimmed = value.trim();
        if (!trimmed) return null;
        if (/^\d+(?:\.\d+)?$/.test(trimmed)) value = Number(trimmed);
        else value = trimmed;
    }
    if (typeof value === 'number' && Number.isFinite(value) && Math.abs(value) < 1e12) value *= 1000;
    const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
}

// -- KaTeX math rendering ----------------------------------------------------------

// Renders LaTeX math delimiters ($...$ and $$...$$) in HTML content.
// Runs after marked.parse() and sanitize(), before DOM insertion.
// Protects code blocks (<pre><code>) from accidental math rendering.
function renderMath(html) {
    if (typeof katex === 'undefined') return html;
    try {
        return _renderMathImpl(html);
    } catch (e) {
        // Never let math rendering break the message pipeline
        return html;
    }
}

function _renderMathImpl(html) {

    const codeBlocks = [];
    // Protect <pre><code> blocks
    let safe = (html || '').replace(
        /<pre\b[^>]*>[\s\S]*?<\/pre>/gi,
        function (match) {
            codeBlocks.push(match);
            return '\u0000CODE' + (codeBlocks.length - 1) + '\u0000';
        }
    );

    // Protect inline <code> blocks
    safe = safe.replace(
        /<code\b[^>]*>[\s\S]*?<\/code>/gi,
        function (match) {
            codeBlocks.push(match);
            return '\u0000CODE' + (codeBlocks.length - 1) + '\u0000';
        }
    );

    // Render display math $$...$$ first
    safe = safe.replace(/\$\$([\s\S]*?)\$\$/g, function (match, formula) {
        try {
            return katex.renderToString(formula, { displayMode: true, throwOnError: false });
        } catch (e) {
            return match;
        }
    });

    // Render inline math $...$ (must not be preceded or followed by $)
    safe = safe.replace(/(?<!\$)\$(?!\$)([\s\S]*?)(?<!\$)\$(?!\$)/g, function (match, formula) {
        try {
            return katex.renderToString(formula, { displayMode: false, throwOnError: false });
        } catch (e) {
            return match;
        }
    });

    // Restore code blocks
    safe = safe.replace(/\u0000CODE(\d+)\u0000/g, function (match, idx) {
        return codeBlocks[parseInt(idx, 10)] || match;
    });

    return safe;
}

function truncateLine(text, max) {
    const first = (text || '').split('\n')[0].trim();
    return first.length > max ? first.slice(0, max) + '\u2026' : first;
}

// \u2500\u2500 Wiki-link transform \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

function transformWikiLinks(html) {
    return (html || '').replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (m, target, display) => {
        const t = (target || '').trim();
        const label = ((display || target) || '').trim();
        const attr = escape(t).replace(/"/g, '&quot;');
        return '<a href="#" class="kb-wikilink text-indigo-600 dark:text-indigo-400 font-medium no-underline hover:underline" data-wikilink="' + attr + '">' + escape(label) + '</a>';
    });
}

// \u2500\u2500 KB doc preview rendering \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

function kbSplitFrontmatter(content) {
    const m = (content || '').match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
    if (!m) return { fm: null, body: content || '' };
    return { fm: m[1], body: m[2] };
}

function kbParseFM(raw) {
    const fm = {};
    (raw || '').split('\n').forEach(function (line) {
        const i = line.indexOf(':');
        if (i < 0) return;
        const k = line.slice(0, i).trim();
        if (!k) return;
        let v = line.slice(i + 1).trim();
        if (v.startsWith('[') && v.endsWith(']')) {
            v = v.slice(1, -1).split(',').map(function (s) { return s.trim().replace(/^["']|["']$/g, ''); }).filter(Boolean);
        } else {
            v = v.replace(/^["']|["']$/g, '');
        }
        fm[k] = v;
    });
    return fm;
}

function kbRenderFrontmatter(raw) {
    const fm = kbParseFM(raw);
    const asList = function (v) { return Array.isArray(v) ? v : (v ? [v] : []); };
    const tags = asList(fm.tags), aliases = asList(fm.aliases);
    const known = new Set(['title', 'type', 'description', 'tags', 'aliases', 'created', 'updated']);
    let h = '<div class="mb-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-4">';
    h += '<div class="flex items-center gap-2 flex-wrap mb-1">';
    if (fm.type) h += '<span class="text-xs font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300">' + escape(fm.type) + '</span>';
    if (fm.title) h += '<span class="text-base font-semibold text-gray-800 dark:text-gray-100">' + escape(fm.title) + '</span>';
    h += '</div>';
    if (fm.description) h += '<p class="text-sm text-gray-600 dark:text-gray-300 mt-1 mb-2">' + escape(fm.description) + '</p>';
    if (tags.length) h += '<div class="flex flex-wrap gap-1 mb-2">' + tags.map(function (t) { return '<span class="text-xs px-2 py-0.5 rounded bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300">#' + escape(t) + '</span>'; }).join('') + '</div>';
    if (aliases.length) h += '<div class="text-xs text-gray-500 dark:text-gray-400 mb-1">aliases: ' + aliases.map(escape).join(', ') + '</div>';
    const meta = [];
    if (fm.created) meta.push('created ' + escape(String(fm.created)));
    if (fm.updated) meta.push('updated ' + escape(String(fm.updated)));
    Object.keys(fm).forEach(function (k) {
        if (!known.has(k)) meta.push(escape(k) + ': ' + escape(Array.isArray(fm[k]) ? fm[k].join(', ') : fm[k]));
    });
    if (meta.length) h += '<div class="text-xs text-gray-400 dark:text-gray-500">' + meta.join(' &middot; ') + '</div>';
    h += '</div>';
    return h;
}

function renderKBDocPreview(content) {
    const parts = kbSplitFrontmatter(content);
    const cardHtml = parts.fm !== null ? kbRenderFrontmatter(parts.fm) : '';
    const bodyHtml = typeof marked !== 'undefined'
        ? transformWikiLinks(sanitize(renderMath(marked.parse(parts.body))))
        : escape(parts.body);
    return cardHtml + bodyHtml;
}

// \u2500\u2500 Wiki-link preview modal \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

var _wikilinkHistory = [];

function _getOrCreateWikilinkModal() {
    let $modal = $('#wikilink-preview-modal');
    if ($modal.length) return $modal;

    $modal = $([
        '<div id="wikilink-preview-modal" class="hidden fixed inset-0 bg-black/50 flex items-center justify-center z-[150] p-4">',
        '  <div class="wikilink-modal-dialog bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-[700px] flex flex-col overflow-hidden" style="max-height:85vh;">',
        '    <div class="flex items-center justify-between px-5 py-3 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">',
        '      <div class="flex items-center gap-2 min-w-0">',
        '        <button class="wikilink-modal-back hidden text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 flex-shrink-0" aria-label="Back">',
        '          <svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/></svg>',
        '        </button>',
        '        <h3 class="wikilink-modal-title text-sm font-semibold text-gray-800 dark:text-gray-200 truncate"></h3>',
        '      </div>',
        '      <button class="wikilink-modal-close text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 flex-shrink-0" aria-label="Close">&times;</button>',
        '    </div>',
        '    <div class="wikilink-modal-body prose dark:prose-invert text-sm max-w-none p-5 overflow-y-auto flex-1" style="max-height:70vh;"></div>',
        '    <div class="px-5 py-3 border-t border-gray-200 dark:border-gray-700 flex justify-end flex-shrink-0">',
        '      <button class="wikilink-modal-close px-4 py-1.5 text-xs font-medium text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 rounded-lg">Close</button>',
        '    </div>',
        '  </div>',
        '</div>',
    ].join('\n'));

    function _closeModal() { $modal.addClass('hidden'); _wikilinkHistory = []; }

    $modal.on('click', function (e) { if (e.target === this) _closeModal(); });
    $modal.find('.wikilink-modal-dialog').on('click', '.wikilink-modal-close', function () { _closeModal(); });
    $modal.find('.wikilink-modal-dialog').on('click', '.wikilink-modal-back', function () {
        if (_wikilinkHistory.length) {
            var prev = _wikilinkHistory.pop();
            _renderWikilinkModal(prev.title, prev.html, false);
        }
    });
    $modal.find('.wikilink-modal-dialog').on('click', '.kb-wikilink', function (e) {
        e.preventDefault();
        var t = $(this).attr('data-wikilink');
        if (t) document.dispatchEvent(new CustomEvent('evonic:wikilink-click', { detail: { title: t, _fromModal: true } }));
    });
    $modal.find('.wikilink-modal-body').on('click', 'a[href^="http://"], a[href^="https://"]', function (e) {
        e.preventDefault();
        window.open(this.href, '_blank', 'noopener,noreferrer');
    });
    $(document).on('keydown.wikilinkModal', function (e) {
        if (e.key === 'Escape' && !$modal.hasClass('hidden')) _closeModal();
    });

    $('body').append($modal);
    return $modal;
}

function _renderWikilinkModal(title, htmlContent, pushHistory) {
    var $modal = _getOrCreateWikilinkModal();

    if (pushHistory) {
        var curTitle = $modal.find('.wikilink-modal-title').text();
        var curHtml = $modal.find('.wikilink-modal-body').html();
        if (curTitle) _wikilinkHistory.push({ title: curTitle, html: curHtml });
    }

    $modal.find('.wikilink-modal-title').text(title);

    if (htmlContent === null || htmlContent === undefined) {
        $modal.find('.wikilink-modal-body').html(
            '<div class="flex flex-col items-center justify-center py-12 text-gray-400 dark:text-gray-500">' +
            '<svg xmlns="http://www.w3.org/2000/svg" class="w-12 h-12 mb-3 text-gray-300 dark:text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>' +
            '<p class="text-sm font-medium">Dokumen belum tersedia</p>' +
            '<p class="text-xs mt-1">Informasi ini belum ada di knowledge base.</p>' +
            '</div>'
        );
    } else {
        $modal.find('.wikilink-modal-body').html(htmlContent);
    }

    var $back = $modal.find('.wikilink-modal-back');
    if (_wikilinkHistory.length) $back.removeClass('hidden');
    else $back.addClass('hidden');

    $modal.find('.wikilink-modal-body').scrollTop(0);
}

function showWikilinkPreview(title, htmlContent, fromModal) {
    var $modal = _getOrCreateWikilinkModal();
    var isOpen = !$modal.hasClass('hidden');
    _renderWikilinkModal(title, htmlContent, isOpen && fromModal);
    $modal.removeClass('hidden');
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

function _diffTokens(text) {
    return text.match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) || [];
}

function _intralineDiff(oldText, newText) {
    const oldTokens = _diffTokens(oldText);
    const newTokens = _diffTokens(newText);
    const cellCount = oldTokens.length * newTokens.length;

    // Do not allocate a large LCS table for unusually long generated lines.
    if (cellCount > 60000) return [escape(oldText), escape(newText)];

    const dp = Array.from({ length: oldTokens.length + 1 }, () => new Int32Array(newTokens.length + 1));
    for (let i = 1; i <= oldTokens.length; i++) {
        for (let j = 1; j <= newTokens.length; j++) {
            dp[i][j] = oldTokens[i - 1] === newTokens[j - 1]
                ? dp[i - 1][j - 1] + 1
                : Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
    }

    const unchangedOld = new Set();
    const unchangedNew = new Set();
    for (let i = oldTokens.length, j = newTokens.length; i > 0 && j > 0;) {
        if (oldTokens[i - 1] === newTokens[j - 1]) {
            unchangedOld.add(--i);
            unchangedNew.add(--j);
        } else if (dp[i - 1][j] >= dp[i][j - 1]) {
            i--;
        } else {
            j--;
        }
    }

    const render = (tokens, unchanged, className) => tokens.map((token, index) => {
        const html = escape(token);
        return unchanged.has(index) ? html : `<span class="${className}">${html}</span>`;
    }).join('');
    return [
        render(oldTokens, unchangedOld, 'hl-diff-remove-change'),
        render(newTokens, unchangedNew, 'hl-diff-add-change'),
    ];
}

function _renderDiffLine(type, text, content = escape(text)) {
    const className = type === 'add' ? 'hl-diff-add' : type === 'remove' ? 'hl-diff-remove' : 'hl-diff-context';
    const prefix = type === 'add' ? '+' : type === 'remove' ? '-' : ' ';
    return `<span class="${className}">${prefix}${content}</span>`;
}

function _renderDiffChangeBlock(removed, added) {
    const rendered = [];
    const pairs = Math.min(removed.length, added.length);
    for (let i = 0; i < pairs; i++) {
        const [oldHtml, newHtml] = _intralineDiff(removed[i], added[i]);
        rendered.push(_renderDiffLine('remove', removed[i], oldHtml));
        rendered.push(_renderDiffLine('add', added[i], newHtml));
    }
    for (let i = pairs; i < removed.length; i++) rendered.push(_renderDiffLine('remove', removed[i]));
    for (let i = pairs; i < added.length; i++) rendered.push(_renderDiffLine('add', added[i]));
    return rendered;
}

export function highlightDiff(patch) {
    if (!patch) return '';
    const lines = String(patch).split('\n');
    const rendered = [];
    for (let index = 0; index < lines.length;) {
        const line = lines[index];
        if (line.startsWith('-') && !line.startsWith('--- ')) {
            const removed = [];
            while (index < lines.length && lines[index].startsWith('-') && !lines[index].startsWith('--- ')) {
                removed.push(lines[index++].slice(1));
            }
            const added = [];
            while (index < lines.length && lines[index].startsWith('+') && !lines[index].startsWith('+++ ')) {
                added.push(lines[index++].slice(1));
            }
            rendered.push(..._renderDiffChangeBlock(removed, added));
            continue;
        }
        if (line.startsWith('@@')) rendered.push(`<span class="hl-diff-header">${escape(line)}</span>`);
        else if (line.startsWith('--- ') || line.startsWith('+++ ')) rendered.push(`<span class="hl-diff-filename">${escape(line)}</span>`);
        else if (line.startsWith('+')) rendered.push(_renderDiffLine('add', line.slice(1)));
        else if (line.startsWith('\\')) rendered.push(`<span class="hl-diff-meta">${escape(line)}</span>`);
        else rendered.push(_renderDiffLine('context', line.slice(1)));
        index++;
    }
    return rendered.join('\n');
}

// ── Tool result rendering helpers ─────────────────────────────────────────────

function _summarizeToolResultValue(value) {
    // Preserve concise scalar arrays (such as validation reason_code) while
    // continuing to suppress nested objects and potentially verbose payloads.
    if (value === null || value === undefined || typeof value === 'object' && !Array.isArray(value)) return null;
    if (Array.isArray(value)) {
        const scalarValues = value.filter(item => item !== null && item !== undefined && typeof item !== 'object');
        if (!scalarValues.length) return null;
        const summary = scalarValues.slice(0, 4).map(String).join(', ');
        return scalarValues.length > 4 ? `${summary}, …` : summary;
    }
    return String(value);
}

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
            const summary = _summarizeToolResultValue(result[k]);
            if (summary !== null) parts.push(`${k}: ${summary}`);
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

function _recallText(value) {
    return value === null || value === undefined ? '' : String(value).trim();
}

function _recallDate(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

function _recallHeader(label, count, engine) {
    const $header = $('<div class="recall-header">');
    const $title = $('<div class="recall-title">').append($('<span class="recall-dot">'), $('<span>').text(label));
    const $meta = $('<div class="recall-header-meta">');
    if (_recallText(engine)) $meta.append($('<span class="recall-chip">').text(engine));
    if (Number.isFinite(count)) $meta.append($('<span class="recall-count">').text(count));
    return $header.append($title, $meta);
}

function _recallEmpty(message) {
    return $('<div class="recall-empty">').text(message);
}

function _recallBody(text) {
    const value = _recallText(text) || 'No preview available.';
    const $body = $('<div class="recall-body">').text(value);
    if (value.length <= 360 && value.split('\n').length <= 6) return $body;
    $body.addClass('is-clamped');
    const $button = $('<button type="button" class="recall-more">').text('Show more').on('click', function () {
        const expanded = $body.toggleClass('is-clamped').hasClass('is-clamped') === false;
        $(this).text(expanded ? 'Show less' : 'Show more');
    });
    return $('<div>').append($body, $button);
}

function _recallMeta(items) {
    const $meta = $('<div class="recall-meta">');
    items.filter(item => item.value).forEach(item => $meta.append($('<span>').addClass(item.className || '').text(item.value)));
    return $meta;
}

function _renderRecallResult(result) {
    const $wrap = $('<div class="recall-ui">');
    const memories = Array.isArray(result.memories) ? result.memories : [];
    $wrap.append(_recallHeader('Memory recall', memories.length, result.engine));
    if (!memories.length) return $wrap.append(_recallEmpty(_recallText(result.result) || 'No memories found.'));

    const $list = $('<div class="recall-list">');
    memories.forEach((memory, index) => {
        const m = memory && typeof memory === 'object' ? memory : {};
        const identity = _recallText(m.slug) || (m.id !== null && m.id !== undefined ? '#' + m.id : 'Memory ' + (index + 1));
        const content = _recallText(m.snippet) || _recallText(m.content) || _recallText(m.title);
        const score = m.score === null || m.score === undefined || m.score === '' ? NaN : Number(m.score);
        const $top = $('<div class="recall-card-top">').append($('<span class="recall-card-title">').text(identity));
        const $chips = $('<span class="recall-card-chips">');
        if (_recallText(m.category)) $chips.append($('<span class="recall-chip">').text(m.category));
        if (_recallText(m.evidence)) $chips.append($('<span class="recall-chip recall-chip-accent">').text(m.evidence));
        if (Number.isFinite(score)) $chips.append($('<span class="recall-chip recall-score">').text(score.toFixed(3)));
        $top.append($chips);
        const $card = $('<div class="recall-card">').append($top, _recallBody(content));
        const meta = [
            {value: _recallText(m.source_file), className: 'recall-path'},
            {value: _recallDate(m.created_at), className: 'recall-time'}
        ];
        if (meta.some(item => item.value)) $card.append(_recallMeta(meta));
        $list.append($card);
    });
    return $wrap.append($list);
}

function _renderRecallThinkResult(result) {
    const $wrap = $('<div class="recall-ui">');
    const facts = Array.isArray(result.facts) ? result.facts : [];
    const gaps = Array.isArray(result.gaps) ? result.gaps : [];
    $wrap.append(_recallHeader('Memory synthesis', facts.length, result.engine));

    const $list = $('<div class="recall-list">');
    facts.forEach((fact, index) => {
        const f = fact && typeof fact === 'object' ? fact : {};
        const $top = $('<div class="recall-card-top">').append($('<span class="recall-card-title">').text('Fact ' + (index + 1)));
        if (_recallText(f.evidence)) $top.append($('<span class="recall-chip recall-chip-accent">').text(f.evidence));
        const $card = $('<div class="recall-card">').append($top, _recallBody(_recallText(f.fact) || _recallText(f.snippet)));
        const meta = [
            {value: _recallText(f.source), className: 'recall-source'},
            {value: _recallText(f.source_file), className: 'recall-path'}
        ];
        if (meta.some(item => item.value)) $card.append(_recallMeta(meta));
        $list.append($card);
    });
    if (facts.length) $wrap.append($list);
    if (gaps.length) {
        const $gaps = $('<details class="recall-gaps">').append($('<summary>').text('Knowledge gaps · ' + gaps.length));
        gaps.forEach(gap => $gaps.append($('<div>').text(_recallText(gap))));
        $wrap.append($gaps);
    }
    if (!facts.length && !gaps.length) $wrap.append(_recallEmpty('No synthesis results.'));
    return $wrap;
}

function _renderRecallGraphResult(result) {
    const $wrap = $('<div class="recall-ui">');
    const edges = Array.isArray(result.edges) ? result.edges : [];
    $wrap.append(_recallHeader('Knowledge graph', edges.length, result.engine));
    if (_recallText(result.start)) $wrap.append($('<div class="recall-query">').text('From: ' + result.start));
    if (!edges.length) return $wrap.append(_recallEmpty(_recallText(result.result) || 'No connections found.'));

    const $list = $('<div class="recall-list">');
    edges.forEach(edge => {
        const e = edge && typeof edge === 'object' ? edge : {};
        const $row = $('<div class="recall-edge">').append(
            $('<span>').text(_recallText(e.from) || 'Unknown source'),
            $('<span class="recall-relation">').text(_recallText(e.edge) || 'related to'),
            $('<span>').text(_recallText(e.to) || 'Unknown target')
        );
        if (e.hop !== null && e.hop !== undefined) $row.append($('<span class="recall-hop">').text('hop ' + e.hop));
        $list.append($('<div class="recall-card recall-card-edge">').append($row));
    });
    return $wrap.append($list);
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
    // recall — show full memory contents for all modes (fts, think, graph)
    if (ev.tool === 'recall' && typeof ev.result === 'object' && ev.result !== null) {
        if ('memories' in ev.result) return _renderRecallResult(ev.result);
        if ('facts' in ev.result)    return _renderRecallThinkResult(ev.result);
        if ('edges' in ev.result)    return _renderRecallGraphResult(ev.result);
        return $('<div class="text-xs text-purple-700 bg-purple-50 border border-purple-200 rounded px-2 py-1">').text(summarizeToolResult(ev.result));
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
        for (let k = lo; k <= hi;) {
            const { type, line } = ops[k];
            if (type === 'remove') {
                const removed = [];
                while (k <= hi && ops[k].type === 'remove') removed.push(ops[k++].line);
                const added = [];
                while (k <= hi && ops[k].type === 'add') added.push(ops[k++].line);
                html += _renderDiffChangeBlock(removed, added).join('\n') + '\n';
                continue;
            }
            html += _renderDiffLine(type, line) + '\n';
            k++;
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
    if (needsCollapse) $header.append($('<span class="sys-chevron text-[10px] flex-shrink-0 mt-0.5 ml-auto">').addClass(fullColorClass).html('&#9660;'));

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
function _wrapImageWithDownload($img) {
    const imageUrl = $img.attr('src') || $img.attr('data-src');
    if (!imageUrl) return;
    // Lazy-load images to prevent flooding HTTP connections on page with many images
    $img.attr('loading', 'lazy');
    // Thumbnail styling: constrained size via inline CSS (Tailwind compiled CSS
    // may not include arbitrary-value classes like max-w-[85vw]).
    $img.addClass('rounded-lg')
        .css({
            'max-width': '400px',
            'max-height': '300px',
            'object-fit': 'contain',
            'cursor': 'pointer',
            'display': 'block',
        });
    // Responsive: on mobile (<768px) use viewport-relative width
    const _applyResponsiveWidth = function() {
        $img.css('max-width', window.innerWidth < 768 ? '85vw' : '400px');
    };
    _applyResponsiveWidth();
    $(window).on('resize.chatThumb', $.debounce ? undefined : _applyResponsiveWidth);
    // Simple debounced resize handler
    let _resizeTimer;
    $(window).on('resize.chatThumb', function() {
        clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(_applyResponsiveWidth, 150);
    });

    const $container = $('<div>').css({
        position: 'relative',
        display: 'block',
        borderRadius: '0.5rem',
        overflow: 'hidden',
    });
    $img.wrap($container);
    const $wrapper = $img.parent();

    // Lightbox click on the wrapper (image area)
    $wrapper.on('click', function(e) {
        // Don't open lightbox if download button was clicked
        if ($(e.target).closest('button').length) return;
        Lightbox.openFromImage($img[0]);
    });

    const $btn = $('<button>')
        .addClass('w-9 h-9 rounded-md text-white cursor-pointer')
        .css({
            position: 'absolute',
            top: '6px',
            left: '6px',
            zIndex: 10,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            opacity: 0,
            transition: 'opacity 200ms ease',
            backgroundColor: 'rgba(0,0,0,0.4)',
            border: 'none',
        })
        .attr('title', 'Download image')
        .attr('aria-label', 'Download image')
        .html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>')
        .on('click', async function(e) {
            e.preventDefault();
            e.stopPropagation();
            try {
                const response = await fetch(imageUrl);
                const blob = await response.blob();
                const blobUrl = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = blobUrl;
                a.download = imageUrl.split('/').pop().split('?')[0] || 'image';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(blobUrl);
            } catch (err) {
                window.open(imageUrl, '_blank');
            }
        });
    $wrapper.append($btn);

    // Hover: show button
    $wrapper.on('mouseenter', function() { $btn.css('opacity', 1); });
    $wrapper.on('mouseleave', function() { $btn.css('opacity', 0); });
    // Button hover: darker background
    $btn.on('mouseenter', function() { $(this).css('backgroundColor', 'rgba(0,0,0,0.6)'); });
    $btn.on('mouseleave', function() { $(this).css('backgroundColor', 'rgba(0,0,0,0.4)'); });
    // Focus: show button with ring
    $btn.on('focus', function() {
        $(this).css({ opacity: 1, outline: '2px solid rgba(255,255,255,0.7)', outlineOffset: '2px' });
    });
    $btn.on('blur', function() {
        $(this).css({ outline: 'none', outlineOffset: '0' });
        if (!$wrapper.is(':hover')) $btn.css('opacity', 0);
    });
}

const _COPY_ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const _CHECK_ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

function _copyText(text, onDone) {
    const fallback = () => {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); onDone(); } catch (_) {}
        document.body.removeChild(ta);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(onDone).catch(fallback);
    } else {
        fallback();
    }
}

// Applies highlight.js syntax highlighting to every <pre><code> block in a
// rendered markdown bubble. Runs on the live DOM (after sanitize), so the
// injected token <span>s never pass through the sanitizer. No-op if hljs is
// not loaded. Each block is guarded so one bad block can't break the bubble.
function _highlightCode($bubble) {
    if (typeof window === 'undefined' || !window.hljs) return;
    $bubble.find('pre code').each(function () {
        if (this.dataset.highlighted) return;
        try {
            window.hljs.highlightElement(this);
        } catch (e) {
            this.dataset.highlighted = 'error';
        }
    });
}

// Adds a hover-revealed copy button to the bottom-right of every <pre> code block
// and <blockquote> inside a rendered markdown bubble.
function _addCopyButtons($bubble) {
    $bubble.find('pre, blockquote').each(function () {
        const $el = $(this);
        if ($el.data('copyAttached')) return;
        $el.data('copyAttached', true);

        // Wrap so the button stays pinned even when a <pre> scrolls horizontally.
        $el.wrap($('<div>').css({ position: 'relative' }));
        const $container = $el.parent();

        const $btn = $('<button type="button">')
            .attr('title', 'Copy')
            .attr('aria-label', 'Copy to clipboard')
            .css({
                position: 'absolute',
                bottom: '6px',
                right: '6px',
                zIndex: 5,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: '28px',
                height: '28px',
                padding: '0',
                borderRadius: '6px',
                border: 'none',
                cursor: 'pointer',
                color: 'currentColor',
                backgroundColor: 'rgba(128,128,128,0.25)',
                opacity: 0,
                transition: 'opacity 150ms ease',
            })
            .html(_COPY_ICON)
            .on('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                const text = $el.is('pre') ? ($el.find('code').text() || $el.text()) : $el.text();
                _copyText(text, function () {
                    $btn.html(_CHECK_ICON).css('color', '#22c55e');
                    setTimeout(function () { $btn.html(_COPY_ICON).css('color', 'currentColor'); }, 1500);
                });
            });
        $container.append($btn);

        $container.on('mouseenter', function () { $btn.css('opacity', 1); });
        $container.on('mouseleave', function () { if (!$btn.is(':focus')) $btn.css('opacity', 0); });
        $btn.on('mouseenter', function () { $(this).css('backgroundColor', 'rgba(128,128,128,0.4)'); });
        $btn.on('mouseleave', function () { $(this).css('backgroundColor', 'rgba(128,128,128,0.25)'); });
        $btn.on('focus', function () { $(this).css({ opacity: 1, outline: '2px solid rgba(128,128,128,0.7)', outlineOffset: '1px' }); });
        $btn.on('blur', function () {
            $(this).css({ outline: 'none', outlineOffset: '0' });
            if (!$container.is(':hover')) $btn.css('opacity', 0);
        });
    });
}

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

    const meta        = opts.metadata || {};
    const isSlashCmd  = !!meta.slash_command;
    const isBashExec  = !!meta.bash_exec;
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
        $bubble.append(_buildSysBalloon(agentTag, agentContent, 'text-blue-500', 'text-blue-400', 120));

    } else if (isSystemUser) {
        const sysMatch = content.match(/^(\[(?:SYSTEM(?:\/[^\]]*)?|System\/[^\]]*)\])\s*/);
        const sysTag = sysMatch ? sysMatch[1] : '';
        const sysContent = sysTag ? content.slice(sysMatch[0].length) : content;
        $bubble = $('<div class="bg-orange-100 text-orange-900 border border-orange-300 rounded-2xl px-4 py-2.5 text-sm break-words">');
        $bubble.append(_buildSysBalloon(sysTag, sysContent, 'text-orange-500', 'text-orange-400', 120));

    } else if (isUser) {
        $bubble = $('<div class="rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap break-words">').addClass(userBubbleClass);
        const meta = opts.metadata || {};
        const attachmentInfos = Array.isArray(meta.attachment_infos)
            ? meta.attachment_infos
            : (meta.attachment_info ? [meta.attachment_info] : []);
        const imageInfos = attachmentInfos.filter(info => info && info.is_image);
        const imageUrls = Array.isArray(meta.image_urls)
            ? meta.image_urls
            : (meta.image_url ? [meta.image_url] : imageInfos.map(info =>
                `/api/attachments/${encodeURIComponent(info.attachment_id)}/view`));
        const addImage = (slot) => {
            const imageUrl = imageUrls[slot - 1];
            if (!imageUrl) return false;
            const $img = createLazyImage(imageUrl);
            $bubble.append($img);
            _wrapImageWithDownload($img);
            setupImageForLazy($img);
            return true;
        };
        const referenced = new Set();
        const tokenPattern = /\[Image #(\d+)\]/g;
        let cursor = 0;
        let match;
        while ((match = tokenPattern.exec(content || ''))) {
            const slot = Number(match[1]);
            if (!imageUrls[slot - 1]) continue;
            if (match.index > cursor) $bubble.append(document.createTextNode(content.slice(cursor, match.index)));
            addImage(slot);
            referenced.add(slot);
            cursor = match.index + match[0].length;
        }
        if (content && cursor < content.length) $bubble.append(document.createTextNode(content.slice(cursor)));
        for (let slot = 1; slot <= imageUrls.length; slot++) {
            if (!referenced.has(slot)) addImage(slot);
        }
        // Render non-image file badges
        attachmentInfos.filter(info => info && !info.is_image).forEach(info => {
            const $badge = $('<div class="flex items-center gap-1.5 mb-1 px-2 py-1 bg-white/20 rounded text-xs">')
                .append($('<svg class="w-3.5 h-3.5 flex-shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor"><path d="M3 3.5A1.5 1.5 0 0 1 4.5 2h6.879a1.5 1.5 0 0 1 1.06.44l4.122 4.12A1.5 1.5 0 0 1 17 7.622V16.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 16.5v-13Z"/></svg>'))
                .append($('<span class="truncate">').text(info.filename));
            $bubble.prepend($badge);
        });

    } else if (isError) {
        const $icon = $('<svg class="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-8-5a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 10 5Zm0 10a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clip-rule="evenodd"/></svg>');
        $bubble = $('<div class="bg-red-50 text-red-700 border border-red-200 rounded-lg px-4 py-2 text-sm flex items-start gap-2">').append($icon, $('<span>').text(content));

    } else if (isSystem) {
        const sysMatch = content.match(/^\[system[^\]]*\]\s*/i);
        const sysTag = sysMatch ? sysMatch[0].trim() : '[SYSTEM]';
        const sysContent = content.replace(/^\[system[^\]]*\]\s*/i, '');
        $bubble = $('<div class="bg-orange-200 text-gray-600 border-gray-400 rounded-2xl px-4 py-2.5 text-sm break-words">');
        $bubble.append(_buildSysBalloon(sysTag, sysContent, 'text-gray-500', 'text-gray-400', 120));

    } else if (isBashExec) {
        // Bash exec (!) command output — dark terminal with hacker green text
        const rendered = typeof marked !== 'undefined'
            ? sanitize(renderMath(marked.parse((content || '')))).replace(/<table/g, '<div class="table-wrapper"><table').replace(/<\/table>/g, '</table></div>')
            : escape(content);
        $bubble = $('<div class="chat-prose terminal-output rounded-2xl px-4 py-2.5 text-sm break-words">');
        $bubble.css({
            'background-color': '#152022',
            'color': '#359635',
            'border': '1px solid #174217'
        });
        $bubble.attr('role', 'article');
        $bubble.html(rendered);
        $bubble.find('pre').css({
            'background-color': '#0d1517',
            'border': '1px solid #174217',
            'border-radius': '0.375rem'
        });
        $bubble.find('code').css({
            'color': '#359635',
            'background': 'transparent'
        });
        $bubble.find('em').css('color', '#6b8e6b');
        $bubble.find('img').each(function() {
            const $img = $(this);
            _wrapImageWithDownload($img);
            retrofitImageForLazy($img);
        });
        _addCopyButtons($bubble);
    } else if (isSlashCmd) {
        // Slash command response — blue styling, visible to user only (not sent to LLM)
        const rendered = typeof marked !== 'undefined'
            ? sanitize(renderMath(marked.parse((content || '').replace(/[\u201c\u201d\u00ab\u00bb]/g, '"'))).replace(/<table/g, '<div class="table-wrapper"><table').replace(/<\/table>/g, '</table></div>'))
            : escape(content);
        $bubble = $('<div class="chat-prose rounded-2xl px-4 py-2.5 text-sm break-words text-blue-800 border border-blue-200 dark:bg-blue-950 dark:text-blue-200 dark:border-blue-800">');
        $bubble.attr('role', 'article');
        $bubble.html(rendered);
        $bubble.find('img').each(function() {
            const $img = $(this);
            _wrapImageWithDownload($img);
            retrofitImageForLazy($img);
        });
        _highlightCode($bubble);
        _addCopyButtons($bubble);
    } else {
        // assistant: markdown with sanitizer
        const rendered = typeof marked !== 'undefined'
            ? transformWikiLinks(sanitize(renderMath(marked.parse((content || '').replace(/[\u201c\u201d\u00ab\u00bb]/g, '"'))).replace(/<table/g, '<div class="table-wrapper"><table').replace(/<\/table>/g, '</table></div>')))
            : escape(content);
        $bubble = $('<div class="chat-prose rounded-2xl px-4 py-2.5 border-gray-300 text-sm break-words">').addClass(assistantBubbleClass);
        $bubble.attr('role', 'article');
        $bubble.html(rendered);
        $bubble.find('img').each(function() {
            const $img = $(this);
            _wrapImageWithDownload($img);
            retrofitImageForLazy($img);
        });
        _highlightCode($bubble);
        _addCopyButtons($bubble);
        $bubble.on('click', '.kb-wikilink', function (e) {
            e.preventDefault();
            var title = $(this).attr('data-wikilink');
            if (title) document.dispatchEvent(new CustomEvent('evonic:wikilink-click', { detail: { title: title } }));
        });
        // Render non-image file (send_file) as a type-aware, previewable card.
        // Images stay inline via the markdown body.
        if (meta.attachment_info && !meta.attachment_info.is_image) {
            $bubble.prepend($('<div class="mb-2">').append(buildAttachmentCard(meta.attachment_info)));
        }
    }

    const $inner = $('<div class="max-w-[80%] min-w-0">').append($bubble);

    if (showTimestamps && (isUser || role === 'assistant') && opts.timestamp != null) {
        const date = normalizeTimestamp(opts.timestamp);
        let label = '';
        if (date) {
            try {
                label = formatTimestamp
                    ? formatTimestamp(date)
                    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } catch (e) {}
        }
        if (date && label) {
            const tsAlign = isUser
                ? (userAlign === 'left' ? 'text-left' : 'text-right')
                : (assistantAlign === 'left' ? 'text-left' : 'text-right');
            const accessibleLabel = date.toLocaleString([], {
                dateStyle: 'long',
                timeStyle: 'short',
            });
            const $time = $('<time class="block text-[10px] leading-4 text-gray-500 dark:text-gray-400 mt-0.5 px-1 tabular-nums">')
                .addClass(tsAlign)
                .attr('datetime', date.toISOString())
                .attr('aria-label', accessibleLabel)
                .attr('title', accessibleLabel)
                .text(label);
            $inner.append($time);
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
    renderKBDocPreview,
    showWikilinkPreview,
};
