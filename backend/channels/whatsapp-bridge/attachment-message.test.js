'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
    attachmentTransportReady, buildDocumentMessage, submitAttachment,
} = require('./attachment-message');

test('document payload preserves bytes and attachment metadata', () => {
    const fileBuffer = Buffer.from('%PDF-1.7\n');

    const content = buildDocumentMessage({
        fileBuffer,
        filePath: '/staging/report.pdf',
        caption: 'Quarterly report',
        mimeType: 'application/pdf',
    });

    assert.equal(content.document, fileBuffer);
    assert.equal(content.mimetype, 'application/pdf');
    assert.equal(content.fileName, 'report.pdf');
    assert.equal(content.caption, 'Quarterly report');
});

test('document payload uses stable defaults for optional metadata', () => {
    const content = buildDocumentMessage({
        fileBuffer: Buffer.from('data'),
        filePath: '/staging/archive.bin',
    });

    assert.equal(content.mimetype, 'application/octet-stream');
    assert.equal(content.fileName, 'archive.bin');
    assert.equal(content.caption, undefined);
});

test('attachment transport requires a connected, send-ready socket', () => {
    assert.equal(attachmentTransportReady({
        hasSocket: true, connectionStatus: 'connected', messageSendReady: true,
    }), true);
    assert.equal(attachmentTransportReady({
        hasSocket: true, connectionStatus: 'connected', messageSendReady: false,
    }), false);
    assert.equal(attachmentTransportReady({
        hasSocket: false, connectionStatus: 'connected', messageSendReady: true,
    }), false);
    assert.equal(attachmentTransportReady({
        hasSocket: true, connectionStatus: 'disconnected', messageSendReady: true,
    }), false);
});

test('attachment submission registers lifecycle metadata and preserves recipient routing', async () => {
    const calls = [];
    const outboundLifecycle = {
        accept: async (request) => {
            calls.push(request);
            return {
                correlation_id: request.correlationId,
                status: 'accepted',
                message_id: 'baileys-1',
                retry_count: 0,
            };
        },
    };
    const fileBuffer = Buffer.from('image data');

    const result = await submitAttachment({
        lifecycle: outboundLifecycle,
        jid: '131902740668594@lid',
        fileBuffer,
        filePath: '/staging/photo.png',
        caption: 'Evidence',
        mimeType: 'image/png',
        correlationId: 'attachment-1',
        sessionId: 'session-1',
    });

    assert.equal(result.httpStatus, 200);
    assert.equal(result.body.success, true);
    assert.equal(result.body.status, 'accepted');
    assert.equal(result.body.correlation_id, 'attachment-1');
    assert.equal(result.body.message_id, 'baileys-1');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].correlationId, 'attachment-1');
    assert.equal(calls[0].jid, '131902740668594@lid');
    assert.equal(calls[0].content.document, fileBuffer);
    assert.equal(calls[0].content.mimetype, 'image/png');
    assert.equal(calls[0].content.fileName, 'photo.png');
    assert.deepEqual(calls[0].metadata, {
        outbound_kind: 'attachment',
        attachment_name: 'photo.png',
        attachment_mime_type: 'image/png',
        session_id: 'session-1',
    });
});
