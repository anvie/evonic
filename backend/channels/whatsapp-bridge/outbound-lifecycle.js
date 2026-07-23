'use strict';

const DELIVERY_STATUS = 3;
const FAILED_STATUS = 0;

function keyId(key) {
    return key?.id || '';
}

function failureReason(update) {
    const error = update?.error || update?.messageStubParameters?.[0];
    if (typeof error === 'string') return error;
    return error?.message || error?.output?.payload?.message || 'WhatsApp rejected the message';
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
    }

    async accept({ correlationId, jid, content, retryEligible = false }) {
        this.prune();
        if (this.byCorrelation.has(correlationId)) {
            return this.snapshot(this.byCorrelation.get(correlationId));
        }
        const entry = {
            correlationId, jid, content, retryEligible,
            retries: 0, status: 'sending', keys: new Set(), activeKey: null,
            createdAt: Date.now(), pendingRetry: false,
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
            entry.pendingRetry = false;
            this.emitStatus(entry, 'accepted', { baileys_message_id: id });
        } catch (error) {
            await this.fail(entry, error?.message || String(error), false);
        }
    }

    async onMessageUpdates(updates = []) {
        for (const item of updates) {
            const messageId = keyId(item?.key);
            const entry = this.byKey.get(messageId);
            if (!entry || entry.status === 'delivered') continue;
            const update = item?.update || {};
            if (update.status >= DELIVERY_STATUS) {
                this.deliver(entry, messageId);
            } else if ((update.status === FAILED_STATUS || update.error)
                       && messageId === entry.activeKey) {
                await this.fail(entry, failureReason(update), true);
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

    async fail(entry, reason, asynchronous) {
        if (entry.status === 'delivered' || entry.status === 'failed') return;
        const canRetry = asynchronous && entry.retryEligible && entry.retries < this.maxRetries;
        if (canRetry) {
            entry.retries += 1;
            entry.status = 'retrying';
            this.emitStatus(entry, 'retrying', { reason, retry: entry.retries });
            if (!this.connected || this.retryBlocked) {
                entry.pendingRetry = true;
                return;
            }
            await this.sendAttempt(entry);
            return;
        }
        entry.status = 'failed';
        this.emitStatus(entry, 'failed', { reason, terminal: !asynchronous || !entry.retryEligible });
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
        const cutoff = Date.now() - this.ttlMs;
        for (const [correlationId, entry] of this.byCorrelation) {
            if (entry.createdAt >= cutoff || !['delivered', 'failed'].includes(entry.status)) continue;
            this.byCorrelation.delete(correlationId);
            for (const id of entry.keys) this.byKey.delete(id);
        }
    }
}

module.exports = { OutboundLifecycle };
