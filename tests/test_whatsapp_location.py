from backend.channels.whatsapp import (
    _format_location_text,
    _normalize_location_payload,
)


def test_normalize_static_location():
    result = _normalize_location_payload({
        'latitude': -6.2088,
        'longitude': 106.8456,
        'name': 'Monas',
        'address': 'Jakarta Pusat',
        'accuracy_in_meters': 12,
        'is_live': False,
    })
    assert result == {
        'latitude': -6.2088,
        'longitude': 106.8456,
        'name': 'Monas',
        'address': 'Jakarta Pusat',
        'accuracy_in_meters': 12,
        'is_live': False,
    }


def test_normalize_coerces_numeric_strings():
    result = _normalize_location_payload({'latitude': '-6.2', 'longitude': '106.8'})
    assert result['latitude'] == -6.2
    assert result['longitude'] == 106.8


def test_normalize_rejects_missing_coordinates():
    assert _normalize_location_payload({'name': 'no coords'}) is None


def test_normalize_rejects_out_of_range_coordinates():
    assert _normalize_location_payload({'latitude': 91.0, 'longitude': 106.8}) is None
    assert _normalize_location_payload({'latitude': -6.2, 'longitude': -181.0}) is None


def test_normalize_rejects_non_dict_payload():
    assert _normalize_location_payload(None) is None
    assert _normalize_location_payload('location') is None


def test_format_location_text_with_label():
    text = _format_location_text({
        'latitude': -6.2088,
        'longitude': 106.8456,
        'name': 'Monas',
        'address': '',
        'is_live': False,
    })
    assert text == (
        '[Location shared] Monas\n'
        'latitude=-6.2088, longitude=106.8456\n'
        'https://www.google.com/maps?q=-6.2088,106.8456'
    )


def test_format_location_text_falls_back_to_address():
    text = _format_location_text({
        'latitude': 1.5,
        'longitude': 2.5,
        'name': '',
        'address': 'Jl. Contoh 1',
        'is_live': False,
    })
    assert text.startswith('[Location shared] Jl. Contoh 1')


def test_format_live_location_text_without_label():
    text = _format_location_text({
        'latitude': 1.5,
        'longitude': 2.5,
        'name': '',
        'address': '',
        'is_live': True,
    })
    assert text.startswith('[Live location shared]\n')
    assert 'https://www.google.com/maps?q=1.5,2.5' in text
