'use strict';

/** Return the text content carried by a supported inbound WhatsApp message. */
function extractInboundText(content) {
    return content?.conversation
        || content?.extendedTextMessage?.text
        || content?.imageMessage?.caption
        || content?.videoMessage?.caption
        || content?.documentMessage?.caption
        || '';
}

/**
 * Download and normalize an inbound document for the bridge callback.
 *
 * The raw media URL/key are intentionally not forwarded. Baileys owns media
 * decryption, and the Python channel receives only bytes plus bounded metadata.
 */
async function downloadInboundDocument({ message, content, downloadMediaMessage, logger }) {
    const documentMessage = content?.documentMessage;
    if (!documentMessage) return null;

    const buffer = await downloadMediaMessage(message, 'buffer', {}, { logger });
    if (!Buffer.isBuffer(buffer)) {
        throw new TypeError('Baileys document download did not return a buffer');
    }

    return {
        base64: buffer.toString('base64'),
        mimetype: documentMessage.mimetype || 'application/octet-stream',
        filename: documentMessage.fileName || 'document',
        file_length: buffer.length,
    };
}

module.exports = { downloadInboundDocument, extractInboundText };
