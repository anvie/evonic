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

test('post-send NACK retries an eligible resolved-LID DM once', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    const accepted = await lifecycle.accept({
        correlationId: 'correlation-1',
        jid: '628111@s.whatsapp.net',
        content: { text: 'hello' },
        retryEligible: true,
    });

    assert.equal(accepted.status, 'accepted');
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1' },
        update: { status: 0, error: new Error('NACK 463') },
    }]);

    assert.equal(sent.length, 2);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'retrying', 'accepted']);
    assert.ok(events.every((event) => event.correlation_id === 'correlation-1'));

    await lifecycle.onMessageUpdates([{
        key: { id: 'key-2' },
        update: { status: 0, error: new Error('second NACK') },
    }]);
    assert.equal(sent.length, 2);
    assert.equal(events.at(-1).status, 'failed');
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
        content: { text: 'hello' },
        retryEligible: true,
    });
    await lifecycle.onConnection('disconnected', { terminal: true });
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1' },
        update: { status: 0, error: new Error('auth unavailable') },
    }]);

    assert.equal(sent.length, 1);
    assert.equal(events.at(-1).status, 'retrying');
    await lifecycle.onConnection('connected');
    assert.equal(sent.length, 2);
    assert.equal(events.at(-1).status, 'accepted');
});
