'use strict';

function normalizeRecipientJid(value) {
    const jid = String(value || '').trim();
    if (!jid) return '';
    if (!jid.includes('@')) return `${jid}@s.whatsapp.net`;

    const separator = jid.lastIndexOf('@');
    const user = jid.slice(0, separator);
    const server = jid.slice(separator + 1);
    // Device-qualified PN JIDs identify a participant device, not the chat.
    // Preserve LID and group namespaces because their identifiers are canonical.
    return server === 's.whatsapp.net'
        ? `${user.split(':', 1)[0]}@${server}`
        : jid;
}

module.exports = { normalizeRecipientJid };
