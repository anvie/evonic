"""Forwarded-header trust."""


def _echo_remote_addr(environ, start_response):
    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [environ['REMOTE_ADDR'].encode()]


def _remote_addr_seen(peer, forwarded, trusted='127.0.0.1,::1'):
    from app import _TrustedProxyFix, _parse_trusted_proxies

    wsgi = _TrustedProxyFix(_echo_remote_addr, _parse_trusted_proxies(trusted))
    environ = {'REMOTE_ADDR': peer, 'HTTP_X_FORWARDED_FOR': forwarded}
    return b''.join(wsgi(environ, lambda *a: None)).decode()


def test_forwarded_for_ignored_from_untrusted_peer():
    assert _remote_addr_seen('203.0.113.9', '10.9.9.9') == '203.0.113.9'


def test_forwarded_for_honoured_from_trusted_proxy():
    assert _remote_addr_seen('127.0.0.1', '198.51.100.7') == '198.51.100.7'
    assert _remote_addr_seen('::ffff:127.0.0.1', '198.51.100.7') == '198.51.100.7'


def test_trusted_proxies_accepts_cidr_and_skips_garbage():
    assert _remote_addr_seen('172.18.0.5', '198.51.100.7', '172.18.0.0/16, nonsense') == '198.51.100.7'
