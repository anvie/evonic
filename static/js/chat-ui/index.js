/**
 * index.js — ChatUI facade class.
 *
 * Usage (ES module):
 *   import { ChatUI, SSEAdapter, PollingAdapter, ReplayAdapter } from '/static/js/chat-ui/index.js';
 *   const ui = new ChatUI($('#chat-messages'), { perspective: 'user', agentAvatarUrl: '...' });
 *
 * window.ChatUI is also assigned so non-module page code can construct it after the module loads.
 */

import { log, installDiagnostic } from './debug.js';
import { Turn } from './turn.js';
import { SSEAdapter, PollingAdapter, ReplayAdapter } from './transport.js';
import { DEFAULT_RENDERERS } from './renderers.js';

export { SSEAdapter, PollingAdapter, ReplayAdapter };

// ── Perspective helpers ───────────────────────────────────────────────────────

const PERSPECTIVES = {
    'A': { userAlign: 'right', assistantAlign: 'left' },
    'B': { userAlign: 'left',  assistantAlign: 'right' },
    // legacy aliases
    'user':  { userAlign: 'right', assistantAlign: 'left' },
    'agent': { userAlign: 'left',  assistantAlign: 'right' },
};

// ── ChatUI class ──────────────────────────────────────────────────────────────

export class ChatUI {
    /**
     * @param {jQuery|Element|string} container  - chat message container
     * @param {object} [opts]
     * @param {string} [opts.perspective='user']   - 'user' | 'agent'
     * @param {boolean} [opts.showTimestamps=false]
     * @param {string} [opts.agentAvatarUrl]
     * @param {string} [opts.agentId]             - needed for approval API
     * @param {Function} [opts.formatTimestamp]
     * @param {object} [opts.renderers]            - partial renderer override
     * @param {string} [opts.userBubbleClass]
     * @param {string} [opts.assistantBubbleClass]
     */
    constructor(container, opts = {}) {
        this.$container = $(container);
        this._perspective = opts.perspective || 'user';
        this._opts = Object.assign({
            showTimestamps: false,
            agentAvatarUrl: null,
            agentId: '',
            formatTimestamp: null,
            userBubbleClass: 'bg-indigo-500 text-white dark:bg-indigo-800',
            assistantBubbleClass: 'bg-gray-200 text-gray-800 dark:bg-[#152A3A] border dark:border-[#1B394F] dark:text-gray-100',
        }, opts);
        this._renderers = Object.assign({}, DEFAULT_RENDERERS, opts.renderers || {});
        this._turns = new Map();   // id → Turn
        this._eventLog = [];       // last 200 TurnEvents
        this.$bus = $('<div>');    // jQuery event bus
        this._lastLiveTurnId = null; // ID of the most-recent live SSE turn, for dup detection

        this._log = log('ui');
        installDiagnostic(this, this._turns, this._eventLog);

        // Expose toggleSysBalloon globally (used by system balloon click handlers)
        window.toggleSysBalloon = _toggleSysBalloon;
    }

    // ── Configuration ─────────────────────────────────────────────────────────

    get _cfg() {
        const p = PERSPECTIVES[this._perspective] || PERSPECTIVES.user;
        return Object.assign({}, this._opts, p);
    }

    // ── Public: messages ──────────────────────────────────────────────────────

    /**
     * Append a message bubble to the container.
     * @param {string} role   - 'user' | 'assistant' | 'system' | 'error'
     * @param {string} content
     * @param {object} [opts] - { metadata, timestamp }
     * @returns {jQuery}  the wrapper div (anchor for Turn)
     */
    appendMessage(role, content, opts = {}) {
        const hasMeta = opts.metadata && (opts.metadata.image_url || opts.metadata.attachment_info);
        if (role !== 'error' && (!content || !content.trim()) && !hasMeta) {
            this._log.warn('appendMessage SKIPPED empty/whitespace content', role);
            return $();
        }

        // Remove empty-state placeholder
        this.$container.find('[data-empty-state]').remove();

        // For assistant with timeline metadata, render a finalized thinking bubble first
        if (role === 'assistant' && opts.metadata && opts.metadata.timeline && opts.metadata.timeline.length > 0) {
            this._log.info('appendMessage rendering finalized bubble for assistant, timeline_len=', opts.metadata.timeline.length);
            this._renderFinalizedBubble(opts.metadata.timeline, opts.metadata.thinking_duration);
        }

        const $wrapper = this._renderers.buildMessageBubble(role, content, opts, this._cfg);
        if (!$wrapper || !$wrapper.length) {
            this._log.warn('appendMessage buildMessageBubble returned empty', role);
            return $();
        }
        this.$container.append($wrapper);
        const totalKids = this.$container.children().length;
        this._log.info('appendMessage appended', role, 'totalChildren=', totalKids, 'contentPreview=', String(content||'').slice(0,60));
        this._smartScroll();
        return $wrapper;
    }

    /**
     * Clear all messages and dispose all active turns.
     */
    clear() {
        this._turns.forEach(turn => turn.dispose('clear'));
        this._turns.clear();
        this.$container.empty();
        this._lastLiveTurnId = null;
    }

    // ── Public: turn management ───────────────────────────────────────────────

    /**
     * Begin a new turn anchored to a user message element.
     * The thinking bubble is inserted immediately after $anchor.
     * @param {jQuery} $anchor - user message wrapper
     * @returns {Turn}
     */
    beginTurn($anchor) {
        // Dispose any previous non-finalized turn (defensive)
        const cfg = this._cfg;
        const turn = new Turn({
            $anchor,
            $container:      this.$container,
            renderers:       this._renderers,
            agentAvatarUrl:  cfg.agentAvatarUrl,
            assistantAlign:  cfg.assistantAlign,
            agentId:         cfg.agentId || this._opts.agentId || '',
            onPhaseChange:   (t, prev, next) => {
                this._log.info('turn:phase', t.id, prev, '→', next);
                this.$bus.trigger('turn:phase', [{ turnId: t.id, phase: next, prevPhase: prev }]);
                if (next === 'done' || next === 'aborted') {
                    this._turns.delete(t.id);
                    this.$bus.trigger('turn:disposed', [{ turnId: t.id }]);
                }
            },
            onDispose:       (t, reason) => {
                this._turns.delete(t.id);
                this.$bus.trigger('turn:disposed', [{ turnId: t.id, reason }]);
            },
            onTrigger:       (evtName, data) => {
                this._log.debug('turn event', evtName, data);
                this.$bus.trigger(evtName, [data]);
                if (evtName === 'state:changed') {
                    document.dispatchEvent(new CustomEvent('evonic:state-changed', { detail: data }));
                    // Keep the established page-level event for consumers that have
                    // not migrated to the more specific SSE event name yet.
                    document.dispatchEvent(new CustomEvent('evonic:agent-state-changed', { detail: data }));
                }
                if (evtName === 'tasks:auto_transition' || evtName === 'tasks:stale') {
                    document.dispatchEvent(new CustomEvent('evonic:' + evtName, { detail: data }));
                }
                if (evtName === 'approval:required') {
                    document.dispatchEvent(new CustomEvent('evonic:approval-required', { detail: data }));
                }
                if (evtName === 'approval:resolved') {
                    document.dispatchEvent(new CustomEvent('evonic:approval-resolved', { detail: data }));
                }
                if (evtName === 'session_clear') {
                    this.clear();
                }
            },
        });

        this._turns.set(turn.id, turn);
        this._log.info('beginTurn', turn.id);
        return turn;
    }

    /**
     * High-level: append user message, create turn, optionally attach transport.
     * @param {string} text
     * @param {object} [opts]
     * @param {Function} [opts.transportFactory]  - (turnId) => Transport
     * @returns {{ turn: Turn, $userEl: jQuery }}
     */
    send(text, opts = {}) {
        const $userEl = this.appendMessage('user', text);
        const turn = this.beginTurn($userEl);
        if (opts.transportFactory) {
            const transport = opts.transportFactory(turn.id);
            if (transport) turn.attach(transport);
        }
        this.scrollToBottom();
        return { turn, $userEl };
    }

    // ── Public: replay ────────────────────────────────────────────────────────

    /**
     * Replay a history array (JSONL-style entries) into the container.
     * @param {Array}   entries
     * @param {object}  [opts]
     * @param {boolean} [opts.batch=true]   - instant render (no animation)
     * @param {boolean} [opts.persistent]   - carry over active thinking bubble across calls
     */
    replay(entries, opts = {}) {
        const batch = opts.batch !== false;
        let thinkingTurn = opts._persistTurn || null;

        const _finalizeAndAppend = (thinkingRole, content, metadata) => {
            if (thinkingTurn) {
                thinkingTurn.dispose('replay_finalize');
                thinkingTurn = null;
            }
            this.appendMessage(thinkingRole, content, { metadata });
        };

        const _ensureTurn = (afterEl) => {
            if (!thinkingTurn) {
                const $anchor = afterEl || this.$container.find('[data-msg-role="user"]').last();
                thinkingTurn = this.beginTurn($anchor.length ? $anchor : $());
            }
            return thinkingTurn;
        };

        for (const entry of (entries || [])) {
            if (entry.type === 'user') {
                if (thinkingTurn) { thinkingTurn.dispose('replay_user'); thinkingTurn = null; }
                this.appendMessage('user', entry.content || '', { timestamp: entry.ts ? new Date(entry.ts).toISOString() : null });
                continue;
            }
            if (entry.type === 'thinking') {
                _ensureTurn();
                thinkingTurn.ingest({ event: 'thinking', data: { content: entry.content }, seq: 0 });
                continue;
            }
            if (entry.type === 'tool_call') {
                _ensureTurn();
                thinkingTurn.ingest({ event: 'tool_call_started', data: { tool: entry.function, args: entry.params || {}, param_types: {} }, seq: 0 });
                continue;
            }
            if (entry.type === 'tool_output') {
                _ensureTurn();
                let result;
                try { result = JSON.parse(entry.content); } catch(e) { result = { data: entry.content }; }
                thinkingTurn.ingest({ event: 'tool_executed', data: { tool: entry.function, result, error: !!entry.error }, seq: 0 });
                continue;
            }
            if (entry.type === 'intermediate') {
                _ensureTurn();
                thinkingTurn.ingest({ event: 'response_chunk', data: { content: entry.content, is_final: false }, seq: 0 });
                continue;
            }
            if (entry.type === 'final') {
                const meta = entry.metadata || {};
                const hadStreamingTurn = !!thinkingTurn;
                if (thinkingTurn) {
                    thinkingTurn.ingest({ event: 'done', data: { thinking_duration: meta.thinking_duration }, seq: 0 });
                    thinkingTurn = null;
                }
                // If we built the timeline from streaming events, don't also render it
                // from metadata — that would create a duplicate thinking bubble.
                const msgMeta = hadStreamingTurn ? Object.assign({}, meta, { timeline: [] }) : meta;
                this.appendMessage(meta.error ? 'error' : 'assistant', entry.content, {
                    metadata: msgMeta,
                    timestamp: entry.ts || entry.created_at || null,
                });
                continue;
            }
            if (entry.type === 'error') {
                if (thinkingTurn) { thinkingTurn.ingest({ event: 'done', data: {}, seq: 0 }); thinkingTurn = null; }
                this.appendMessage('error', entry.content || '');
                continue;
            }
            if (entry.type === 'system') {
                const sysMeta = (entry.metadata && (entry.metadata.slash_command || entry.metadata.bash_exec)) ? { metadata: { slash_command: entry.metadata.slash_command || false, bash_exec: entry.metadata.bash_exec || false } } : {};
                this.appendMessage('system', entry.content || '', sysMeta);
                continue;
            }
        }

        if (opts.persistent) {
            // Caller wants to keep the active turn across calls (idle poll)
            return thinkingTurn;
        }

        if (thinkingTurn) {
            thinkingTurn.dispose('replay_end');
        }

        if (batch) this.scrollToBottom();
        return null;
    }

    // ── Public: perspective ───────────────────────────────────────────────────

    /**
     * Re-paint existing messages with new alignment.
     * Only changes justify-start/justify-end; colors are per-role.
     */
    setPerspective(perspective) {
        if (!PERSPECTIVES[perspective]) return;
        this._perspective = perspective;
        const p = PERSPECTIVES[perspective];

        this.$container.find('[data-msg-role]').each(function() {
            const role = $(this).attr('data-msg-role');
            const isUser = role === 'user';
            const isRight = isUser ? (p.userAlign === 'right') : (p.assistantAlign === 'right');
            const newAlign = isRight
                ? 'items-end md:items-start md:justify-end'
                : 'items-start md:justify-start';
            $(this).removeClass('items-end items-start md:items-start md:justify-end md:justify-start justify-start justify-end').addClass(newAlign);
        });
    }

    // ── Public: hooks ─────────────────────────────────────────────────────────

    on(event, handler) {
        this.$bus.on(event, handler);
        return this;
    }

    off(event, handler) {
        this.$bus.off(event, handler);
        return this;
    }

    trigger(event, data) {
        this.$bus.trigger(event, [data]);
    }

    // ── Public: containers & scroll ───────────────────────────────────────────

    scrollToBottom() {
        const c = this.$container[0];
        if (c) c.scrollTop = c.scrollHeight;
    }

    isNearBottom(threshold = 300) {
        const c = this.$container[0];
        if (!c) return true;
        return c.scrollHeight - c.scrollTop - c.clientHeight < threshold;
    }

    batchRender(fn) {
        try { fn(); } finally {}
        this.scrollToBottom();
    }

    // ── Public: queued message indicators ────────────────────────────────────

    markLastUserBubbleQueued($msgEl) {
        // Accept an explicit element or fall back to the last user-role message
        const $last = ($msgEl && $msgEl.length) ? $msgEl : this.$container.find('[data-msg-role="user"]').last();
        if (!$last.length || $last.find('.queued-indicator').length) return;
        const isRight = $last.hasClass('md:justify-end') || $last.hasClass('justify-end');
        const $indicator = $('<div class="queued-indicator flex items-center gap-1 text-[10px] text-gray-400 mt-0.5 px-1">').addClass(isRight ? 'justify-end' : 'justify-start');
        $indicator.html('<svg class="w-3 h-3" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/></svg><span>Queued</span>');
        $last.find('div').first().append($indicator);
    }

    markQueuedAsDelivered() {
        this.$container.find('.queued-indicator').each(function() {
            const $el = $(this);
            setTimeout(() => {
                $el.css('transition', 'opacity 0.5s').css('opacity', '0');
                setTimeout(() => $el.remove(), 500);
            }, 2000);
        });
    }

    // ── Public: upload progress indicators ──────────────────────────────────

    markMessageUploading($msgEl) {
        if (!$msgEl || !$msgEl.length) return;
        if ($msgEl.find('.upload-indicator').length) return;
        const isRight = $msgEl.hasClass('md:justify-end') || $msgEl.hasClass('justify-end');
        const $indicator = $('<div class="upload-indicator flex items-center gap-1.5 mt-1 px-1">').addClass(isRight ? 'justify-end' : 'justify-start');
        $indicator.html('<span class="tool-spinner" style="width:10px;height:10px;border-width:1.5px;border-color:rgba(255,255,255,0.3);border-top-color:rgba(255,255,255,0.9)"></span><span class="upload-indicator-text" style="font-size:10px;color:rgba(255,255,255,0.7)">Uploading…</span>');
        $msgEl.find('div').first().append($indicator);
    }

    updateUploadProgress($msgEl, percent) {
        if (!$msgEl || !$msgEl.length) return;
        const $text = $msgEl.find('.upload-indicator-text');
        if ($text.length) $text.text('Uploading… ' + percent + '%');
    }

    markMessageUploadDone($msgEl) {
        if (!$msgEl || !$msgEl.length) return;
        $msgEl.find('.upload-indicator').each(function() {
            const $el = $(this);
            $el.css('transition', 'opacity 0.5s').css('opacity', '0');
            setTimeout(() => $el.remove(), 500);
        });
    }

    clearContainer() {
        this.clear();
    }

    // ── Public: compatibility with v1 call sites ──────────────────────────────

    /** @deprecated Use beginTurn + attach instead */
    showThinkingIndicator(startTs, insertAfterEl) {
        // Anchor to the explicit element if given, otherwise the very last child so the
        // thinking bubble always appears at the end (not anchored to last user message,
        // which may be mid-conversation when called without an explicit anchor).
        const $anchor = insertAfterEl ? $(insertAfterEl) : this.$container.children().last();
        const turn = this.beginTurn($anchor);
        if (startTs) turn._startTime = startTs;
        this._lastLiveTurnId = turn.id; // track for duplicate-bubble detection
        // A new thinking bubble starting means the agent is now processing any queued
        // message — clear the "Queued" badge regardless of how we got here (turn:split,
        // restoreActiveReasoning after /stop, idle poll, etc.).
        this.markQueuedAsDelivered();
        return turn;
    }

    /** @deprecated Use turn.dispose() instead */
    removeThinkingIndicator(turnOrId) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (turn && turn.dispose) turn.dispose('remove');
    }

    /** @deprecated Use turn.ingest() instead */
    appendTimelineEntry(turnOrId, ev) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn) return;
        let event = ev.type;
        if (ev.type === 'thinking')    event = 'thinking';
        if (ev.type === 'tool_call')   event = 'tool_call_started';
        if (ev.type === 'tool_result') event = 'tool_executed';
        if (ev.type === 'response')    { turn.ingest({ event: 'response_chunk', data: { content: ev.content, is_final: true }, seq: 0 }); return; }
        if (ev.type === 'retry')       event = 'retry';
        turn.ingest({ event, data: ev, seq: 0 });
    }

    /** @deprecated Use turn.dispose() (finalize path) instead */
    finalizeThinkingBubble(turnOrId, duration) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn) return;
        turn.ingest({ event: 'done', data: { thinking_duration: duration }, seq: 0 });
    }

    /** @deprecated */
    getTimelineEntryCount(turnOrId) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn || !turn.$timeline) return 0;
        return turn.$timeline.find('.timeline-entry').length;
    }

    /** @deprecated */
    clearActiveSpinner() {
        this._turns.forEach(t => { if (!t._finalized) t.dispose('clear_spinner'); });
    }

    /** @deprecated Use SSEAdapter directly */
    connectThinkingStream(url, turnOrId, opts = {}) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn) return null;

        const agentIdMatch = url.match(/\/agents\/([^/?]+)\//);
        const agentId = agentIdMatch ? agentIdMatch[1] : '';
        const afterSeq = parseInt(new URL(url, window.location.origin).searchParams.get('after') || '0', 10);

        const adapter = new SSEAdapter(url, { agentId, afterSeq });
        turn.attach(adapter);

        // Wire onSplit / onDone callbacks from v1 callers
        if (opts.onSplit) {
            const origTrigger = turn._onTrigger;
            turn._onTrigger = (evtName, data) => {
                origTrigger(evtName, data);
                if (evtName === 'turn:split') {
                    // Always anchor after the CURRENT last user message in the DOM.
                    // opts.userMsgEl is the message that started this turn, but the
                    // split is triggered by a newly injected message that was appended
                    // to the container AFTER opts.userMsgEl. Using the stale anchor
                    // would insert the new bubble above the injected message.
                    const $lastUser = this.$container.find('[data-msg-role="user"]').last();
                    const $anchor = $lastUser.length ? $lastUser : (opts.userMsgEl ? $(opts.userMsgEl) : turn.$anchor);
                    const newTurn = this.beginTurn($anchor);
                    this._lastLiveTurnId = newTurn.id;
                    this.markQueuedAsDelivered();
                    // Re-route the SSE adapter to the new turn so subsequent events
                    // are not silently dropped by the finalized old turn's ingest guard.
                    adapter._handler = (evt) => newTurn.ingest(evt);
                    opts.onSplit(newTurn);
                }
            };
        }
        if (opts.onDone) {
            // Fire when the turn reaches done/aborted phase
            const doneHandler = (e, payload) => {
                if (payload && payload.turnId === turn.id) {
                    this.$bus.off('turn:disposed', doneHandler);
                    opts.onDone(turn);
                }
            };
            this.$bus.on('turn:disposed', doneHandler);
        }
        // Wire onFinalResponse: fires when SSE delivers response_chunk(is_final) → done
        // so the page can render the final markdown bubble synchronously
        if (opts.onFinalResponse) {
            const finalHandler = (e, payload) => {
                if (payload && payload.turnId === turn.id) {
                    this.$bus.off('final:response', finalHandler);
                    opts.onFinalResponse(payload.content, payload.thinking_duration);
                }
            };
            this.$bus.on('final:response', finalHandler);
        }

        return adapter;
    }

    /** @deprecated */
    closeStream() {
        // Stop all active transports across all turns so reconnecting SSE
        // adapters don't interfere with pollForResponse finalizing.
        for (const [, turn] of this._turns) {
            if (!turn._finalized && turn._transports) {
                for (const t of turn._transports) {
                    try { t.stop(); } catch (e) { /* ignore */ }
                }
                turn._transports.length = 0;
            }
        }
    }

    /** @deprecated */
    hasActiveStream() {
        for (const [, turn] of this._turns) {
            if (!turn._finalized) return true;
        }
        return false;
    }

    /** @deprecated Use on('approval:required', ...) */
    appendApprovalCard(turnOrId, data, agentId) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn) return;
        turn._agentId = agentId || turn._agentId;
        turn._addApprovalCard(data);
    }

    /** @deprecated */
    resolveApprovalCard(data) {
        this._turns.forEach(turn => turn._resolveApprovalCard(data));
    }

    /** @deprecated */
    setApprovalPendingState(turnOrId, pending) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn) return;
        turn._updateSpinner(pending ? 'awaiting_approval' : 'thinking');
    }

    /** @deprecated */
    resolveThinkingIndicator(turnOrId, timeline, duration) {
        const turn = typeof turnOrId === 'string' ? this._turns.get(turnOrId) : turnOrId;
        if (!turn) return;
        for (const ev of timeline) this.appendTimelineEntry(turn, ev);
        this.finalizeThinkingBubble(turn, duration);
    }

    /** @deprecated */
    renderThinkingBubble(timeline, duration) {
        const $anchor = this.$container.find('[data-msg-role="user"]').last();
        const turn = this.beginTurn($anchor);
        this.resolveThinkingIndicator(turn, timeline, duration);
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    _renderFinalizedBubble(timeline, duration) {
        if (this._lastLiveTurnId) {
            const $existing = this.$container.find(`[data-turn-id="${CSS.escape(this._lastLiveTurnId)}"]`);
            this._lastLiveTurnId = null;
            if ($existing.length) return; // SSE already rendered the thinking bubble
        }
        const $anchor = this.$container.find('[data-msg-role="user"]').last();
        const turn = this.beginTurn($anchor);
        for (const ev of timeline) this.appendTimelineEntry(turn, ev);
        this.finalizeThinkingBubble(turn, duration);
    }

    _smartScroll() {
        const c = this.$container[0];
        if (!c) return;
        if (c.scrollHeight - c.scrollTop - c.clientHeight < 300) c.scrollTop = c.scrollHeight;
    }

}

// ── toggleSysBalloon global (needed by system balloon click handlers) ─────────

function _toggleSysBalloon(sysId) {
    const el = document.querySelector('[data-sys-id="' + sysId + '"]');
    if (!el) return;
    const full = el.querySelector('.sys-balloon-full');
    const preview = el.querySelector('.sys-balloon-content');
    const chevron = el.querySelector('.sys-chevron');
    if (!full) return;

    const isCollapsed = full.style.display === 'none' || full.style.display === '';
    if (isCollapsed) {
        if (preview) preview.style.display = 'none';
        full.style.display = 'block';
        full.style.maxHeight = '0';
        full.style.transition = 'max-height 0.25s ease';
        requestAnimationFrame(() => { full.style.maxHeight = full.scrollHeight + 'px'; });
        if (chevron) { chevron.style.transition = 'transform 0.2s ease'; chevron.style.transform = 'rotate(180deg)'; }
    } else {
        full.style.maxHeight = full.scrollHeight + 'px';
        full.style.transition = 'max-height 0.25s ease';
        requestAnimationFrame(() => { full.style.maxHeight = '0'; });
        full.addEventListener('transitionend', function handler() {
            full.removeEventListener('transitionend', handler);
            full.style.display = 'none';
            if (preview) preview.style.display = '';
        });
        if (chevron) { chevron.style.transition = 'transform 0.2s ease'; chevron.style.transform = ''; }
    }
}

// ── Assign to window for non-module page code ─────────────────────────────────

window.ChatUI = ChatUI;
window.SSEAdapter = SSEAdapter;
window.PollingAdapter = PollingAdapter;
window.ReplayAdapter = ReplayAdapter;
window.Lightbox = Lightbox;
window.renderKBDocPreview = renderKBDocPreview;
window.showWikilinkPreview = showWikilinkPreview;

log('ui').info('chat-ui v2 loaded');
