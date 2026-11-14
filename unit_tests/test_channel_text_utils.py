from backend.channels import discord, telegram, text_utils, whatsapp, whatsapp_dispatcher


def test_adapters_reuse_the_shared_helpers():
    assert discord._human_size is text_utils.human_size
    assert telegram._human_size is text_utils.human_size
    assert discord._sanitize_filename is text_utils.sanitize_filename
    assert telegram._sanitize_filename is text_utils.sanitize_filename
    assert whatsapp._split_message is text_utils.split_message
    assert whatsapp_dispatcher._split_message is text_utils.split_message


def test_human_size():
    assert text_utils.human_size(None) == '0B'
    assert text_utils.human_size(512) == '512B'
    assert text_utils.human_size(1536) == '1.5KB'
    assert text_utils.human_size(5 * 1024 ** 4) == '5120.0GB'


def test_sanitize_filename():
    assert text_utils.sanitize_filename('') == 'file'
    assert text_utils.sanitize_filename('a b/c.txt') == 'a_b_c.txt'
    assert len(text_utils.sanitize_filename('x' * 500)) == 120


def test_split_message_prefers_paragraph_breaks_and_respects_limit():
    assert text_utils.split_message('short') == ['short']
    text = 'aaaa\n\nbbbb cccc'
    chunks = text_utils.split_message(text, max_len=8)
    assert chunks[0] == 'aaaa' and all(len(c) <= 8 for c in chunks)
    assert ''.join(chunks).replace(' ', '') == 'aaaabbbbcccc'
