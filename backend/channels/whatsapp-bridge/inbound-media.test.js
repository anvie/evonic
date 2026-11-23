'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { downloadInboundMedia } = require('./inbound-media');

test('returns null when the message has no media of that kind', async () => {
    const result = await downloadInboundMedia({
        message: {}, content: { imageMessage: {} }, kind: 'audio',
        downloadMediaMessage: async () => assert.fail('must not download'),
    });
    assert.equal(result, null);
});

test('downloads audio and keeps its mimetype', async () => {
    const result = await downloadInboundMedia({
        message: { id: 1 },
        content: { audioMessage: { mimetype: 'audio/ogg; codecs=opus' } },
        kind: 'audio',
        downloadMediaMessage: async () => Buffer.from('voice'),
    });
    assert.deepEqual(result, {
        base64: Buffer.from('voice').toString('base64'),
        mimetype: 'audio/ogg; codecs=opus',
    });
});

test('falls back to a default mimetype', async () => {
    const result = await downloadInboundMedia({
        message: {}, content: { imageMessage: {} }, kind: 'image',
        downloadMediaMessage: async () => Buffer.from('img'),
    });
    assert.equal(result.mimetype, 'image/jpeg');
});

test('rejects declared-oversize media without downloading', async () => {
    await assert.rejects(downloadInboundMedia({
        message: {}, content: { videoMessage: { fileLength: '100' } }, kind: 'video',
        maxBytes: 10,
        downloadMediaMessage: async () => assert.fail('must not download'),
    }), RangeError);
});

test('rejects oversize downloaded media', async () => {
    await assert.rejects(downloadInboundMedia({
        message: {}, content: { videoMessage: {} }, kind: 'video', maxBytes: 3,
        downloadMediaMessage: async () => Buffer.from('toolong'),
    }), RangeError);
});

test('propagates download errors so the caller can flag the failure', async () => {
    await assert.rejects(downloadInboundMedia({
        message: {}, content: { imageMessage: {} }, kind: 'image',
        downloadMediaMessage: async () => { throw new Error('410 gone'); },
    }), /410 gone/);
});

test('rejects a non-buffer result', async () => {
    await assert.rejects(downloadInboundMedia({
        message: {}, content: { imageMessage: {} }, kind: 'image',
        downloadMediaMessage: async () => 'nope',
    }), TypeError);
});
