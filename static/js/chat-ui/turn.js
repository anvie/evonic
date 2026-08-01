/**
 * turn.js — Turn state machine, anchored bubble creation, dispose.
 *
 * Phase transitions:
 *   pending           → thinking          (turn_begin)
 *   thinking          → tool_running      (tool_call_started)
 *   tool_running      → tool_done         (tool_executed)
 *   tool_done         → thinking          (thinking)
 *   tool_done         → tool_running      (tool_call_started)
 *   *                 → awaiting_approval (approval_required)
 *   awaiting_approval → <prev non-approval> (approval_resolved)
 *   *                 → final             (response — is_final)
 *   final             → done              (done)
 *   *                 → aborted           (abort)
 *
 * Spinner location derived from phase (one location, structurally impossible to double):
 *   pending | thinking | awaiting_approval → header spinner
 *   tool_running                           → active timeline-entry border-spinner
 *   tool_done                              → inline "Thinking…" row in panel
 *   final | done | aborted                → no spinner
 */

import { log, assert } from './debug.js';
import { buildSavedArtifactsBlock } from './artifacts.js';

const STALE_TIMEOUT_MS = 300_000; // 5 minutes — safety net for truly abandoned turns

const TERMINAL_PHASES = new Set(['final', 'done', 'aborted']);

/**
 * Pure reducer: given current phase and incoming event kind, return next phase.
 * Returns null if the transition is illegal (absorbed silently).
 */
export function reduceTurn(phase, eventKind, isFinal = false) {
    // A response only ENDS the turn when it is the FINAL response. Intermediate
    // response_chunks (is_final=false, emitted before further tool calls) must NOT
    // move the turn to the terminal 'final' phase — otherwise every subsequent
    // tool_call/tool_executed/thinking/real-final is absorbed as a no-op and the
    // live turn freezes at the first intermediate message.
    const isTerminalResponse = eventKind === 'done' ||
        (eventKind === 'response_chunk' && isFinal);
    if (phase === 'pending') {
        if (eventKind === 'turn_begin')        return 'thinking';
        if (eventKind === 'thinking')          return 'thinking';
        if (eventKind === 'tool_call_started') return 'tool_running';
        if (isTerminalResponse)                return 'final';
        if (eventKind === 'approval_required') return 'awaiting_approval';
    }
    if (phase === 'thinking') {
        if (eventKind === 'tool_call_started') return 'tool_running';
        if (isTerminalResponse)                return 'final';
        if (eventKind === 'approval_required') return 'awaiting_approval';
        if (eventKind === 'thinking')          return 'thinking'; // no-op transition
        if (eventKind === 'retry')             return 'thinking';
        if (eventKind === 'turn_split')        return 'pending';
    }
    if (phase === 'tool_running') {
        if (eventKind === 'tool_executed')     return 'tool_done';
        if (isTerminalResponse)                return 'final';
        if (eventKind === 'approval_required') return 'awaiting_approval';
    }
    if (phase === 'tool_done') {
        if (eventKind === 'thinking')          return 'thinking';
        if (eventKind === 'tool_call_started') return 'tool_running';
        if (isTerminalResponse)                return 'final';
        if (eventKind === 'approval_required') return 'awaiting_approval';
    }
    if (phase === 'awaiting_approval') {
        if (eventKind === 'approval_resolved') return 'thinking';
    }
    if (phase === 'final') {
        if (eventKind === 'done')              return 'done';
        if (eventKind === 'turn_split')        return 'pending';
    }
    // abort from any phase
    if (eventKind === 'abort')                 return 'aborted';
    return null; // illegal / no-op
}

/**
 * Derive where the spinner should appear given the current phase.
 * Returns 'header' | 'entry' | 'thinking_row' | 'none'.
 */
export function deriveSpinnerLocation(phase) {
    if (phase === 'pending' || phase === 'thinking' || phase === 'awaiting_approval') return 'header';
    if (phase === 'tool_running') return 'entry';
    if (phase === 'tool_done')    return 'thinking_row';
    return 'none';
}

let _turnCounter = 0;

export class Turn {
    /**
     * @param {object} opts
     * @param {jQuery}   opts.$anchor        - user message wrapper to insert bubble after
     * @param {jQuery}   opts.$container     - chat container
     * @param {object}   opts.renderers      - renderer registry (from renderers.js)
     * @param {string}   [opts.agentAvatarUrl]
     * @param {string}   [opts.assistantAlign] - 'left' | 'right'
     * @param {Function} opts.onPhaseChange  - (turn, prevPhase, nextPhase) => void
     * @param {Function} opts.onDispose      - (turn, reason) => void
     * @param {Function} opts.onTrigger      - (eventName, data) => void  (for UI events)
     * @param {string}   [opts.agentId]      - for approval API calls
     */
    constructor(opts) {
        this.id = 'turn-' + Date.now() + '-' + (++_turnCounter);
        this.$anchor = opts.$anchor;
        this._$container = opts.$container;
        this._renderers = opts.renderers;
        this._agentAvatarUrl = opts.agentAvatarUrl || null;
        this._assistantAlign = opts.assistantAlign || 'left';
        this._onPhaseChange = opts.onPhaseChange || (() => {});
        this._onDispose = opts.onDispose || (() => {});
        this._onTrigger = opts.onTrigger || (() => {});
        this._agentId = opts.agentId || '';

        this.phase = 'pending';
        this._lastSeq = 0;
        this._transports = [];
        this._timerInterval = null;
        this._staleTimeout = null;
        this._scrollRAF = null;
        this._startTime = Date.now();
        this._finalized = false;
        this._preApprovalPhase = 'thinking';
        this._finalContent = null;  // stored from response_chunk (is_final), used for SSE→response bubble
        this._savedArtifacts = [];  // artifacts saved via save_artifact during this turn
        this._savedArtifactsRendered = false;
        this._$savedArtifactsBlock = null;  // live strip element, updated in place

        this._log = log('turn');

        this._buildDOM();
        this._startTimer();
        this._armStaleTimeout();
    }

    // ── DOM ──────────────────────────────────────────────────────────────────

    _buildDOM() {
        const alignClass = this._assistantAlign === 'right' ? 'justify-end' : 'justify-start';
        const avatarHtml = this._agentAvatarUrl
            ? `<img src="${this._agentAvatarUrl}" alt="" class="w-7 h-7 rounded-full object-cover flex-shrink-0 mt-1 bg-indigo-50 dark:bg-indigo-900/20" onerror="this.onerror=null;this.style.display='none'">`
            : '';

        // Thinking bubble header
        this.$bubble = $(`<div class="flex ${alignClass} ${avatarHtml ? 'items-start gap-2' : ''}"
             role="status" aria-live="polite" aria-label="Agent is thinking"
             data-turn-id="${this.id}">`);

        const $inner = $('<div class="thinking-bubble flex items-center gap-2 rounded-lg px-3 py-2 text-xs border border-purple-200 bg-purple-50 dark:border-purple-800 dark:bg-purple-900/60 cursor-pointer">');
        $inner.append(
            $('<span>').html('&#129504;'),
            $('<span class="font-medium text-purple-700 dark:text-purple-200">').text('Thinking'),
            $('<span class="thinking-step-count text-purple-500 dark:text-purple-300 text-[10px] ml-0.5 hidden">'),
            $('<span class="thinking-spinner">').append($('<span class="tool-spinner">')),
            $('<span class="thinking-timer-inline text-purple-500 dark:text-purple-400 text-[10px]">').text('0.0 s'),
            $('<span class="ml-1 text-purple-500 dark:text-purple-300 tool-trace-chevron text-sm">').html('&#9656;')
        );

        if (avatarHtml) this.$bubble.append($(avatarHtml));
        this.$bubble.append($inner);

        // Timeline panel container
        this.$panel = $(`<div class="flex ${alignClass}" data-turn-id="${this.id}">`);
        const $panelInner = $('<div class="ml-5 max-w-[80%]">');
        this.$timeline = $('<div class="timeline-panel hidden space-y-0.5 py-0.5">');
        $panelInner.append(this.$timeline);
        this.$panel.append($panelInner);

        // Click to expand/collapse timeline
        $inner.on('click', () => {
            this.$timeline.toggleClass('hidden');
            $inner.find('.tool-trace-chevron').toggleClass('rotated');
        });

        // Always append at the end of the container. Inserting after $anchor would
        // place the bubble in the middle of the conversation when there are already
        // agent messages rendered after the user message (e.g. turn_split, replay).
        this._$container.append(this.$bubble);
        this.$bubble.after(this.$panel);

        // Auto-expand timeline on creation so user sees thought process in real-time
        this.$timeline.removeClass('hidden');
        this.$bubble.find('.tool-trace-chevron').addClass('rotated');
    }

    _startTimer() {
        const timerEl = this.$bubble.find('.thinking-timer-inline')[0];
        if (!timerEl) return;
        this._timerInterval = setInterval(() => {
            const elapsed = Date.now() - this._startTime;
            timerEl.textContent = (elapsed / 1000).toFixed(1) + ' s';
        }, 100);
    }

    _armStaleTimeout() {
        this._staleTimeout = setTimeout(() => {
            this._log.warn('stale timeout reached, auto-finalizing', this.id);
            this._finalizeBubble(null);
        }, STALE_TIMEOUT_MS);
    }

    _clearTimers() {
        if (this._timerInterval) { clearInterval(this._timerInterval); this._timerInterval = null; }
        if (this._staleTimeout)  { clearTimeout(this._staleTimeout);   this._staleTimeout = null; }
        if (this._scrollRAF)     { cancelAnimationFrame(this._scrollRAF); this._scrollRAF = null; }
    }

    // ── State machine ────────────────────────────────────────────────────────

    /**
     * Primary entry point: processes a single TurnEvent from any transport.
     * @param {{ event: string, data: object, seq: number }} evt
     */
    ingest(evt) {
        const { event: evtName, data = {}, seq = 0 } = evt;

        // Drop events after finalization — prevents a reconnected SSEAdapter from
        // feeding a completed turn with the next turn's events.
        if (this._finalized) {
            this._log.warn('ingest after finalize ignored', evtName, this.id);
            console.warn('[turn] ingest ignored (finalized) event=%s turn=%s', evtName, this.id);
            return;
        }

        // Sequence deduplication
        if (seq && seq <= this._lastSeq) {
            this._log.debug('dedup seq', seq, '≤', this._lastSeq, this.id);
            return;
        }
        if (seq) this._lastSeq = seq;

        // Reset stale timeout on every live event — turn is clearly still active
        if (this._staleTimeout) {
            clearTimeout(this._staleTimeout);
            this._staleTimeout = setTimeout(() => {
                this._log.warn('stale timeout reached, auto-finalizing', this.id);
                this._finalizeBubble(null);
            }, STALE_TIMEOUT_MS);
        }

        this._log.debug('ingest', evtName, seq, '→ phase was', this.phase, this.id);

        const nextPhase = reduceTurn(this.phase, evtName, data.is_final);
        if (nextPhase !== null && nextPhase !== this.phase) {
            const prev = this.phase;
            this.phase = nextPhase;
            this._log.info('phase', prev, '→', nextPhase, 'via', evtName, this.id);
            this._onPhaseChange(this, prev, nextPhase);
            this._updateSpinner(nextPhase);
        }

        this._handleEventRendering(evtName, data);
    }

    _updateSpinner(phase) {
        const loc = deriveSpinnerLocation(phase);
        this._log.debug('spinner →', loc, this.id);

        const $spinner = this.$bubble.find('.thinking-spinner');
        const $approvalColor = this.$bubble.find('.thinking-bubble');

        if (phase === 'awaiting_approval') {
            $approvalColor
                .removeClass('border-purple-200 bg-purple-50 dark:border-purple-800 dark:bg-purple-900/60')
                .addClass('border-orange-300 bg-orange-50 dark:border-orange-700 dark:bg-orange-900/60');
            this.$bubble.find('span:first').html('&#128274;');
            this.$bubble.find('.font-medium')
                .text('Menunggu Approval')
                .removeClass('text-purple-700 dark:text-purple-200')
                .addClass('text-orange-700 dark:text-orange-200');
        } else if (this._lastApprovalPhase !== phase && (phase === 'thinking' || phase === 'tool_running' || phase === 'tool_done')) {
            $approvalColor
                .addClass('border-purple-200 bg-purple-50 dark:border-purple-800 dark:bg-purple-900/60')
                .removeClass('border-orange-300 bg-orange-50 dark:border-orange-700 dark:bg-orange-900/60');
            this.$bubble.find('span:first').html('&#129504;');
            this.$bubble.find('.font-medium')
                .text('Thinking')
                .addClass('text-purple-700 dark:text-purple-200')
                .removeClass('text-orange-700 dark:text-orange-200');
        }

        if (loc === 'none' || TERMINAL_PHASES.has(phase)) {
            $spinner.hide();
        } else {
            $spinner.show();
        }
    }

    _handleEventRendering(evtName, data) {
        if (evtName === 'turn_begin') {
            // Already in thinking phase — bubble is ready
            if (data.ts) this._startTime = data.ts;
            return;
        }

        if (evtName === 'thinking') {
            this._addTimelineEntry({ type: 'thinking', content: data.content || '' });
            return;
        }

        if (evtName === 'tool_call_started') {
            this._addTimelineEntry({ type: 'tool_call', tool: data.tool, args: data.args || {}, param_types: data.param_types || {} });
            return;
        }

        if (evtName === 'tool_executed') {
            // Surface saved artifacts live (below the thinking stream) the moment a
            // save_artifact call succeeds — no need to wait for the turn to finish.
            if (this._captureSavedArtifact(data)) this._renderSavedArtifacts();
            this._mergeToolResult(data);
            this._showThinkingRow();
            // Fire tool:executed for agent-state-bridge
            this._onTrigger('tool:executed', data);
            return;
        }

        if (evtName === 'state:changed') {
            this._onTrigger('state:changed', data);
            return;
        }

        if (evtName === 'tasks:auto_transition' || evtName === 'tasks:stale') {
            this._onTrigger(evtName, data);
            return;
        }

        if (evtName === 'response_chunk' && data.content) {
            // Render every response chunk in the trace (intermediate + final), matching
            // the history-render view. Only the FINAL chunk is stashed for the final
            // response bubble; intermediates stay inline in the thinking timeline.
            this._addTimelineEntry({ type: 'response', content: data.content });
            if (data.is_final) this._finalContent = data.content;
            return;
        }

        if (evtName === 'retry') {
            this._addTimelineEntry({ type: 'retry', message: data.message, retry_count: data.retry_count, max_retries: data.max_retries });
            return;
        }

        if (evtName === 'approval_required') {
            this._addApprovalCard(data);
            this._onTrigger('approval:required', data);
            return;
        }

        if (evtName === 'approval_resolved') {
            this._resolveApprovalCard(data);
            this._onTrigger('approval:resolved', data);
            return;
        }

        if (evtName === 'done') {
            console.warn('[turn] done event turn=%s _finalized=%s _finalContent=%s', this.id, this._finalized, !!this._finalContent);
            this._finalizeBubble(data.thinking_duration);
            // Fire final:response so page-level code can render the response bubble
            // synchronously — no dependency on pollForResponse JSONL poll.
            if (this._finalContent) {
                console.warn('[turn] firing final:response turn=%s contentLen=%d', this.id, this._finalContent.length);
                this._onTrigger('final:response', {
                    turnId: this.id,
                    content: this._finalContent,
                    thinking_duration: data.thinking_duration,
                });
            }
            return;
        }

        if (evtName === 'turn_split') {
            this._finalizeBubble(null);
            // ChatUI will create a new Turn for the continuation
            this._onTrigger('turn:split', { turnId: this.id });
            return;
        }

        if (evtName === 'session_clear') {
            // Propagate to ChatUI.onTrigger which calls ChatUI.clear()
            this._onTrigger('session_clear', data);
            return;
        }
    }

    // ── Timeline entries ──────────────────────────────────────────────────────

    _addTimelineEntry(ev) {
        const total = this.$timeline.find('.timeline-entry').length;
        if (total > 0 && total % 10 === 0) {
            console.warn('[turn] _addTimelineEntry count=%d type=%s turn=%s — possible duplicate replay?', total + 1, ev.type, this.id);
        }
        // Remove "Thinking..." placeholder when a new event arrives
        this.$timeline.find('.tl-thinking-pending').remove();

        // Deactivate previous last entry
        const $prevLast = this.$timeline.find('.timeline-entry:last-child');
        if ($prevLast.length) this._deactivateEntry($prevLast);

        const $entry = this._safeRender('timeline:' + ev.type, () => this._renderers.buildTimelineEntry(ev, true));
        if (!$entry) return;

        this.$timeline.append($entry);
        this.$timeline.removeClass('hidden');

        // Update step count badge
        const toolCount = this.$timeline.find('.timeline-entry[data-tool-type="tool_call"]').length;
        const $stepEl = this.$bubble.find('.thinking-step-count');
        if (toolCount > 0) {
            $stepEl.text(toolCount + ' tools').removeClass('hidden');
        }

        this._smartScroll();
    }

    _mergeToolResult(data) {
        const $panel = this.$timeline;
        const $calls = $panel.find('.timeline-entry[data-tool-type="tool_call"]');
        let $target = null;
        $calls.each(function() {
            if ($(this).attr('data-tool-name') === data.tool && !$(this).find('.tl-detail > .mt-2').length) {
                $target = $(this);
                return false; // break
            }
        });
        if (!$target) $target = $calls.last();
        if (!$target || !$target.length) return;

        this._deactivateEntry($target);

        const newBorder = data.error ? 'border-red-300' : 'border-green-300';
        $target.removeClass('border-blue-300 border-transparent').addClass(newBorder).attr('data-border', newBorder);

        const $status = $target.find('.tl-status');
        if ($status.length) {
            $status.html(data.error
                ? '<span class="text-[14px] font-bold leading-none" style="color:#ef4444">&#10005;</span>'
                : '<span class="text-[14px] font-bold leading-none" style="color:#22c55e">&#10003;</span>');
        }

        const $detail = $target.find('.tl-detail');
        if ($detail.length) {
            const resultLabelColor = data.error ? 'text-red-400' : 'text-green-400';
            const $wrapper = $('<div class="mt-2 pt-2 border-t border-gray-100">');
            const labelSpan = $('<span class="text-[10px] uppercase tracking-wide font-semibold block mb-1">').addClass(resultLabelColor).text('Result');
            const resultContent = this._safeRender('tool_result_detail', () => this._renderers.buildToolResultDetail(data));
            $wrapper.append(labelSpan);
            if (resultContent) $wrapper.append(resultContent);
            $detail.append($wrapper);
        }
    }

    // Record a successful save_artifact result so it can be shown as a saved-items
    // strip between the thinking bubble and the final response on finalize.
    // Returns true if a new artifact was captured (so callers can re-render).
    _captureSavedArtifact(data) {
        if (!data || data.tool !== 'save_artifact' || data.error) return false;
        const result = data.result || {};
        const filename = result.filename;
        if (!filename) return false;
        // Dedup by filename (a re-save overwrites) — keep the latest.
        this._savedArtifacts = this._savedArtifacts.filter(a => a.filename !== filename);
        this._savedArtifacts.push({ filename, filepath: result.filepath, size: result.size });
        return true;
    }

    // Render (or update in place) the saved-artifacts strip just below the thinking
    // panel. Called live as each save_artifact succeeds AND again on finalize, so
    // the strip grows incrementally rather than appearing only at the end.
    _renderSavedArtifacts() {
        if (!this._savedArtifacts.length) return;
        const $block = this._safeRender('saved_artifacts', () =>
            buildSavedArtifactsBlock(this._savedArtifacts, { agentIdFallback: this._agentId }));
        if (!$block || !$block.length) return;
        if (this._$savedArtifactsBlock && this._$savedArtifactsBlock.length) {
            // Replace the existing strip with the refreshed list (avoids duplicates).
            this._$savedArtifactsBlock.replaceWith($block);
        } else {
            this.$panel.after($block);
        }
        this._$savedArtifactsBlock = $block;
        this._savedArtifactsRendered = true;
        this._smartScroll();
    }

    _showThinkingRow() {
        if (this._finalized) return; // don't add a pending row to a completed turn
        this.$timeline.find('.tl-thinking-pending').remove();
        const $pending = $('<div class="tl-thinking-pending border-l-2 border-transparent pl-3 py-0.5 relative">');
        $pending.append(
            $('<span class="tl-border-spinner">').append(
                $('<span class="tool-spinner">').css({'border-color': 'rgba(168,85,247,0.15)', 'border-top-color': '#a855f7'})
            ),
            $('<span class="text-[11px] text-purple-500 dark:text-purple-400">').text('Thinking...')
        );
        this.$timeline.append($pending);
        this._smartScroll();
    }

    _deactivateEntry($entry) {
        const savedBorder = $entry.attr('data-border');
        if (savedBorder) $entry.removeClass('border-transparent').addClass(savedBorder);
        $entry.find('.tl-border-spinner').remove();
    }

    // ── Finalization ──────────────────────────────────────────────────────────

    _finalizeBubble(duration) {
        if (this._finalized) { console.warn('[turn] _finalizeBubble SKIP already finalized turn=%s', this.id); return; }
        console.warn('[turn] _finalizeBubble turn=%s duration=%s timelineEntries=%d', this.id, duration, this.$timeline.find('.timeline-entry').length);
        this._finalized = true;
        this._clearTimers();

        this.$bubble.attr('data-finalized', 'true');
        this.$bubble.find('.thinking-spinner').remove();

        const $timer = this.$bubble.find('.thinking-timer-inline');
        if ($timer.length) {
            if (duration != null) {
                $timer.text(Number(duration).toFixed(1) + ' s');
            } else {
                $timer.addClass('hidden');
            }
        }

        this.$timeline.find('.tl-thinking-pending').remove();
        this._deactivateEntry(this.$timeline.find('.timeline-entry:last-child'));

        // Auto-collapse the timeline when turn completes, keeping UI clean
        this.$timeline.addClass('hidden');
        this.$bubble.find('.tool-trace-chevron').removeClass('rotated');

        // Show saved artifacts (if any) between the thinking bubble and final response
        this._renderSavedArtifacts();

        this.phase = 'done';
        this._onPhaseChange(this, 'final', 'done');
        this._log.info('finalized', this.id, 'duration=', duration);
    }

    // ── Approval card ─────────────────────────────────────────────────────────

    _addApprovalCard(data) {
        const entryCount = this.$timeline.find('.timeline-entry').length;
        const pendingCount = this.$timeline.find('.tl-thinking-pending').length;
        this._log.warn('approval card shown — timeline entries:', entryCount, 'pending rows:', pendingCount, 'phase:', this.phase, 'finalized:', this._finalized, 'turn:', this.id);
        console.warn('[approval] card shown. timeline entries=%d pending=%d phase=%s finalized=%s', entryCount, pendingCount, this.phase, this._finalized);
        if (data.approval_id && this.$timeline.find(`[data-approval-id="${CSS.escape(data.approval_id)}"]`).length) return;

        const riskLevel = (data.approval_info && data.approval_info.risk_level) || 'medium';
        const riskColorClass = riskLevel === 'high' ? 'text-red-500 dark:text-red-400'
            : riskLevel === 'low' ? 'text-yellow-500 dark:text-yellow-400'
            : 'text-orange-500 dark:text-orange-400';
        const riskBgClass = riskLevel === 'high' ? 'bg-red-50/80 border-red-300 dark:bg-red-950/40 dark:border-red-800'
            : riskLevel === 'low' ? 'bg-yellow-50/80 border-yellow-300 dark:bg-yellow-950/40 dark:border-yellow-800'
            : 'bg-orange-50/80 border-orange-300 dark:bg-orange-950/40 dark:border-orange-800';
        const description = (data.approval_info && data.approval_info.description) || 'This action requires careful consideration.';
        const reasons = (data.reasons || []);
        const toolArgs = data.tool_args || {};
        const codeSnippet = toolArgs.script || toolArgs.code || null;
        const codeLang = toolArgs.script !== undefined ? 'bash' : 'python';

        const $card = $('<div class="approval-card timeline-entry rounded-lg mb-2">').addClass(riskBgClass).addClass('border').attr('data-approval-id', data.approval_id);

        const $summary = $('<div class="approval-summary hidden items-center gap-2 px-3 py-2 cursor-pointer select-none hover:opacity-80 transition-opacity" title="Click to expand">');
        const $summaryIcon = $('<span class="approval-summary-icon text-xs font-bold">');
        const $summaryTool = $('<span class="text-xs text-gray-600 dark:text-gray-300">').append(
            $('<span class="font-semibold">').text('Tool: '),
            $('<code class="bg-white/60 dark:bg-gray-700/60 px-1 rounded">').text(data.tool || '')
        );
        const $summaryChevron = $('<span class="ml-auto text-[10px] text-gray-400 flex items-center gap-1">').html('details <svg class="approval-chevron inline w-3 h-3 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>');
        $summary.append($summaryIcon, $summaryTool, $summaryChevron);

        const $details = $('<div class="approval-details p-3">');
        $details.append(
            $('<div class="flex items-center gap-2 mb-2">').append(
                $('<span class="text-sm font-semibold">').addClass(riskColorClass).html('&#9888; Approval Required'),
                $('<span class="text-[10px] uppercase tracking-wide font-semibold px-1.5 py-0.5 rounded border border-current">').addClass(riskColorClass).text(riskLevel)
            ),
            $('<div class="text-xs text-gray-700 dark:text-gray-200 mb-1">').append(
                $('<span class="font-semibold">').text('Tool: '),
                $('<code class="bg-white/60 dark:bg-gray-700/60 px-1 rounded">').text(data.tool || '')
            ),
            $('<div class="text-xs text-gray-600 dark:text-gray-300 mb-2">').text(description)
        );

        if (reasons.length) {
            const $ul = $('<ul class="text-xs text-gray-600 dark:text-gray-300 list-disc list-inside mb-2 space-y-0.5">');
            reasons.forEach(r => $ul.append($('<li>').text(r)));
            $details.append($ul);
        }

        if (codeSnippet) {
            $details.append(
                $('<div class="mb-2">').append(
                    $('<div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">').text('Code ').append($('<span class="font-normal text-gray-400">').text('(' + codeLang + ')')),
                    $('<pre class="text-xs bg-gray-900 text-gray-100 rounded p-2 overflow-auto max-h-48 whitespace-pre-wrap break-all">').append($('<code>').text(codeSnippet))
                )
            );
        }

        const $actions = $('<div class="approval-actions flex gap-2 mt-2">');
        const $approveBtn = $('<button class="approve-btn text-xs font-semibold px-3 py-1.5 rounded bg-green-600 text-white hover:bg-green-700 transition-colors">').text('Approve');
        const $rejectBtn = $('<button class="reject-btn text-xs font-semibold px-3 py-1.5 rounded bg-red-600 text-white hover:bg-red-700 transition-colors">').text('Reject');
        $actions.append($approveBtn, $rejectBtn);

        const $statusEl = $('<div class="approval-status text-xs font-semibold mt-1 hidden">');
        $details.append($actions, $statusEl);
        $card.append($summary, $details);

        $approveBtn.on('click', () => this._submitApproval(data.approval_id, 'approve', $card));
        $rejectBtn.on('click', () => this._submitApproval(data.approval_id, 'reject', $card));
        $summary.on('click', () => {
            const isHidden = $details.toggleClass('hidden').hasClass('hidden');
            $summary.find('.approval-chevron').css('transform', isHidden ? '' : 'rotate(180deg)');
        });

        this.$timeline.append($card);
        this.$timeline.removeClass('hidden');
        this._smartScroll();
    }

    async _submitApproval(approvalId, decision, $card) {
        $card.find('.approval-actions button').prop('disabled', true);
        try {
            const res = await fetch(`/api/agents/${encodeURIComponent(this._agentId)}/chat/approve`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({approval_id: approvalId, decision}),
            });
            if (res.ok) {
                this._markApprovalResolved($card, decision, false);
            } else {
                const body = await res.json().catch(() => ({}));
                $card.find('.approval-status').text(body.error || 'Failed.').removeClass('hidden').addClass('text-red-600');
                $card.find('.approval-actions button').prop('disabled', false);
            }
        } catch (e) {
            $card.find('.approval-status').text('Network error.').removeClass('hidden').addClass('text-red-600');
            $card.find('.approval-actions button').prop('disabled', false);
        }
    }

    _resolveApprovalCard(data) {
        const $card = this.$timeline.find(`[data-approval-id="${CSS.escape(data.approval_id)}"]`);
        if ($card.length) this._markApprovalResolved($card, data.decision, data.timed_out);
    }

    _markApprovalResolved($card, decision, timedOut) {
        $card.find('.approval-actions').addClass('hidden');
        let statusText, statusClass, iconHtml;
        if (timedOut) {
            statusText = 'Timed out — auto-rejected.'; statusClass = 'text-gray-500'; iconHtml = '&#x23F1; Timed out';
        } else if (decision === 'approve') {
            statusText = 'Approved — executing...'; statusClass = 'text-green-600'; iconHtml = '&#10003; Approved';
        } else {
            statusText = 'Rejected.'; statusClass = 'text-red-500'; iconHtml = '&#10007; Rejected';
        }
        $card.find('.approval-status').text(statusText).removeClass('hidden').addClass(statusClass);
        $card.find('.approval-summary-icon').html(iconHtml);
        $card.find('.approval-summary').removeClass('hidden').addClass('flex');
        $card.find('.approval-details').addClass('hidden');
    }

    // ── Transport ─────────────────────────────────────────────────────────────

    attach(transport) {
        this._transports.push(transport);
        transport.start((evt) => this.ingest(evt));
    }

    // ── Disposal ──────────────────────────────────────────────────────────────

    dispose(reason) {
        this._log.info('dispose', this.id, 'reason=', reason);
        this._clearTimers();
        this._transports.forEach(t => { try { t.stop(); } catch(e) {} });
        this._transports = [];

        // Only remove DOM if not finalized
        if (!this._finalized) {
            this.$bubble.remove();
            this.$panel.remove();
            // The live saved-artifacts strip is a sibling of $panel — remove it too
            // so an aborted turn doesn't leave an orphaned strip behind.
            if (this._$savedArtifactsBlock) this._$savedArtifactsBlock.remove();
        }

        this._onDispose(this, reason);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _safeRender(name, fn) {
        try {
            return fn();
        } catch (err) {
            log('render').error(`renderer "${name}" threw`, err);
            this._onTrigger('renderer:error', { renderer: name, error: err });
            return $('<div class="text-xs text-red-400 italic px-2 py-1">').text(`[render error in ${name}: ${err.message}]`);
        }
    }

    _smartScroll() {
        // Defer the scroll check to the next animation frame to avoid forced synchronous
        // layout reflows during batch replay (many entries inserted in a tight loop).
        if (this._scrollRAF) return; // already scheduled for this frame
        this._scrollRAF = requestAnimationFrame(() => {
            this._scrollRAF = null;
            const c = this._$container[0];
            if (!c) return;
            if (c.scrollHeight - c.scrollTop - c.clientHeight < 300) {
                c.scrollTop = c.scrollHeight;
            }
        });
    }
}
