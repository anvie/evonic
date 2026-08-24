'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { extractInboundLocation } = require('./inbound-location');


test('static location message yields coordinates and metadata', () => {
    const location = extractInboundLocation({
        locationMessage: {
            degreesLatitude: -6.175,
            degreesLongitude: 106.827,
            name: 'Monas',
            address: 'Jakarta Pusat',
            accuracyInMeters: 12,
        },
    });
    assert.deepEqual(location, {
        latitude: -6.175,
        longitude: 106.827,
        name: 'Monas',
        address: 'Jakarta Pusat',
        accuracy_in_meters: 12,
        is_live: false,
    });
});


test('live location message is flagged as live', () => {
    const location = extractInboundLocation({
        liveLocationMessage: {
            degreesLatitude: -7.5,
            degreesLongitude: 112.3,
        },
    });
    assert.equal(location.is_live, true);
    assert.equal(location.latitude, -7.5);
    assert.equal(location.longitude, 112.3);
    assert.equal(location.name, '');
    assert.equal(location.address, '');
    assert.equal(location.accuracy_in_meters, null);
});


test('non-location message returns null', () => {
    assert.equal(extractInboundLocation({ conversation: 'hello' }), null);
    assert.equal(extractInboundLocation({}), null);
    assert.equal(extractInboundLocation(null), null);
});
