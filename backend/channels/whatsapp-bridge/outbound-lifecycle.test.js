'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { OutboundLifecycle } = require('./outbound-lifecycle');

function harness(options = {}) {
    const sent = [];
    const events = [];
    const lifecycle = new OutboundLifecycle({
        send: async (jid, content) => {
            const key = { id: `key-${sent.length + 1}` };
            sent.push({ jid, content, key });
            return { key };
        },
        emit: (event) => events.push(event),
        diagnoseFailure: options.diagnoseFailure,
    });
    return { lifecycle, sent, events };
}

test('post-send ACK 463 is terminal and reports reach-out diagnostics', async () => {
    const diagnostics = [];
    const { lifecycle, sent, events } = harness({
        diagnoseFailure: async (jid) => {
            diagnostics.push({ code: '463', jid });
            return {
                reachout_timelocked: true,
                reachout_enforcement_type: 'WEB_COMPANION_ONLY',
                reachout_enforcement_ends: '2026-07-25T00:00:00.000Z',
            };
        },
    });
    await lifecycle.onConnection('connected');
    const accepted = await lifecycle.accept({
        correlationId: 'correlation-1',
        jid: '131902740668594@lid',
        retryJid: '628111@s.whatsapp.net',
        content: { text: 'hello' },
        metadata: { session_id: 'session-1' },
        retryEligible: true,
    });

    assert.equal(accepted.status, 'accepted');
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['463'] },
    }]);

    assert.deepEqual(sent.map((item) => item.jid), ['131902740668594@lid']);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'failed']);
    assert.deepEqual(diagnostics, [{ code: '463', jid: '131902740668594@lid' }]);
    assert.equal(events.at(-1).retry_count, 0);
    assert.equal(events.at(-1).reachout_timelocked, true);
    assert.equal(events.at(-1).reachout_enforcement_type, 'WEB_COMPANION_ONLY');
    assert.equal(events.at(-1).reachout_enforcement_ends, '2026-07-25T00:00:00.000Z');
    assert.ok(events.every((event) => event.session_id === 'session-1'));
    assert.ok(events.every((event) => event.correlation_id === 'correlation-1'));
});

test('ACK 463 arriving before send resolves is replayed as terminal', async () => {
    const sent = [];
    const events = [];
    let lifecycle;
    lifecycle = new OutboundLifecycle({
        send: async (jid, content) => {
            const key = { id: `early-key-${sent.length + 1}` };
            sent.push({ jid, content, key });
            await lifecycle.onMessageUpdates([{
                key: { id: key.id, fromMe: true },
                update: { status: 0, messageStubParameters: ['463'] },
            }]);
            return { key };
        },
        emit: (event) => events.push(event),
        diagnoseFailure: async () => ({ reachout_timelocked: false }),
    });
    await lifecycle.onConnection('connected');

    const result = await lifecycle.accept({
        correlationId: 'correlation-early',
        jid: '628111@s.whatsapp.net',
        retryJid: '131902740668594@lid',
        content: { text: 'hello' },
        retryEligible: true,
    });

    assert.equal(result.status, 'failed');
    assert.equal(result.retry_count, 0);
    assert.deepEqual(sent.map((item) => item.jid), ['628111@s.whatsapp.net']);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'failed']);
});

test('pino ACK 463 is terminal and duplicate updates do not rerun diagnostics', async () => {
    let diagnostics = 0;
    const { lifecycle, sent, events } = harness({
        diagnoseFailure: async () => {
            diagnostics += 1;
            return { reachout_timelocked: true };
        },
    });
    await lifecycle.onConnection('connected');
    await lifecycle.accept({
        correlationId: 'correlation-pino',
        jid: '131902740668594@lid',
        retryJid: '628111@s.whatsapp.net',
        content: { text: 'hello' },
        retryEligible: true,
    });

    await lifecycle.handleBadAck('key-1', '463');
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['463'] },
    }]);

    assert.deepEqual(sent.map((item) => item.jid), ['131902740668594@lid']);
    assert.deepEqual(events.map((event) => event.status), ['accepted', 'failed']);
    assert.equal(diagnostics, 1);
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

test('ACK 463 remains terminal across a disconnect and reconnect', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    await lifecycle.accept({
        correlationId: 'correlation-3',
        jid: '628333@s.whatsapp.net',
        content: { text: 'hello' },
    });
    await lifecycle.onConnection('disconnected', { terminal: true });
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['463'] },
    }]);

    assert.equal(sent.length, 1);
    assert.equal(events.at(-1).status, 'failed');
    await lifecycle.onConnection('connected');
    assert.equal(sent.length, 1);
    assert.equal(events.at(-1).status, 'failed');
});

test('attachment metadata follows acceptance and asynchronous failure', async () => {
    const { lifecycle, sent, events } = harness();
    await lifecycle.onConnection('connected');
    const accepted = await lifecycle.accept({
        correlationId: 'correlation-attachment',
        jid: '628444@s.whatsapp.net',
        content: {
            document: Buffer.from('%PDF-1.7\n'),
            mimetype: 'application/pdf',
            fileName: 'report.pdf',
        },
        metadata: {
            session_id: 'session-attachment',
            outbound_kind: 'attachment',
            attachment_name: 'report.pdf',
            attachment_mime_type: 'application/pdf',
        },
    });

    assert.equal(accepted.status, 'accepted');
    assert.equal(accepted.message_id, 'key-1');
    assert.equal(sent[0].jid, '628444@s.whatsapp.net');
    assert.equal(events[0].outbound_kind, 'attachment');
    assert.equal(events[0].attachment_name, 'report.pdf');
    await lifecycle.onMessageUpdates([{
        key: { id: 'key-1', fromMe: true },
        update: { status: 0, messageStubParameters: ['500'] },
    }]);

    assert.deepEqual(events.map((event) => event.status), ['accepted', 'failed']);
    assert.ok(events.every((event) => event.correlation_id === 'correlation-attachment'));
    assert.ok(events.every((event) => event.session_id === 'session-attachment'));
    assert.ok(events.every((event) => event.outbound_kind === 'attachment'));
});
