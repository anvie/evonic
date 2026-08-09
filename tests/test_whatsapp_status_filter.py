from backend.channels.whatsapp import _is_non_conversational_broadcast, _is_status_broadcast


def test_status_sender_is_broadcast():
    assert _is_status_broadcast("status", "status@broadcast")


def test_status_broadcast_jid_is_filtered():
    assert _is_status_broadcast("", "status@broadcast")


def test_status_namespace_sender_is_filtered():
    assert _is_status_broadcast("status@broadcast", "")


def test_direct_message_is_not_broadcast():
    assert not _is_status_broadcast("628123456789", "628123456789@s.whatsapp.net")


def test_newsletter_jid_is_dropped_as_non_conversational_broadcast():
    assert _is_non_conversational_broadcast(
        "120363218467385331", "120363218467385331@newsletter")
