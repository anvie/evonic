'use strict';

const DELIVERY_STATUS = 3;
const FAILED_STATUS = 0;

function keyId(key) {
    return key?.id || '';
}

function failureCode(update) {
    const error = update?.error;
    return String(update?.messageStubParameters?.[0]
        || error?.output?.payload?.statusCode
        || error?.data?.statusCode
        || '');
}

function failureReason(update) {
    const code = failureCode(update);
    const error = update?.error;
    const message = error?.message || error?.output?.payload?.message
        || 'WhatsApp rejected the message';
    return code ? `${message} (${code})` : message;
}

class OutboundLifecycle {
    constructor({ send, emit, diagnoseFailure = null, ttlMs = 60 * 60 * 1000 }) {
        this.send = send;
        this.emit = emit;
        this.diagnoseFailure = diagnoseFailure;
        this.ttlMs = ttlMs;
        this.connected = false;
        this.byCorrelation = new Map();
        this.byKey = new Map();
        // A NACK can arrive before sock.sendMessage() resolves and exposes its key.
        // Hold those updates briefly, then replay them as soon as the key is known.
        this.pendingUpdates = new Map();
        this.pendingUpdateTtlMs = 30 * 1000;
        this.maxPendingUpdates = 1000;
    }

    async accept({ correlationId, jid, content, metadata = {} }) {
        this.prune();
        if (this.byCorrelation.has(correlationId)) {
            return this.snapshot(this.byCorrelation.get(correlationId));
        }
        const entry = {
            correlationId, jid, content, metadata,
            status: 'sending', keys: new Set(), activeKey: null,
            createdAt: Date.now(),
        };
        this.byCorrelation.set(correlationId, entry);
        await this.sendAttempt(entry);
        return this.snapshot(entry);
    }

    async sendAttempt(entry) {
        try {
            const result = await this.send(entry.jid, entry.content);
            const id = keyId(result?.key);
            if (!id) throw new Error('Baileys returned no message key');
            entry.keys.add(id);
            entry.activeKey = id;
            this.byKey.set(id, entry);
            entry.status = 'accepted';
            this.emitStatus(entry, 'accepted', { baileys_message_id: id, jid: entry.jid });
            const pending = this.pendingUpdates.get(id);
            this.pendingUpdates.delete(id);
            if (pending) await this.onMessageUpdates(pending.updates);
        } catch (error) {
            await this.fail(entry, error?.message || String(error), false);
        }
    }

    async onMessageUpdates(updates = []) {
        this.prune();
        for (const item of updates) {
            const messageId = keyId(item?.key);
            const update = item?.update || {};
            const isFailure = update.status === FAILED_STATUS || update.error;
            const isDelivery = update.status >= DELIVERY_STATUS;
            const entry = this.byKey.get(messageId);
            if (!entry) {
                if (messageId && (isFailure || isDelivery)) {
                    if (this.pendingUpdates.size >= this.maxPendingUpdates
                            && !this.pendingUpdates.has(messageId)) {
                        this.pendingUpdates.delete(this.pendingUpdates.keys().next().value);
                    }
                    const buffered = this.pendingUpdates.get(messageId)
                        || { createdAt: Date.now(), updates: [] };
                    if (buffered.updates.length < 4) buffered.updates.push(item);
                    this.pendingUpdates.set(messageId, buffered);
                }
                continue;
            }
            if (entry.status === 'delivered') continue;
            if (isDelivery) {
                this.deliver(entry, messageId);
            } else if (isFailure && messageId === entry.activeKey) {
                const code = failureCode(update);
                await this.fail(entry, failureReason(update), code);
            }
        }
    }

    onReceipts(updates = []) {
        for (const item of updates) {
            const entry = this.byKey.get(keyId(item?.key));
            if (!entry || entry.status === 'delivered') continue;
            const receipt = item?.receipt || {};
            if (receipt.messageTimestamp || receipt.receiptTimestamp
                    || receipt.readTimestamp || receipt.playedTimestamp) {
                this.deliver(entry, keyId(item.key));
            }
        }
    }

    // handleBadAck is called from the pino logger hook when Baileys logs
    // "received error in ack". This bypasses the EventBuffer entirely, so it
    // catches ACK 463 even when the corresponding messages.update event is
    // lost due to socket churn or buffer timing.
    async handleBadAck(messageId, errorCode) {
        console.log('[whatsapp-bridge] handleBadAck called messageId=%s errorCode=%s',
            messageId, errorCode);
        this.prune();
        if (!messageId) return;
        const entry = this.byKey.get(messageId);
        if (!entry) {
            // Buffer the NACK for replay when the key becomes known (same
            // path that onMessageUpdates uses for early updates).
            if (this.pendingUpdates.size >= this.maxPendingUpdates
                    && !this.pendingUpdates.has(messageId)) {
                this.pendingUpdates.delete(this.pendingUpdates.keys().next().value);
            }
            const buffered = this.pendingUpdates.get(messageId)
                || { createdAt: Date.now(), updates: [] };
            if (buffered.updates.length < 4) {
                buffered.updates.push({
                    key: { id: messageId },
                    update: {
                        status: FAILED_STATUS,
                        messageStubParameters: [String(errorCode)],
                    },
                });
            }
            this.pendingUpdates.set(messageId, buffered);
            return;
        }
        if (entry.status === 'delivered' || entry.status === 'failed') return;
        const code = String(errorCode || '');
        const reason = code ? `Message rejected (${code})` : 'Message rejected';
        await this.fail(entry, reason, code);
    }

    async fail(entry, reason, code = '') {
        if (entry.status === 'delivered' || entry.status === 'failed'
                || entry.status === 'diagnosing_463') return;
        if (code === '463' && this.diagnoseFailure) {
            entry.status = 'diagnosing_463';
            try {
                const diagnostic = await this.diagnoseFailure(entry.jid);
                entry.status = 'failed';
                this.emitStatus(entry, 'failed', {
                    reason,
                    terminal: true,
                    jid: entry.jid,
                    ...diagnostic,
                });
                return;
            } catch (error) {
                entry.status = 'failed';
                this.emitStatus(entry, 'failed', {
                    reason: `${reason}; reach-out diagnostic failed: ${error?.message || error}`,
                    terminal: true,
                    jid: entry.jid,
                });
                return;
            }
        }
        entry.status = 'failed';
        this.emitStatus(entry, 'failed', {
            reason, terminal: true, jid: entry.jid });
    }

    deliver(entry, messageId) {
        entry.status = 'delivered';
        this.emitStatus(entry, 'delivered', {
            baileys_message_id: messageId, jid: entry.jid });
    }

    async onConnection(status) {
        this.connected = status === 'connected';
    }

    emitStatus(entry, status, extra = {}) {
        this.emit({
            event: 'outbound_status',
            correlation_id: entry.correlationId,
            status,
            jid: entry.jid,
            retry_count: 0,
            ...entry.metadata,
            ...extra,
        });
    }

    snapshot(entry) {
        return {
            correlation_id: entry.correlationId,
            status: entry.status,
            retry_count: 0,
            message_id: [...entry.keys].at(-1) || null,
        };
    }

    prune() {
        const now = Date.now();
        const cutoff = now - this.ttlMs;
        for (const [correlationId, entry] of this.byCorrelation) {
            if (entry.createdAt >= cutoff || !['delivered', 'failed'].includes(entry.status)) continue;
            this.byCorrelation.delete(correlationId);
            for (const id of entry.keys) this.byKey.delete(id);
        }
        const pendingCutoff = now - this.pendingUpdateTtlMs;
        for (const [messageId, pending] of this.pendingUpdates) {
            if (pending.createdAt < pendingCutoff) this.pendingUpdates.delete(messageId);
        }
    }
}

module.exports = { OutboundLifecycle };
