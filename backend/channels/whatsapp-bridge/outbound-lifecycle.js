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
    constructor({ send, emit, maxRetries = 1, ttlMs = 60 * 60 * 1000 }) {
        this.send = send;
        this.emit = emit;
        this.maxRetries = maxRetries;
        this.ttlMs = ttlMs;
        this.connected = false;
        this.retryBlocked = false;
        this.byCorrelation = new Map();
        this.byKey = new Map();
        // A NACK can arrive before sock.sendMessage() resolves and exposes its key.
        // Hold those updates briefly, then replay them as soon as the key is known.
        this.pendingUpdates = new Map();
        this.pendingUpdateTtlMs = 30 * 1000;
        this.maxPendingUpdates = 1000;
    }

    async accept({ correlationId, jid, content, retryEligible = false, retryJid = null }) {
        this.prune();
        if (this.byCorrelation.has(correlationId)) {
            return this.snapshot(this.byCorrelation.get(correlationId));
        }
        const entry = {
            correlationId, jid, retryJid, content, retryEligible,
            retries: 0, status: 'sending', keys: new Set(), activeKey: null,
            createdAt: Date.now(), pendingRetry: false,
        };
        this.byCorrelation.set(correlationId, entry);
        await this.sendAttempt(entry);
        return this.snapshot(entry);
    }

    async sendAttempt(entry) {
        try {
            const targetJid = entry.retries > 0 && entry.retryJid ? entry.retryJid : entry.jid;
            const result = await this.send(targetJid, entry.content);
            const id = keyId(result?.key);
            if (!id) throw new Error('Baileys returned no message key');
            entry.keys.add(id);
            entry.activeKey = id;
            this.byKey.set(id, entry);
            entry.status = 'accepted';
            entry.pendingRetry = false;
            this.emitStatus(entry, 'accepted', { baileys_message_id: id, jid: targetJid });
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
                await this.fail(entry, failureReason(update), true, code === '463');
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

    async fail(entry, reason, asynchronous, retryable = false) {
        if (entry.status === 'delivered' || entry.status === 'failed') return;
        const canRetry = asynchronous && retryable && entry.retryEligible
            && entry.retries < this.maxRetries;
        if (canRetry) {
            entry.retries += 1;
            entry.status = 'retrying';
            entry.activeKey = null;
            this.emitStatus(entry, 'retrying', { reason, retry: entry.retries });
            if (!this.connected || this.retryBlocked) {
                entry.pendingRetry = true;
                return;
            }
            await this.sendAttempt(entry);
            return;
        }
        entry.status = 'failed';
        this.emitStatus(entry, 'failed', { reason, terminal: true });
    }

    deliver(entry, messageId) {
        entry.status = 'delivered';
        entry.pendingRetry = false;
        this.emitStatus(entry, 'delivered', { baileys_message_id: messageId });
    }

    async onConnection(status, { terminal = false } = {}) {
        this.connected = status === 'connected';
        this.retryBlocked = terminal && !this.connected;
        if (!this.connected) return;
        this.retryBlocked = false;
        const pending = [...this.byCorrelation.values()]
            .filter((entry) => entry.pendingRetry && entry.status !== 'delivered');
        for (const entry of pending) await this.sendAttempt(entry);
    }

    emitStatus(entry, status, extra = {}) {
        this.emit({
            event: 'outbound_status',
            correlation_id: entry.correlationId,
            status,
            jid: entry.jid,
            retry_count: entry.retries,
            ...extra,
        });
    }

    snapshot(entry) {
        return {
            correlation_id: entry.correlationId,
            status: entry.status,
            retry_count: entry.retries,
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
