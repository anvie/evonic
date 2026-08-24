'use strict';

/**
 * Extract location payload from an inbound WhatsApp message.
 *
 * WhatsApp carries static shares as `locationMessage` and live shares as
 * `liveLocationMessage`. Both expose degreesLatitude/degreesLongitude plus
 * optional name/address metadata. Returns null when the message is not a
 * location share.
 */
function extractInboundLocation(content) {
    const msg = content?.locationMessage || content?.liveLocationMessage;
    if (!msg) return null;
    return {
        latitude: msg.degreesLatitude ?? null,
        longitude: msg.degreesLongitude ?? null,
        name: msg.name || '',
        address: msg.address || '',
        accuracy_in_meters: msg.accuracyInMeters ?? null,
        is_live: Boolean(content?.liveLocationMessage),
    };
}

module.exports = { extractInboundLocation };
