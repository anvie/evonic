'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { OutboundLifecycle } = require('./outbound-lifecycle');

function harness() {
    const sent = [];
    const events = [];
    const lifecycle = new OutboundLifecycle({
        send: async (jid, content) => {
            const key = { id: `key-${sent.length + 1}` };
            sent.push({ jid, content, key });
            return { key };
        },
        emit: (event) => events.push(event),
        maxRetries: 1,
    });
    return { lifecycle, sent, events };
}

test('post-send ACK 463 retries a resolved-LID DM once using its original LID', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    const accepted = await lifecycle.accept({
        correlationId: 'correlation-1',
        jid: '628111@s.whatsapp.net',
        retryJid: '131902740668594@lid',
        content: { text: 'hello' },
        retryEligible: true,
    });

    assert.equal(accepted.status, 'accepted');
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['463'] },
    }]);

    assert.deepEqual(sent.map((item) => item.jid), [
        '628111@s.whatsapp.net',
        '131902740668594@lid',
    ]);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'retrying', 'accepted']);
    assert.ok(events.every((event) => event.correlation_id === 'correlation-1'));

    await lifecycle.onMessageUpdates([{
        key: { id: 'key-2', fromMe: true },
        update: { status: 0, messageStubParameters: ['463'] },
    }]);
    assert.equal(sent.length, 2);
    assert.equal(events.at(-1).status, 'failed');
});

test('ACK 463 arriving before send resolves is replayed and retried to the LID', async () => {
    const sent = [];
    const events = [];
    let lifecycle;
    lifecycle = new OutboundLifecycle({
        send: async (jid, content) => {
            const key = { id: `early-key-${sent.length + 1}` };
            sent.push({ jid, content, key });
            if (sent.length === 1) {
                await lifecycle.onMessageUpdates([{
                    key: { id: key.id, fromMe: true },
                    update: { status: 0, messageStubParameters: ['463'] },
                }]);
            }
            return { key };
        },
        emit: (event) => events.push(event),
        maxRetries: 1,
    });
    await lifecycle.onConnection('connected');

    const result = await lifecycle.accept({
        correlationId: 'correlation-early',
        jid: '628111@s.whatsapp.net',
        retryJid: '131902740668594@lid',
        content: { text: 'hello' },
        retryEligible: true,
    });

    assert.equal(result.status, 'accepted');
    assert.equal(result.retry_count, 1);
    assert.deepEqual(sent.map((item) => item.jid), [
        '628111@s.whatsapp.net',
        '131902740668594@lid',
    ]);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'retrying', 'accepted']);
});

test('non-463 NACK is reported failed without retrying an eligible LID DM', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    await lifecycle.accept({
        correlationId: 'correlation-non-retryable',
        jid: '628111@s.whatsapp.net',
        retryJid: '131902740668594@lid',
        content: { text: 'hello' },
        retryEligible: true,
    });

    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['500'] },
    }]);

    assert.equal(sent.length, 1);
    assert.equal(events.at(-1).status, 'failed');
    assert.match(events.at(-1).reason, /500/);
});

test('delivery receipt confirms delivery and suppresses later duplicate retry', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    await lifecycle.accept({
        correlationId: 'correlation-2',
        jid: '628222@s.whatsapp.net',
        content: { text: 'hello' },
        retryEligible: true,
    });

    lifecycle.onReceipts([{
        key: { id: 'key-1' },
        receipt: { messageTimestamp: 123 },
    }]);
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1' },
        update: { status: 0, error: new Error('late NACK') },
    }]);

    assert.equal(sent.length, 1);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'delivered']);
});

test('normal group send reports failure without retrying', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    await lifecycle.accept({
        correlationId: 'correlation-group',
        jid: '120363000000000001@g.us',
        content: { text: 'hello group' },
        retryEligible: false,
    });
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1' },
        update: { status: 0, error: new Error('group NACK') },
    }]);

    assert.equal(sent.length, 1);
    assert.equal(events.at(-1).status, 'failed');
});

test('terminal disconnect blocks pending retry until connection is restored', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    await lifecycle.accept({
        correlationId: 'correlation-3',
        jid: '628333@s.whatsapp.net',
        retryJid: '131902740668594@lid',
        content: { text: 'hello' },
        retryEligible: true,
    });
    await lifecycle.onConnection('disconnected', { terminal: true });
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['463'] },
    }]);

    assert.equal(sent.length, 1);
    assert.equal(events.at(-1).status, 'retrying');
    await lifecycle.onConnection('connected');
    assert.equal(sent.length, 2);
    assert.equal(sent[1].jid, '131902740668594@lid');
    assert.equal(events.at(-1).status, 'accepted');
});
