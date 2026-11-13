"""X-Device-IPs header parsing for Evonet connectors."""

from backend.workplaces.connector_relay import _MAX_DEVICE_IPS, _parse_device_ips


def test_parse_device_ips_keeps_valid_addresses_in_order():
    header = '203.0.113.10, 2001:db8:1::1'

    assert _parse_device_ips(header) == '203.0.113.10, 2001:db8:1::1'


def test_parse_device_ips_drops_junk_and_duplicates():
    header = '<script>, 10.0.0.2,10.0.0.2 , not-an-ip, , 2001:DB8::0001'

    assert _parse_device_ips(header) == '10.0.0.2, 2001:db8::1'


def test_parse_device_ips_caps_the_list():
    header = ', '.join(f'10.0.0.{i}' for i in range(1, 10))

    assert len(_parse_device_ips(header).split(', ')) == _MAX_DEVICE_IPS


def test_parse_device_ips_empty_or_missing_header():
    assert _parse_device_ips('') == ''
    assert _parse_device_ips(None) == ''
