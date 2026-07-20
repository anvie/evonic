'use strict';

const MESSAGE_CONTAINERS = [
    'ephemeralMessage',
    'viewOnceMessage',
    'viewOnceMessageV2',
    'viewOnceMessageV2Extension',
    'documentWithCaptionMessage',
];

/** Recursively unwrap WhatsApp container messages to their actual content. */
function unwrapMessage(message) {
    let content = message;
    const seen = new Set();

    while (content && typeof content === 'object' && !seen.has(content)) {
        seen.add(content);
        const container = MESSAGE_CONTAINERS.find(
            (key) => content[key]?.message && typeof content[key].message === 'object'
        );
        if (!container) break;
        content = content[container].message;
    }

    return content;
}

/**
 * Extract agent-readable content and identifying metadata from a quoted message.
 * Text is kept separately from media captions so consumers can distinguish them.
 */
function extractQuotedMessage(message) {
    const content = unwrapMessage(message);
    if (!content || typeof content !== 'object') return null;

    if (typeof content.conversation === 'string') {
        return {
            type: 'text',
            text: content.conversation,
            caption: null,
            filename: null,
            mimetype: null,
        };
    }

    if (content.extendedTextMessage) {
        return {
            type: 'text',
            text: content.extendedTextMessage.text || '',
            caption: null,
            filename: null,
            mimetype: null,
        };
    }

    for (const [key, type] of [
        ['imageMessage', 'image'],
        ['videoMessage', 'video'],
        ['documentMessage', 'document'],
    ]) {
        const media = content[key];
        if (!media) continue;
        const caption = typeof media.caption === 'string' && media.caption
            ? media.caption
            : null;
        return {
            type,
            text: caption,
            caption,
            filename: media.fileName || null,
            mimetype: media.mimetype || null,
        };
    }

    return null;
}

module.exports = { extractQuotedMessage, unwrapMessage };
