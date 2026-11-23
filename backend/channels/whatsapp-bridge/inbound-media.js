'use strict';

const MEDIA_MESSAGE_KEYS = {
    image: 'imageMessage',
    audio: 'audioMessage',
    video: 'videoMessage',
};
const DEFAULT_MIMETYPES = {
    image: 'image/jpeg',
    audio: 'audio/ogg',
    video: 'video/mp4',
};
const MAX_MEDIA_BYTES = 20 * 1024 * 1024;

/**
 * Download an inbound image, audio or video and normalize it for the bridge callback.
 *
 * Returns null when the message carries no media of that kind. Throws when the
 * download fails or the media exceeds maxBytes, so the caller can report which
 * kind failed instead of silently dropping it.
 */
async function downloadInboundMedia({ message, content, kind, downloadMediaMessage, logger,
                                      maxBytes = MAX_MEDIA_BYTES }) {
    const media = content?.[MEDIA_MESSAGE_KEYS[kind]];
    if (!media) return null;

    // fileLength is sender-declared metadata: it only lets us skip a pointless download.
    if (Number(media.fileLength) > maxBytes) {
        throw new RangeError(`WhatsApp ${kind} exceeds ${maxBytes} bytes`);
    }
    const buffer = await downloadMediaMessage(message, 'buffer', {}, { logger });
    if (!Buffer.isBuffer(buffer)) {
        throw new TypeError(`Baileys ${kind} download did not return a buffer`);
    }
    if (buffer.length > maxBytes) {
        throw new RangeError(`WhatsApp ${kind} exceeds ${maxBytes} bytes`);
    }
    return {
        base64: buffer.toString('base64'),
        mimetype: media.mimetype || DEFAULT_MIMETYPES[kind],
    };
}

module.exports = { downloadInboundMedia, MEDIA_MESSAGE_KEYS };
