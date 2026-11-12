/**
 * artifacts.js — Saved-artifacts strip shown in chat between the thinking bubble
 * and the final response, plus a self-contained viewer (lightbox for images,
 * modal for pdf/text/code/media). Mirrors the Artifacts tab on the agent detail page.
 *
 * No backend changes: artifacts are served by GET /api/agents/<id>/artifacts/<filename>.
 */

import { Lightbox } from './lightbox.js';
import { sanitize } from './renderers.js';

const _IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico'];
const _VIDEO_EXTS = ['mp4', 'webm', 'mov', 'avi', 'mkv', 'm4v'];
const _AUDIO_EXTS = ['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a', 'wma'];
const _DOC_EXTS   = ['pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'odt'];
const _TEXT_EXTS  = ['txt', 'csv', 'json', 'yaml', 'yml', 'xml', 'log', 'md',
    'py', 'c', 'rs', 'js', 'ts', 'jsx', 'tsx', 'cpp', 'cc', 'cxx',
    'h', 'hpp', 'java', 'go', 'rb', 'php', 'cs', 'swift', 'kt',
    'scala', 'r', 'm', 'sh', 'bash', 'zsh', 'ps1', 'sql',
    'html', 'css', 'scss', 'less', 'toml', 'ini', 'cfg', 'conf',
    'env', 'lock', 'diff', 'patch', 'vue', 'svelte', 'lua', 'pl', 'pm', 'gradle', 'groovy'];

function _ext(filename) {
    const parts = String(filename || '').split('.');
    return parts.length > 1 ? parts.pop().toLowerCase() : '';
}

export function categorizeArtifact(filename) {
    const ext = _ext(filename);
    if (_IMAGE_EXTS.includes(ext)) return 'image';
    if (_VIDEO_EXTS.includes(ext)) return 'video';
    if (_AUDIO_EXTS.includes(ext)) return 'sound';
    if (_DOC_EXTS.includes(ext))   return 'document';
    if (_TEXT_EXTS.includes(ext))  return 'text';
    return 'data';
}

function _escape(text) {
    const div = document.createElement('div');
    div.textContent = String(text == null ? '' : text);
    return div.innerHTML;
}

function _formatSize(size) {
    if (size == null) return '';
    return size < 1024 ? size + ' B'
        : size < 1048576 ? (size / 1024).toFixed(1) + ' KB'
        : (size / 1048576).toFixed(1) + ' MB';
}

// Derive the owning agent id from the artifact filepath
// (shared/agents/<id>/artifacts/<filename>); fall back to the chat agent.
function _agentIdFor(item, agentIdFallback) {
    const m = String(item.filepath || '').match(/agents\/([^/]+)\/artifacts\//);
    return (m && m[1]) || agentIdFallback || '';
}

function artifactUrl(item, agentIdFallback) {
    const agentId = _agentIdFor(item, agentIdFallback);
    return `/api/agents/${encodeURIComponent(agentId)}/artifacts/${encodeURIComponent(item.filename)}`;
}

// SVG path + colors per category (mirrors getArtifactIcon on the agent detail page).
function _categoryIcon(category) {
    switch (category) {
        case 'document':
            return { bg: 'bg-blue-50 dark:bg-blue-900/20', color: 'text-blue-500', path: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path>' };
        case 'image':
            return { bg: 'bg-green-50 dark:bg-green-900/20', color: 'text-green-500', path: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path>' };
        case 'sound':
            return { bg: 'bg-purple-50 dark:bg-purple-900/20', color: 'text-purple-500', path: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"></path>' };
        case 'video':
            return { bg: 'bg-rose-50 dark:bg-rose-900/20', color: 'text-rose-500', path: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"></path>' };
        case 'text':
            return { bg: 'bg-amber-50 dark:bg-amber-900/20', color: 'text-amber-500', path: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>' };
        default:
            return { bg: 'bg-gray-100 dark:bg-gray-700', color: 'text-gray-500', path: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"></path>' };
    }
}

function _iconEl(category, sizeClass) {
    const icon = _categoryIcon(category);
    return $(
        `<div class="flex items-center justify-center ${sizeClass || 'w-10 h-10'} rounded-md flex-shrink-0 ${icon.bg}">` +
        `<svg class="w-5 h-5 ${icon.color}" fill="none" stroke="currentColor" viewBox="0 0 24 24">${icon.path}</svg>` +
        `</div>`
    );
}

const _DOWNLOAD_SVG = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>';
const _TRASH_SVG = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>';

/**
 * Build the saved-artifacts strip.
 * @param {Array<{filename, filepath, size}>} artifacts
 * @param {object} [opts] - { agentIdFallback }
 * @returns {jQuery}
 */
export function buildSavedArtifactsBlock(artifacts, opts = {}) {
    const items = (artifacts || []).filter(a => a && a.filename);
    if (!items.length) return $();
    const agentIdFallback = opts.agentIdFallback || '';

    const $wrap = $('<div class="flex justify-start" data-saved-artifacts>');
    const $inner = $('<div class="ml-5 max-w-[80%] w-full">');

    const $header = $('<div class="flex items-center gap-1.5 mb-1 text-[11px] font-medium text-gray-400 dark:text-gray-500">');
    $header.html(
        '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>' +
        `<span>Saved ${items.length} item${items.length > 1 ? 's' : ''}</span>`
    );
    $inner.append($header);

    // Collect image-artifact URLs (in display order) so the lightbox can page
    // prev/next across sibling images only.
    const imageUrls = items
        .filter(item => categorizeArtifact(item.filename) === 'image')
        .map(item => artifactUrl(item, agentIdFallback));

    const $list = $('<div class="space-y-1.5">');
    items.forEach(item => $list.append(_buildCard(item, agentIdFallback, imageUrls)));
    $inner.append($list);
    $wrap.append($inner);
    return $wrap;
}

function _buildCard(item, agentIdFallback, imageUrls) {
    const category = categorizeArtifact(item.filename);
    const url = artifactUrl(item, agentIdFallback);
    const sizeStr = _formatSize(item.size);
    // For images, build the gallery context (sibling images + this one's index)
    // so the lightbox can navigate prev/next.
    const gallery = (category === 'image' && imageUrls && imageUrls.length)
        ? { urls: imageUrls, index: imageUrls.indexOf(url) }
        : null;

    const $card = $('<div class="bg-gray-50 dark:bg-gray-700/50 border border-gray-200 dark:border-gray-600 rounded-lg p-2 hover:border-indigo-300 dark:hover:border-indigo-500 transition-colors flex items-center gap-2.5 group">');

    // Thumbnail (image) or category icon.
    let $thumb;
    if (category === 'image') {
        $thumb = $('<div class="w-10 h-10 rounded-md overflow-hidden flex-shrink-0 bg-gray-100 dark:bg-gray-800 flex items-center justify-center cursor-pointer">');
        const $img = $('<img class="w-full h-full object-cover" alt="">').attr('src', url);
        // Fallback to a placeholder image icon when the thumbnail fails to load.
        $img.on('error', function () {
            $(this).remove();
            $thumb.append(_iconEl('image', 'w-10 h-10').addClass('rounded-md'));
        });
        $thumb.append($img);
    } else {
        $thumb = _iconEl(category, 'w-10 h-10').addClass('cursor-pointer');
    }
    $thumb.on('click', () => openSavedArtifact(url, item.filename, category, gallery));
    $card.append($thumb);

    const $meta = $('<div class="flex-1 min-w-0 cursor-pointer">');
    $meta.append(
        $('<p class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">').attr('title', item.filename).text(item.filename),
        $('<p class="text-xs text-gray-400">').text(sizeStr)
    );
    $meta.on('click', () => openSavedArtifact(url, item.filename, category, gallery));
    $card.append($meta);

    const $dl = $('<a class="p-1.5 text-indigo-500 hover:text-indigo-700 rounded hover:bg-indigo-50 dark:hover:bg-indigo-900/20 transition-colors opacity-0 group-hover:opacity-100 flex-shrink-0" title="Download">')
        .attr({ href: url, download: item.filename })
        .html(_DOWNLOAD_SVG);
    $dl.on('click', (e) => e.stopPropagation());
    $card.append($dl);

    return $card;
}

/**
 * Open an artifact: lightbox for images, modal viewer for everything else.
 * @param {object} [gallery] - { urls: string[], index: number } to enable
 *   prev/next navigation across sibling image artifacts. Optional.
 * @param {object} [opts] - { downloadUrl, allowDelete } forwarded to the viewer
 *   modal. Defaults preserve artifact behavior (download from the view URL,
 *   delete enabled). Attachments pass a separate downloadUrl and allowDelete:false.
 */
export function openSavedArtifact(url, filename, category, gallery, opts = {}) {
    if (category === 'image') {
        const urls = (gallery && gallery.urls && gallery.urls.length) ? gallery.urls : [url];
        let idx = (gallery && typeof gallery.index === 'number') ? gallery.index : 0;
        if (idx < 0) idx = Math.max(0, urls.indexOf(url));
        const lb = (window.Lightbox && window.Lightbox.open) ? window.Lightbox : Lightbox;
        lb.open(urls, idx);
        return;
    }
    _openViewerModal(url, filename, category, opts);
}

/**
 * Build a card for a file delivered via the send_file tool (a stored
 * attachment), mirroring the saved-artifact card so agent-sent files can be
 * previewed before download. Reuses the shared viewer/lightbox.
 * @param {{attachment_id, filename, size_bytes, is_image}} info
 * @returns {jQuery}
 */
export function buildAttachmentCard(info) {
    const filename = info.filename || 'file';
    const viewUrl = `/api/attachments/${encodeURIComponent(info.attachment_id)}/view`;
    const downloadUrl = `/api/attachments/${encodeURIComponent(info.attachment_id)}/download`;
    const category = info.is_image ? 'image' : categorizeArtifact(filename);
    const sizeStr = _formatSize(info.size_bytes);

    const open = () => openSavedArtifact(viewUrl, filename, category, null, {
        downloadUrl, allowDelete: false,
    });

    const $card = $('<div class="bg-gray-50 dark:bg-gray-700/50 border border-gray-200 dark:border-gray-600 rounded-lg p-2 hover:border-indigo-300 dark:hover:border-indigo-500 transition-colors flex items-center gap-2.5 group max-w-sm">');

    let $thumb;
    if (category === 'image') {
        $thumb = $('<div class="w-10 h-10 rounded-md overflow-hidden flex-shrink-0 bg-gray-100 dark:bg-gray-800 flex items-center justify-center cursor-pointer">');
        const $img = $('<img class="w-full h-full object-cover" alt="">').attr('src', viewUrl);
        $img.on('error', function () {
            $(this).remove();
            $thumb.append(_iconEl('image', 'w-10 h-10').addClass('rounded-md'));
        });
        $thumb.append($img);
    } else {
        $thumb = _iconEl(category, 'w-10 h-10').addClass('cursor-pointer');
    }
    $thumb.on('click', open);
    $card.append($thumb);

    const $meta = $('<div class="flex-1 min-w-0 cursor-pointer">');
    $meta.append(
        $('<p class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">').attr('title', filename).text(filename),
        $('<p class="text-xs text-gray-400">').text(sizeStr)
    );
    $meta.on('click', open);
    $card.append($meta);

    const $dl = $('<a class="p-1.5 text-indigo-500 hover:text-indigo-700 rounded hover:bg-indigo-50 dark:hover:bg-indigo-900/20 transition-colors opacity-0 group-hover:opacity-100 flex-shrink-0" title="Download">')
        .attr({ href: downloadUrl, download: filename })
        .html(_DOWNLOAD_SVG);
    $dl.on('click', (e) => e.stopPropagation());
    $card.append($dl);

    return $card;
}

let _escHandler = null;
let _viewerRawContent = null;

function _closeViewerModal() {
    const modal = document.getElementById('chat-artifact-viewer-modal');
    if (modal) modal.remove();
    if (_escHandler) { document.removeEventListener('keydown', _escHandler); _escHandler = null; }
}

async function _copyViewerContent() {
    const $btn = $(this);
    if (!_viewerRawContent) {
        (window.toast?.warning || console.warn)('No content to copy.');
        return;
    }
    try {
        await navigator.clipboard.writeText(_viewerRawContent);
    } catch(e) {
        // Fallback for older browsers or non-HTTPS
        const textarea = document.createElement('textarea');
        textarea.value = _viewerRawContent;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
    }
    // Visual feedback
    const $span = $btn.find('span');
    const origText = $span.text();
    $btn.find('svg').replaceWith('<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>');
    $span.text(' Copied!');
    $btn.removeClass('text-indigo-600 dark:text-indigo-400').addClass('text-green-600 dark:text-green-400');
    setTimeout(() => {
        $btn.find('svg').replaceWith('<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>');
        $span.text(origText);
        $btn.removeClass('text-green-600 dark:text-green-400').addClass('text-indigo-600 dark:text-indigo-400');
    }, 2000);
}

async function _deleteArtifactFromViewer(url, filename) {
    const m = url.match(/agents\/([^/]+)\/artifacts/);
    const agentId = m ? m[1] : '';
    if (!agentId) {
        (window.toast?.error || alert)('Could not determine agent ID.');
        return;
    }

    let ok = false;
    try {
        ok = await window.showConfirm({
            title: 'Delete Artifact',
            message: `Delete "${filename}"? This cannot be undone.`,
            confirmText: 'Delete',
            danger: true
        });
    } catch (_) {
        ok = confirm(`Delete "${filename}"? This cannot be undone.`);
    }
    if (!ok) return;

    const deleteUrl = `/api/agents/${encodeURIComponent(agentId)}/artifacts/${encodeURIComponent(filename)}`;
    try {
        const res = await fetch(deleteUrl, { method: 'DELETE' });
        if (res.ok) {
            (window.toast?.success || console.log)('Artifact deleted.');
            _closeViewerModal();
        } else {
            const msg = res.status === 404 ? 'Artifact not found.' : `Server error (${res.status}).`;
            (window.toast?.error || alert)(msg);
        }
    } catch (_) {
        (window.toast?.error || alert)('Network error. Please try again.');
    }
}

function _openViewerModal(url, filename, category, opts = {}) {
    _closeViewerModal();
    const downloadUrl = opts.downloadUrl || url;
    const allowDelete = opts.allowDelete !== false;

    const $overlay = $('<div id="chat-artifact-viewer-modal" class="fixed inset-0 flex items-center justify-center p-4" style="z-index:200;background:rgba(0,0,0,0.6);">');
    $overlay.on('click', (e) => { if (e.target === $overlay[0]) _closeViewerModal(); });

    const $box = $('<div class="bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-4xl mx-4 max-h-[90vh] flex flex-col overflow-hidden">');

    const $head = $('<div class="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-600 flex-shrink-0">');
    const $title = $('<div class="min-w-0 flex-1 pr-3">');
    $title.append(
        $('<h3 class="text-base font-semibold text-gray-800 dark:text-gray-100 truncate">').attr('title', filename).text(filename),
        $('<p class="text-xs text-gray-400">').text(category.charAt(0).toUpperCase() + category.slice(1))
    );
    const $actions = $('<div class="flex items-center gap-2 flex-shrink-0">');
    const $dl = $('<a class="flex items-center gap-1.5 px-3 py-1.5 text-xs text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 rounded-md transition-colors" title="Download">')
        .attr({ href: downloadUrl, download: filename })
        .html(_DOWNLOAD_SVG + '<span>Download</span>');
    const $copy = $('<button class="flex items-center gap-1.5 px-3 py-1.5 text-xs text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 rounded-md transition-colors" title="Copy content">')
        .html('<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg><span>Copy</span>');
    $copy.on('click', _copyViewerContent);
    const $close = $('<button class="flex items-center justify-center w-8 h-8 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors" title="Close">')
        .html('<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>');
    $close.on('click', _closeViewerModal);
    $actions.append($dl, $copy);
    if (allowDelete) {
        const $delete = $('<button class="flex items-center justify-center w-8 h-8 rounded-md text-red-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 dark:hover:text-red-400 transition-colors" title="Delete">').html(_TRASH_SVG);
        $delete.on('click', (e) => { e.stopPropagation(); _deleteArtifactFromViewer(url, filename); });
        $actions.append($delete);
    }
    $actions.append($close);
    $head.append($title, $actions);

    const $body = $('<div class="flex-1 overflow-y-auto p-6">');
    $body.html('<div class="flex items-center justify-center py-16"><svg class="animate-spin w-8 h-8 text-indigo-500" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg></div>');

    $box.append($head, $body);
    $overlay.append($box);
    $(document.body).append($overlay);

    _escHandler = (e) => { if (e.key === 'Escape') _closeViewerModal(); };
    document.addEventListener('keydown', _escHandler);

    _renderViewerContent($body, url, filename, category);
}

async function _renderViewerContent($body, url, filename, category) {
    const ext = _ext(filename);
    try {
        if (ext === 'md') {
            const text = await (await fetch(url)).text();
            _viewerRawContent = text;
            $body.html('<div class="recap-prose max-w-none">' + sanitize(marked.parse(text)) + '</div>');
        } else if (ext === 'pdf') {
            $body.html(`<iframe src="${url}" class="w-full rounded-md border border-gray-200 dark:border-gray-600" style="min-height:70vh;"></iframe>`);
        } else if (_VIDEO_EXTS.includes(ext)) {
            const type = ext === 'mov' ? 'quicktime' : ext === 'mkv' ? 'x-matroska' : ext === 'm4v' ? 'mp4' : ext;
            $body.html(`<div class="flex justify-center"><video controls class="max-w-full max-h-[70vh] rounded-md shadow-lg"><source src="${url}" type="video/${type}">Your browser does not support the video tag.</video></div>`);
        } else if (_AUDIO_EXTS.includes(ext)) {
            const type = ext === 'm4a' ? 'mp4' : ext === 'wma' ? 'x-ms-wma' : ext;
            $body.html(`<div class="flex flex-col items-center gap-4 py-8"><audio controls class="w-full max-w-lg"><source src="${url}" type="audio/${type}">Your browser does not support the audio tag.</audio></div>`);
        } else if (_IMAGE_EXTS.includes(ext)) {
            $body.html(`<div class="flex justify-center"><img src="${url}" alt="${_escape(filename)}" class="max-w-full max-h-[70vh] rounded-md shadow-lg object-contain"></div>`);
        } else if (_TEXT_EXTS.includes(ext) || category === 'text') {
            const text = await (await fetch(url)).text();
            _viewerRawContent = text;
            if ((ext === 'patch' || ext === 'diff') && window.EvPatchViewer) {
                $body.html(window.EvPatchViewer.render(text));
                return;
            }
            $body.html(`<pre class="bg-gray-50 dark:bg-gray-900 rounded-md p-4 text-sm text-gray-800 dark:text-gray-200 overflow-x-auto max-h-[70vh] whitespace-pre-wrap font-mono">${_escape(text)}</pre>`);
        } else {
            $body.html(
                '<div class="flex flex-col items-center gap-4 py-12 text-gray-400">' +
                '<svg class="w-16 h-16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>' +
                '<p class="text-sm">Preview not available for this file type.</p>' +
                `<a href="${url}" download class="inline-flex items-center gap-1.5 px-4 py-2 bg-indigo-500 hover:bg-indigo-600 text-white rounded-md text-sm font-medium transition-colors">${_DOWNLOAD_SVG}<span>Download File</span></a>` +
                '</div>'
            );
        }
    } catch (e) {
        $body.html('<div class="flex flex-col items-center gap-3 py-12 text-red-400"><svg class="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M10.29 3.86l-8.6 14.86A1 1 0 002.56 20h18.88a1 1 0 00.87-1.5l-8.6-14.86a1 1 0 00-1.74 0z"></path></svg><p class="text-sm">Failed to load file content.</p></div>');
    }
}
