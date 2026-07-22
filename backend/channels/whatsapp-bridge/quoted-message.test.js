'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { extractQuotedMessage, unwrapMessage } = require('./quoted-message');

test('extracts quoted plain text', () => {
    assert.deepEqual(extractQuotedMessage({ conversation: 'hello' }), {
        type: 'text', text: 'hello', caption: null, filename: null, mimetype: null,
    });
});

test('extracts quoted image caption and MIME type', () => {
    assert.deepEqual(extractQuotedMessage({
        imageMessage: { caption: 'NU logo instruction', mimetype: 'image/png' },
    }), {
        type: 'image', text: 'NU logo instruction', caption: 'NU logo instruction',
        filename: null, mimetype: 'image/png',
    });
});

test('extracts quoted document caption, filename, and MIME type', () => {
    assert.deepEqual(extractQuotedMessage({
        documentMessage: {
            caption: '<html>complete instructions</html>',
            fileName: 'brief.html',
            mimetype: 'text/html',
        },
    }), {
        type: 'document', text: '<html>complete instructions</html>',
        caption: '<html>complete instructions</html>', filename: 'brief.html',
        mimetype: 'text/html',
    });
});

test('recursively unwraps ephemeral, view-once, and document-caption containers', () => {
    const nested = {
        ephemeralMessage: { message: {
            viewOnceMessageV2: { message: {
                documentWithCaptionMessage: { message: {
                    documentMessage: { caption: 'nested caption', fileName: 'nested.pdf' },
                } },
            } },
        } },
    };
    assert.deepEqual(unwrapMessage(nested), {
        documentMessage: { caption: 'nested caption', fileName: 'nested.pdf' },
    });
    assert.equal(extractQuotedMessage(nested).text, 'nested caption');
});

test('extracts quoted video captions', () => {
    assert.deepEqual(extractQuotedMessage({
        videoMessage: { caption: 'watch this', mimetype: 'video/mp4' },
    }), {
        type: 'video', text: 'watch this', caption: 'watch this',
        filename: null, mimetype: 'video/mp4',
    });
});

test('retains media identity when a quoted video has no caption', () => {
    assert.deepEqual(extractQuotedMessage({
        videoMessage: { mimetype: 'video/mp4' },
    }), {
        type: 'video', text: null, caption: null, filename: null, mimetype: 'video/mp4',
    });
});
