"""HTTP client for driving Evonic agents programmatically (benchmark harness).

Auth: mints a signed Flask session cookie ({'authenticated': True}) using the
app's SECRET_KEY from .env — same serializer Flask uses (itsdangerous,
'cookie-session' salt, sha1 HMAC). CSRF uses the double-submit pattern from
app.py:csrf_protect — any token works as long as cookie == header.
"""

import hashlib
import os
import secrets
import time

import requests
from flask.json.tag import TaggedJSONSerializer
from itsdangerous import URLSafeTimedSerializer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BASE_URL = os.environ.get('EVONIC_BASE_URL', 'http://localhost:8080')


def _load_secret_key(env_path=None):
    env_path = env_path or os.path.join(REPO_ROOT, '.env')
    # python-dotenv keeps the LAST occurrence of a duplicated key — match that.
    key = None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('SECRET_KEY='):
                key = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not key:
        raise RuntimeError(f"SECRET_KEY not found in {env_path}")
    return key


def mint_session_cookie(secret_key, payload=None):
    """Produce a Flask-compatible signed session cookie value."""
    s = URLSafeTimedSerializer(
        secret_key,
        salt='cookie-session',
        serializer=TaggedJSONSerializer(),
        signer_kwargs={'key_derivation': 'hmac', 'digest_method': hashlib.sha1},
    )
    return s.dumps(payload or {'authenticated': True})


class EvonicClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, request_timeout=900):
        self.base_url = base_url.rstrip('/')
        self.request_timeout = request_timeout
        self.http = requests.Session()
        cookie = mint_session_cookie(_load_secret_key())
        csrf = secrets.token_hex(32)
        self.http.cookies.set('session', cookie)
        self.http.cookies.set('csrf_token', csrf)
        self.http.headers['X-CSRF-Token'] = csrf

    def _request(self, method, path, max_retries=5, **kwargs):
        kwargs.setdefault('timeout', self.request_timeout)
        url = f"{self.base_url}{path}"
        delay = 5.0
        for attempt in range(max_retries):
            try:
                resp = self.http.request(method, url, **kwargs)
            except requests.exceptions.ConnectionError:
                if attempt >= max_retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get('Retry-After', delay))
                time.sleep(min(retry_after, 120))
                delay *= 2
                continue
            if resp.status_code >= 500 and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return resp
        return resp

    def get_agents(self):
        resp = self._request('GET', '/api/agents')
        resp.raise_for_status()
        return resp.json()

    def chat(self, agent_id, message, user_id):
        """Send one message; returns the parsed JSON response (synchronous)."""
        resp = self._request(
            'POST', f'/api/agents/{agent_id}/chat',
            json={'message': message, 'user_id': user_id},
        )
        resp.raise_for_status()
        return resp.json()

    def poll(self, agent_id, user_id, after_id=0):
        resp = self._request(
            'GET', f'/api/agents/{agent_id}/chat/poll',
            params={'user_id': user_id, 'after': after_id},
        )
        resp.raise_for_status()
        return resp.json().get('messages', [])

    def last_message_id(self, agent_id, user_id):
        msgs = self.poll(agent_id, user_id, after_id=0)
        return max((m['id'] for m in msgs), default=0)

    def chat_and_wait(self, agent_id, message, user_id,
                      poll_interval=2.0, timeout=2700, settle=3.0):
        """Send one message and block until the assistant's reply arrives.

        The chat endpoint buffers messages and processes them asynchronously,
        so we poll /chat/poll for a new final assistant message. Returns
        {'response': str, 'wall_s': float, 'assistant_msgs': [...]}.
        """
        last_id = self.last_message_id(agent_id, user_id)
        t0 = time.time()
        self.chat(agent_id, message, user_id)
        replies = []
        while time.time() - t0 < timeout:
            time.sleep(poll_interval)
            new = self.poll(agent_id, user_id, after_id=last_id)
            assistant = [m for m in new if m['role'] == 'assistant']
            if assistant:
                # absorb stragglers before returning
                time.sleep(settle)
                new = self.poll(agent_id, user_id, after_id=last_id)
                assistant = [m for m in new if m['role'] == 'assistant']
                return {
                    'response': '\n'.join(m['content'] for m in assistant),
                    'wall_s': time.time() - t0,
                    'assistant_msgs': assistant,
                    'last_id': max(m['id'] for m in new),
                }
        raise TimeoutError(
            f"No assistant reply from {agent_id} within {timeout}s (user_id={user_id})")

    def get_history(self, agent_id, session_id, limit=200):
        resp = self._request(
            'GET', f'/api/agents/{agent_id}/chat',
            params={'session_id': session_id, 'limit': limit},
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == '__main__':
    client = EvonicClient()
    agents = client.get_agents()
    names = [a.get('id') for a in (agents if isinstance(agents, list) else agents.get('agents', []))]
    print(f"auth OK — {len(names)} agents: {sorted(str(n) for n in names)[:10]}...")
