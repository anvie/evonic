from backend.channels.whatsapp import _media_download_failed_text


def test_failed_image_gets_placeholder():
    assert _media_download_failed_text({'media_download_failed': ['image']}) == '[Image download failed]'


def test_failed_audio_and_video_get_placeholders():
    assert _media_download_failed_text({'media_download_failed': ['audio']}) == '[Audio download failed]'
    assert _media_download_failed_text({'media_download_failed': ['video']}) == '[Video download failed]'


def test_document_failure_is_left_to_its_own_placeholder():
    assert _media_download_failed_text({'media_download_failed': ['document']}) == ''


def test_no_failure_gives_no_placeholder():
    assert _media_download_failed_text({}) == ''
    assert _media_download_failed_text({'media_download_failed': []}) == ''
