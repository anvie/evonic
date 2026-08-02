/**
 * transport.js — SSEAdapter, PollingAdapter, ReplayAdapter.
 *
 * All three share the same interface:
 *   start(handler)   // calls handler({ event, data, seq }) for each TurnEvent
 *   stop()
 */

import { log } from './debug.js';

// ── SSEAdapter ────────────────────────────────────────────────────────────────

const SSE_EVENTS = [
    'turn_begin', 'turn_split', 'thinking', 'tool_call_started', 'tool_executed',
    'state:changed', 'tasks:auto_transition', 'tasks:stale', 'response_chunk', 'done', 'approval_required', 'approval_resolved', 'retry',
    'message_injected', 'message_injection_applied', 'message_received', 'whatsapp_restriction_warning', 'session_clear',
    'state_changed',
    'heartbeat',
];

// If no event (including heartbeats) arrives within this window, the connection
// is assumed dead and the adapter will force-reconnect.
const LIVENESS_TIMEOUT_MS = 45_000;

export class SSEAdapter {
    /**
     * @param {string} url         - SSE stream URL
     * @param {object} [opts]
     * @param {string} [opts.agentId]
     * @param {string} [opts.sessionId]
     * @param {number} [opts.afterSeq=0]  - resume from this seq (gap-fill will request from here)
     */
    constructor(url, opts = {}) {
        this._url = url;
        this._agentId = opts.agentId || '';
        this._sessionId = opts.sessionId || (new URL(url, window.location.origin).searchParams.get('session_id') || '');
        this._lastSeq = opts.afterSeq || 0;
        this._handler = null;
        this._es = null;
        this._fillingGap = false;
        this._pendingQueue = [];
        this._log = log('sse');
        this._lastEventAt = 0;
        this._livenessInterval = null;
        this._usingUnified = false; // true when using unified /api/realtime/stream
        this._reconnectAttempts = 0; // consecutive immediate failures (for backoff)
        this._connectStartTime = 0;  // when the current EventSource was opened
    }

    start(handler) {
        this._handler = handler;
        this._intentionallyStopped = false;
        this._connect(this._url);
    }

    stop() {
        this._intentionallyStopped = true;
        if (this._livenessInterval) {
            clearInterval(this._livenessInterval);
            this._livenessInterval = null;
        }
        if (this._es) {
            this._es.close();
            this._es = null;
        }
    }

    _connect(url) {
        // Rewrite legacy chat stream URLs to unified realtime endpoint
        if (url.indexOf('/api/agents/') !== -1 && url.indexOf('/chat/stream') !== -1) {
            const u = new URL(url, window.location.origin);
            const agentId = this._agentId || (url.match(/\/agents\/([^/?]+)\//) || [])[1] || '';
            const sessionId = this._sessionId || u.searchParams.get('session_id') || '';
            const after = this._lastSeq;
            let newUrl = '/api/realtime/stream?chat=1';
            if (agentId) newUrl += '&agent_id=' + encodeURIComponent(agentId);
            if (sessionId) newUrl += '&session_id=' + encodeURIComponent(sessionId);
            if (after > 0) newUrl += '&after=' + after;
            this._usingUnified = true;
            url = newUrl;
        }

        this._log.info('open', url);
        const es = new EventSource(url);
        this._es = es;
        this._lastEventAt = Date.now();
        this._connectStartTime = Date.now();

        // Clear any previous liveness interval before starting a new one
        if (this._livenessInterval) clearInterval(this._livenessInterval);
        this._livenessInterval = setInterval(() => {
            if (this._intentionallyStopped) return;
            const elapsed = Date.now() - this._lastEventAt;
            if (elapsed > LIVENESS_TIMEOUT_MS) {
                this._log.warn('liveness timeout — no event for', elapsed, 'ms, forcing reconnect');
                console.warn('[sse] liveness timeout, elapsed=', elapsed, '_lastSeq=', this._lastSeq);
                if (this._es) { this._es.close(); this._es = null; }
                let resumeUrl;
                if (this._usingUnified) {
                    const agentId = this._agentId || '';
                    const sessionId = this._sessionId || '';
                    resumeUrl = '/api/realtime/stream?chat=1';
                    if (agentId) resumeUrl += '&agent_id=' + encodeURIComponent(agentId);
                    if (sessionId) resumeUrl += '&session_id=' + encodeURIComponent(sessionId);
                    if (this._lastSeq > 0) resumeUrl += '&after=' + this._lastSeq;
                } else {
                    const u = new URL(url, window.location.origin);
                    if (this._lastSeq > 0) u.searchParams.set('after', this._lastSeq);
                    resumeUrl = u.pathname + u.search;
                }
                this._connect(resumeUrl);
            }
        }, LIVENESS_TIMEOUT_MS);

        for (const evtName of SSE_EVENTS) {
            es.addEventListener(evtName, (e) => {
                this._lastEventAt = Date.now();
                this._reconnectAttempts = 0; // a message arrived → connection is healthy
                let data;
                try { data = JSON.parse(e.data); } catch (err) { data = {}; }
                this._log.debug('event', evtName, 'seq', data.seq, 'size', e.data.length);
                if (evtName === 'heartbeat') return;  // liveness only — don't process
                this._handleRaw(evtName, data);
            });
        }

        es.onerror = () => {
            this._log.warn('SSE error/closed', url);
            console.warn('[sse] error/closed _lastSeq=', this._lastSeq, '_fillingGap=', this._fillingGap, '_pendingQueue=', this._pendingQueue.length);
            es.close();
            if (this._es === es) this._es = null;
            // Only reconnect if this was NOT an intentional stop (e.g. after 'done')
            if (this._intentionallyStopped) {
                this._log.info('intentionally stopped — no reconnect');
                return;
            }
            // Back off when the connection fails almost immediately (e.g. the server
            // rejects it with 429 too_many_sse_connections or 503 server-busy).
            // EventSource doesn't expose the HTTP status, so use the open duration:
            // a connection that survived a while was healthy → reconnect promptly;
            // one that died right away → exponential backoff up to 30s (matching the
            // server's retry_after) so a reconnect storm can't keep the SSE limit pegged.
            const openMs = Date.now() - this._connectStartTime;
            let delay;
            if (openMs > 10_000) {
                this._reconnectAttempts = 0;
                delay = 2000;
            } else {
                this._reconnectAttempts++;
                delay = Math.min(2000 * Math.pow(2, this._reconnectAttempts - 1), 30_000);
                if (this._reconnectAttempts > 1) {
                    this._log.warn('repeated immediate SSE failure — backing off', delay, 'ms', 'attempt', this._reconnectAttempts);
                    console.warn('[sse] backing off', delay, 'ms after', this._reconnectAttempts, 'immediate failures');
                }
            }
            setTimeout(() => {
                if (this._intentionallyStopped) return;
                if (this._es) return; // another stream already started
                let resumeUrl;
                if (this._usingUnified) {
                    const agentId = this._agentId || '';
                    const sessionId = this._sessionId || '';
                    resumeUrl = '/api/realtime/stream?chat=1';
                    if (agentId) resumeUrl += '&agent_id=' + encodeURIComponent(agentId);
                    if (sessionId) resumeUrl += '&session_id=' + encodeURIComponent(sessionId);
                    if (this._lastSeq > 0) resumeUrl += '&after=' + this._lastSeq;
                } else {
                    const u = new URL(url, window.location.origin);
                    if (this._lastSeq > 0) u.searchParams.set('after', this._lastSeq);
                    resumeUrl = u.pathname + u.search;
                }
                this._log.info('reconnecting from seq', this._lastSeq, resumeUrl);
                console.warn('[sse] reconnecting _lastSeq=', this._lastSeq, '_fillingGap=', this._fillingGap, '_pendingQueue=', this._pendingQueue.length, 'url=', resumeUrl);
                this._connect(resumeUrl);
            }, delay);
        };
    }

    _handleRaw(evtName, data) {
        const seq = data.seq || 0;

        if (this._fillingGap) {
            this._log.debug('queued while filling gap', evtName, 'seq', seq, 'queueLen', this._pendingQueue.length);
            if (this._pendingQueue.length >= 1 && this._pendingQueue.length % 10 === 0) {
                console.warn('[sse] pendingQueue grew to', this._pendingQueue.length, 'while filling gap — possible reconnect storm');
            }
            this._pendingQueue.push({ evtName, data });
            return;
        }

        if (seq && seq <= this._lastSeq) {
            this._log.debug('dedup skip', evtName, 'seq', seq, '≤ lastSeq', this._lastSeq);
            console.log('[sse] dedup skip', evtName, 'seq=', seq, '_lastSeq=', this._lastSeq);
            return;
        }

        if (seq && this._lastSeq > 0 && seq > this._lastSeq + 1) {
            this._log.warn('seq gap detected', this._lastSeq, '→', seq, '— filling');
            this._fillingGap = true;
            this._pendingQueue.push({ evtName, data });
            this._fillGap(this._lastSeq, seq).then(() => {
                this._fillingGap = false;
                console.warn('[sse] draining pendingQueue len=', this._pendingQueue.length, '_lastSeq=', this._lastSeq);
                this._drainQueue();
            });
            return;
        }

        if (seq) this._lastSeq = seq;
        this._dispatch(evtName, data);
    }

    async _fillGap(afterSeq, upToSeq) {
        try {
            const agentId = this._agentId || this._url.match(/\/agents\/([^/?]+)\//)?.[1] || '';
            const res = await $.getJSON(
                `/api/agents/${encodeURIComponent(agentId)}/chat/events?session_id=${encodeURIComponent(this._sessionId)}&after=${afterSeq}&up_to=${upToSeq}`
            );
            const evts = res.events || [];
            this._log.warn('gap-fill response: afterSeq=' + afterSeq + ' upToSeq=' + upToSeq + ' returned=' + evts.length + ' seqs=' + evts.map(e=>e.seq).join(','));
            console.warn('[gap-fill] returned', evts.length, 'events for after=', afterSeq, 'up_to=', upToSeq, 'seqs:', evts.map(e=>e.seq).join(','));
            for (const ev of evts) {
                if (ev.seq <= this._lastSeq) continue;
                this._lastSeq = ev.seq;
                this._dispatch(ev.event, ev.data);
            }
        } catch (err) {
            this._log.warn('gap-fill failed', err, '— skipping gap');
            this._lastSeq = upToSeq - 1;
        }
    }

    // Drain _pendingQueue asynchronously — one event per animation frame so we
    // never block the main thread with a large synchronous burst.
    _drainQueue() {
        if (this._pendingQueue.length === 0) {
            console.warn('[sse] queue drain done _lastSeq=', this._lastSeq);
            return;
        }
        const item = this._pendingQueue.shift();
        const itemSeq = item.data.seq || 0;
        if (itemSeq && itemSeq <= this._lastSeq) {
            console.log('[sse] queue dedup skip', item.evtName, 'seq=', itemSeq);
            // Skip but continue draining without waiting — dedup is cheap
            this._drainQueue();
            return;
        }
        if (itemSeq) this._lastSeq = itemSeq;
        this._dispatch(item.evtName, item.data);
        // Yield to the browser between each real event
        requestAnimationFrame(() => this._drainQueue());
    }

    _dispatch(evtName, data) {
        if (evtName === 'state_changed') {
            // Not turn-scoped — bridge straight to the document-level event that
            // agent_detail.html / sessions.html already listen for (debounced refresh).
            document.dispatchEvent(new CustomEvent('evonic:agent-state-changed', { detail: data }));
            return;
        }
        if (evtName === 'message_received') {
            document.dispatchEvent(new CustomEvent('evonic:message-received', { detail: data }));
            return;
        }
        if (evtName === 'session_clear') {
            this._handler({ event: 'session_clear', data, seq: data.seq || 0 });
            return;
        }
        // done: stop reconnecting after this
        if (evtName === 'done') {
            console.warn('[sse] _dispatch done _lastSeq=', this._lastSeq, 'data.seq=', data.seq);
            this._handler({ event: 'done', data, seq: data.seq || 0 });
            this.stop();
            return;
        }
        this._handler({ event: evtName, data, seq: data.seq || 0 });
    }
}

// ── PollingAdapter ────────────────────────────────────────────────────────────

/**
 * Polls the JSONL history endpoint for new entries, converts them to TurnEvents.
 * Useful as a fallback when SSE fails or for initial replay.
 */
export class PollingAdapter {
    /**
     * @param {string} baseUrl     - e.g. `/api/agents/${id}/chat`
     * @param {object} opts
     * @param {string} [opts.sessionId]
     * @param {number} [opts.intervalMs=1000]
     * @param {number} [opts.startTs=0]       - only fetch entries newer than this ts
     */
    constructor(baseUrl, opts = {}) {
        this._baseUrl = baseUrl;
        this._sessionId = opts.sessionId || '';
        this._intervalMs = opts.intervalMs || 1000;
        this._lastTs = opts.startTs || 0;
        this._handler = null;
        this._timer = null;
        this._running = false;
        this._log = log('poll');
    }

    start(handler) {
        this._handler = handler;
        this._running = true;
        this._timer = setInterval(() => this._tick(), this._intervalMs);
        this._log.info('polling started', this._baseUrl);
    }

    stop() {
        this._running = false;
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
        this._log.info('polling stopped');
    }

    async _tick() {
        if (!this._running) return;
        try {
            const qs = this._sessionId
                ? `session_id=${encodeURIComponent(this._sessionId)}&after_ts=${this._lastTs}&limit=50`
                : `user_id=web_test&after_ts=${this._lastTs}&limit=50`;
            const data = await $.getJSON(`${this._baseUrl}?${qs}`);
            const entries = data.entries || [];
            this._log.debug('tick delta', entries.length, 'lastTs', this._lastTs);
            if (!entries.length) return;

            for (const entry of entries) {
                if (entry.ts <= this._lastTs) continue;
                this._lastTs = entry.ts;

                // Convert JSONL entry to TurnEvent
                const evt = this._entryToEvent(entry);
                if (evt) {
                    // Progressive: one event per rAF to avoid dumping all at once
                    await new Promise(r => requestAnimationFrame(r));
                    this._handler(evt);
                }
            }
        } catch (e) {
            this._log.warn('poll tick error', e);
        }
    }

    _entryToEvent(entry) {
        if (entry.type === 'final') {
            return { event: 'done', data: { thinking_duration: (entry.metadata || {}).thinking_duration }, seq: 0, _entry: entry };
        }
        if (entry.type === 'error') {
            return { event: 'done', data: { thinking_duration: null, error: true }, seq: 0, _entry: entry };
        }
        if (entry.type === 'thinking') {
            return { event: 'thinking', data: { content: entry.content }, seq: 0 };
        }
        if (entry.type === 'tool_call') {
            return { event: 'tool_call_started', data: { tool: entry.function, args: entry.params || {}, param_types: {} }, seq: 0 };
        }
        if (entry.type === 'tool_output') {
            let result;
            try { result = JSON.parse(entry.content); } catch (e) { result = { data: entry.content }; }
            return { event: 'tool_executed', data: { tool: entry.function, result, error: !!entry.error }, seq: 0 };
        }
        if (entry.type === 'intermediate') {
            return { event: 'response_chunk', data: { content: entry.content, is_final: false }, seq: 0 };
        }
        return null;
    }
}

// ── ReplayAdapter ─────────────────────────────────────────────────────────────

/**
 * Emits events from a static history array.
 * batch:true → synchronous (instant render of finalized turns).
 * batch:false → one event per rAF (progressive animation).
 */
export class ReplayAdapter {
    /**
     * @param {Array} events  - array of { event, data, seq } objects
     * @param {object} [opts]
     * @param {boolean} [opts.batch=true]
     */
    constructor(events, opts = {}) {
        this._events = events;
        this._batch = opts.batch !== false;
        this._handler = null;
        this._stopped = false;
        this._log = log('replay');
    }

    start(handler) {
        this._handler = handler;
        if (this._batch) {
            for (const evt of this._events) {
                if (this._stopped) break;
                handler(evt);
            }
        } else {
            this._playAsync();
        }
    }

    stop() {
        this._stopped = true;
    }

    async _playAsync() {
        for (const evt of this._events) {
            if (this._stopped) break;
            await new Promise(r => requestAnimationFrame(r));
            this._handler(evt);
        }
    }
}
