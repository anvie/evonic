/**
 * RealtimeClient — Unified SSE multiplexed connection manager.
 *
 * Consolidates all 5 EventSource connections into one multiplexed
 * connection to /api/realtime/stream with per-channel event routing.
 *
 * Usage:
 *   const rt = new RealtimeClient({
 *     channels: ['status', 'approvals', 'update'],
 *     chat: 1,
 *     sessionId: 'abc123',
 *     agentId: 'my-agent',
 *     workplace: 'wp-1',
 *     chatThrottle: 100,
 *   });
 *
 *   rt.on('status', 'agent_busy_changed', (data) => { ... });
 *   rt.on('approvals', 'approval_required', (data) => { ... });
 *   rt.start();
 *   rt.stop();
 */
var RealtimeClient = (function () {
    'use strict';

    // ---- Channel definitions ----
    var CHANNEL_PRIORITY = {
        status: 0, update: 0,     // Level 0: system/update
        approvals: 1,              // Level 1: user-facing
        chat: 2, workplace: 2,     // Level 2: high throughput
        heartbeat: 0,
        auth_expired: 0,
        channel_disabled: 0,
    };

    // Per-channel resume sequence trackers
    var _channelSeqs = {};
    var _channelIds = {};  // channel -> last SSE id

    function RealtimeClient(opts) {
        opts = opts || {};
        this._channels = (opts.channels || 'status,approvals,update').split(',').map(function (s) { return s.trim(); });
        this._chat = opts.chat ? 1 : 0;
        this._sessionId = opts.sessionId || '';
        this._agentId = opts.agentId || '';
        this._after = opts.after || 0;
        this._workplace = opts.workplace || '';
        this._chatThrottle = opts.chatThrottle || 100;
        this._es = null;
        this._handlers = {};      // channel -> [handler]
        this._started = false;
        this._intentionallyStopped = false;
        this._paused = false;
        this._pauseBuffer = {};   // channel -> [events] buffered during pause
        this._onAuthExpired = opts.onAuthExpired || function () { window.location.href = '/login'; };
        this._visibilityBound = false;
        this._unloadHandlers = [];  // cleanup hooks registered by consumers
    }

    // ---- Public API ----

    RealtimeClient.prototype.on = function (channel, event, handler) {
        // Primary API: on(channel, event, handler). Back-compat: on(channel, handler)
        // registers a channel-wide listener (event '*').
        if (typeof event === 'function') { handler = event; event = '*'; }
        if (!this._handlers[channel]) this._handlers[channel] = [];
        this._handlers[channel].push({ event: event, handler: handler });
    };

    RealtimeClient.prototype.off = function (channel, event, handler) {
        if (typeof event === 'function') { handler = event; event = '*'; }
        var list = this._handlers[channel];
        if (!list) return;
        this._handlers[channel] = list.filter(function (h) {
            return !(h.event === event && h.handler === handler);
        });
    };

    RealtimeClient.prototype.start = function () {
        if (this._started) return;
        this._started = true;
        this._intentionallyStopped = false;
        this._connect();
        this._bindVisibility();
    };

    RealtimeClient.prototype.stop = function () {
        this._intentionallyStopped = true;
        this._started = false;
        this._disconnect();
    };

    RealtimeClient.prototype.pause = function () {
        if (this._paused) return;
        this._paused = true;
        if (this._es && this._es.readyState === EventSource.OPEN) {
            // Send pause signal via a separate fetch
            this._sendCommand('pause');
        }
    };

    RealtimeClient.prototype.resume = function () {
        if (!this._paused) return;
        this._paused = false;
        if (this._es && this._es.readyState === EventSource.OPEN) {
            this._sendCommand('resume');
        }
        // Replay buffered events
        var self = this;
        Object.keys(this._pauseBuffer).forEach(function (ch) {
            var buf = self._pauseBuffer[ch];
            while (buf && buf.length) {
                var item = buf.shift();
                self._dispatch(ch, item.evtName, item.data);
            }
        });
    };

    // ---- Internal: Connection lifecycle ----

    RealtimeClient.prototype._buildUrl = function () {
        var params = [];
        params.push('channels=' + encodeURIComponent(this._channels.join(',')));
        if (this._chat) {
            params.push('chat=1');
            if (this._sessionId) params.push('session_id=' + encodeURIComponent(this._sessionId));
            if (this._agentId) params.push('agent_id=' + encodeURIComponent(this._agentId));
            if (this._after) params.push('after=' + this._after);
        }
        if (this._workplace) params.push('workplace=' + encodeURIComponent(this._workplace));
        if (this._chatThrottle) params.push('chat_throttle=' + this._chatThrottle);
        return '/api/realtime/stream?' + params.join('&');
    };

    RealtimeClient.prototype._connect = function () {
        if (this._intentionallyStopped) return;
        var self = this;
        var url = this._buildUrl();

        try {
            this._es = new EventSource(url);
        } catch (e) {
            // EventSource not supported — fatal
            console.warn('[realtime] EventSource not supported');
            return;
        }

        var es = this._es;

        es.onopen = function () {
            console.log('[realtime] connected');
        };

        es.onmessage = function (e) {
            // Catch-all for events without named event: field
            // (SSE spec: unnamed events go to onmessage)
            try {
                var data = JSON.parse(e.data);
                self._routeEvent('message', data, e.lastEventId);
            } catch (_) {}
        };

        // Register named event listeners — one per possible event type.
        // Double try/catch isolation: outer catch handles JSON parse errors,
        // inner catch (in _dispatch) handles handler errors.
        var ALL_EVENTS = [
            'agent_busy_changed', 'agent_turn_complete', 'whatsapp_bridge_status',
            'approval_required', 'approval_resolved',
            'update_status', 'update_done',
            'turn_begin', 'thinking', 'tool_call_started', 'tool_executed',
            'response_chunk', 'done', 'retry', 'message_injected',
            'message_injection_applied', 'whatsapp_restriction_warning', 'session_clear', 'turn_split',
            'connector_connected', 'connector_disconnected', 'connector_paired',
            'workplace_status_changed',
            'heartbeat', 'auth_expired', 'channel_disabled',
        ];

        ALL_EVENTS.forEach(function (evtName) {
            es.addEventListener(evtName, function (e) {
                var data;
                try { data = JSON.parse(e.data); } catch (_) { data = {}; }
                self._routeEvent(evtName, data, e.lastEventId);
            });
        });

        es.onerror = function () {
            if (self._intentionallyStopped) return;
            es.close();
            if (self._es === es) self._es = null;
            // Auto-reconnect with jitter
            var delay = 2000 + Math.floor(Math.random() * 5000);
            setTimeout(function () {
                if (self._intentionallyStopped) return;
                self._connect();
            }, delay);
        };
    };

    RealtimeClient.prototype._disconnect = function () {
        if (this._es) {
            this._es.close();
            this._es = null;
        }
    };

    RealtimeClient.prototype._sendCommand = function (cmd) {
        try {
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/realtime/' + cmd, true);
            xhr.send();
        } catch (_) {}
    };

    // ---- Internal: Event routing ----

    RealtimeClient.prototype._routeEvent = function (evtName, data, lastEventId) {
        // Track per-channel seq from composite SSE id (e.g. "chat:892")
        if (lastEventId) {
            var parts = lastEventId.split(':');
            if (parts.length === 2) {
                _channelIds[parts[0]] = lastEventId;
                _channelSeqs[parts[0]] = parseInt(parts[1], 10) || 0;
            }
        }

        // Map event name to channel
        var channel = this._eventToChannel(evtName);

        // Handle special events
        if (evtName === 'auth_expired') {
            this.stop();
            if (this._onAuthExpired) this._onAuthExpired(data);
            return;
        }

        if (evtName === 'channel_disabled') {
            var ch = data.channel;
            console.warn('[realtime] channel disabled:', ch);
            this._dispatch('channel_disabled', 'channel_disabled', data);
            return;
        }

        if (evtName === 'heartbeat') return; // no-op

        // Pause buffering for chat/workplace events
        if (this._paused && (channel === 'chat' || channel === 'workplace')) {
            if (!this._pauseBuffer[channel]) this._pauseBuffer[channel] = [];
            if (this._pauseBuffer[channel].length < 100) {
                this._pauseBuffer[channel].push({ evtName: evtName, data: data });
            }
            return;
        }

        this._dispatch(channel, evtName, data);
    };

    RealtimeClient.prototype._dispatch = function (channel, evtName, data) {
        var handlers = this._handlers[channel];
        if (!handlers || !handlers.length) return;
        // Double try/catch isolation: each handler gets its own try/catch.
        // A handler fires when its registered event matches the SSE event name,
        // tolerating the channel prefix (e.g. event 'status' on channel 'update'
        // matches SSE event 'update_status'), or when it is a channel-wide ('*').
        for (var i = 0; i < handlers.length; i++) {
            var h = handlers[i];
            if (h.event !== '*' && h.event !== evtName &&
                (channel + '_' + h.event) !== evtName) continue;
            try {
                h.handler(data);
            } catch (e) {
                console.error('[realtime] handler error on channel', channel, e);
            }
        }
    };

    /**
     * Map SSE event name to logical channel.
     * This is the key routing table — maps 20+ event types to 5 channels.
     */
    RealtimeClient.prototype._eventToChannel = function (evtName) {
        // Status channel events
        if (evtName === 'agent_busy_changed' || evtName === 'agent_turn_complete' ||
            evtName === 'whatsapp_bridge_status') {
            return 'status';
        }
        // Approval channel events
        if (evtName === 'approval_required' || evtName === 'approval_resolved') {
            return 'approvals';
        }
        // Update channel events
        if (evtName === 'update_status' || evtName === 'update_done') {
            return 'update';
        }
        // Chat channel events
        if (evtName === 'turn_begin' || evtName === 'thinking' ||
            evtName === 'tool_call_started' || evtName === 'tool_executed' ||
            evtName === 'response_chunk' || evtName === 'done' ||
            evtName === 'retry' || evtName === 'message_injected' ||
            evtName === 'message_injection_applied' ||
            evtName === 'whatsapp_restriction_warning' || evtName === 'session_clear' ||
            evtName === 'turn_split') {
            return 'chat';
        }
        // Workplace channel events
        if (evtName === 'connector_connected' || evtName === 'connector_disconnected' ||
            evtName === 'connector_paired' || evtName === 'workplace_status_changed') {
            return 'workplace';
        }
        // System events
        return evtName; // passthrough: heartbeat, auth_expired, channel_disabled
    };

    // ---- Visibility change handling ----

    RealtimeClient.prototype._bindVisibility = function () {
        if (this._visibilityBound) return;
        this._visibilityBound = true;
        var self = this;
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) {
                self.pause();
            } else {
                self.resume();
            }
        });
    };

    // ---- Unload hook (consumers register cleanup callbacks) ----

    RealtimeClient.prototype.onUnload = function (fn) {
        this._unloadHandlers.push(fn);
    };

    // ---- SSE comment handler (invoked by caller when EventSource
    //      comment events are intercepted — see x-sse-dropped below) ----

    RealtimeClient.prototype._handleComment = function (comment) {
        // :x-sse-dropped N — server lost N events on a channel
        var match = comment.match(/^x-sse-dropped\s+(\d+)/);
        if (match) {
            var dropped = parseInt(match[1], 10);
            console.warn('[realtime] stream thinned:', dropped, 'events dropped');
            this._dispatch('stream_thinned', { dropped: dropped });
        }
        // :error channel=<name> — producer error
        var errMatch = comment.match(/^error\s+channel=(\S+)/);
        if (errMatch) {
            console.warn('[realtime] producer error on channel:', errMatch[1]);
        }
    };

    return RealtimeClient;
})();

/**
 * getSharedRealtime([opts]) — Returns the single shared RealtimeClient instance
 * for global channels (status, approvals, update). The first caller triggers
 * creation + start; subsequent callers receive the same instance for handler
 * registration. The singleton is creation-order agnostic — any script can call
 * it at any point during page load and handlers registered before start() will
 * fire on the first connection.
 *
 * Options (accepted only on the first call):
 *   opts.workplace  — if provided, creates a separate workplace-scoped instance
 *                     keyed by workplace ID, not the global singleton.
 */
function getSharedRealtime(opts) {
    opts = opts || {};

    // Workplace-scoped instances are separate from the global singleton.
    var workplaceId = opts.workplace;
    if (workplaceId) {
        var wpKey = '_evWorkplaceRT_' + workplaceId;
        if (!window[wpKey]) {
            window[wpKey] = new RealtimeClient({
                channels: '',
                workplace: workplaceId
            });
            window[wpKey].start();
        }
        return window[wpKey];
    }

    // Global singleton for status, approvals, update.
    if (!window._evSharedRT) {
        window._evSharedRT = new RealtimeClient({
            channels: 'status,approvals,update'
        });
        window._evSharedRT.start();

        // Single consolidated beforeunload handler.
        window.addEventListener('beforeunload', function () {
            if (window._evSharedRT) {
                var rt = window._evSharedRT;
                // Run registered consumer cleanup hooks before stopping.
                for (var i = 0; i < rt._unloadHandlers.length; i++) {
                    try { rt._unloadHandlers[i](); } catch (_) {}
                }
                rt.stop();
            }
        });
    }
    return window._evSharedRT;
}
