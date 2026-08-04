'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { downloadInboundDocument, extractInboundText } = require('./inbound-document');


test('document caption participates in inbound text extraction', () => {
    assert.equal(extractInboundText({
        documentMessage: { caption: 'Please review this PDF' },
    }), 'Please review this PDF');
});


test('document download preserves bytes and bounded callback metadata', async () => {
    const bytes = Buffer.from('%PDF-1.7\nexample');
    const calls = [];
    const document = await downloadInboundDocument({
        message: { key: { id: 'message-1' } },
        content: {
            documentMessage: {
                mimetype: 'application/pdf',
                fileName: 'meeting-notes.pdf',
            },
        },
        downloadMediaMessage: async (...args) => {
            calls.push(args);
            return bytes;
        },
        logger: { level: 'warn' },
    });

    assert.equal(calls.length, 1);
    assert.equal(calls[0][1], 'buffer');
    assert.equal(document.base64, bytes.toString('base64'));
    assert.equal(document.mimetype, 'application/pdf');
    assert.equal(document.filename, 'meeting-notes.pdf');
    assert.equal(document.file_length, bytes.length);
});


test('document download uses stable metadata defaults', async () => {
    const document = await downloadInboundDocument({
        message: {},
        content: { documentMessage: {} },
        downloadMediaMessage: async () => Buffer.from('document'),
        logger: {},
    });

    assert.equal(document.mimetype, 'application/octet-stream');
    assert.equal(document.filename, 'document');
});


test('document download rejects non-buffer media results', async () => {
    await assert.rejects(
        downloadInboundDocument({
            message: {},
            content: { documentMessage: {} },
            downloadMediaMessage: async () => 'not-a-buffer',
            logger: {},
        }),
        /did not return a buffer/
    );
});


test('non-document messages do not invoke media download', async () => {
    let called = false;
    const result = await downloadInboundDocument({
        message: {},
        content: { conversation: 'hello' },
        downloadMediaMessage: async () => {
            called = true;
            return Buffer.from('unexpected');
        },
        logger: {},
    });

    assert.equal(result, null);
    assert.equal(called, false);
});
