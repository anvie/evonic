'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { normalizeRecipientJid } = require('./jid');

test('normalizes a device-qualified phone JID to its account JID', () => {
    assert.equal(
        normalizeRecipientJid('628111:0@s.whatsapp.net'),
        '628111@s.whatsapp.net',
    );
});

test('preserves canonical LID and group JIDs', () => {
    assert.equal(normalizeRecipientJid('131902740668594@lid'), '131902740668594@lid');
    assert.equal(normalizeRecipientJid('120363000000000001@g.us'), '120363000000000001@g.us');
});

test('adds the phone namespace to bare recipient digits', () => {
    assert.equal(normalizeRecipientJid('628111'), '628111@s.whatsapp.net');
});
