"""
Exa search tool implementation: one web search through the Exa API, with the text of each result.

The API key is the skill's ``EXA_API_KEY`` variable (Skills › Exa), else the ``EXA_API_KEY`` environment variable.
"""

import os

import requests

ENDPOINT = 'https://api.exa.ai/search'
CATEGORIES = ('company', 'research paper', 'news', 'pdf', 'personal site', 'financial report', 'people')
MAX_RESULTS = 10
DEFAULT_RESULTS = 5
DEFAULT_TEXT_CHARS = 1500
MAX_TEXT_CHARS = 4000
TIMEOUT_SECONDS = 30


def _api_key() -> str:
    try:
        from backend.skills_manager import skills_manager
        key = str((skills_manager.get_skill_config('exa-search') or {}).get('EXA_API_KEY') or '').strip()
    except Exception:
        key = ''
    return key or str(os.environ.get('EXA_API_KEY') or '').strip()


def _number(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _domains(value) -> list:
    if isinstance(value, str):
        value = value.split(',')
    return [str(d).strip() for d in (value or []) if str(d).strip()][:20] if isinstance(value, (list, tuple, str)) else []


def payload_for(args: dict) -> dict:
    """The request body for one search."""
    body = {'query': str(args.get('query') or '').strip(), 'type': 'auto',
            'numResults': _number(args.get('num_results'), DEFAULT_RESULTS, 1, MAX_RESULTS)}
    if args.get('text', True) not in (False, 'false', 'no', 0):
        body['contents'] = {'text': {'maxCharacters': _number(args.get('max_chars'), DEFAULT_TEXT_CHARS, 200, MAX_TEXT_CHARS)}}
    category = str(args.get('category') or '').strip().lower()
    if category in CATEGORIES:
        body['category'] = category
    for arg, key in (('include_domains', 'includeDomains'), ('exclude_domains', 'excludeDomains')):
        domains = _domains(args.get(arg))
        if domains:
            body[key] = domains
    for arg, key in (('start_published_date', 'startPublishedDate'), ('end_published_date', 'endPublishedDate')):
        if str(args.get(arg) or '').strip():
            body[key] = str(args[arg]).strip()
    return body


def to_result(row: dict) -> dict:
    out = {'title': ' '.join(str(row.get('title') or '').split())[:300], 'url': str(row.get('url') or '')[:500]}
    for key in ('published_date', 'author'):
        if row.get(key):
            out[key] = str(row[key])[:100]
    if row.get('text'):
        out['text'] = str(row['text']).strip()
    return out


def execute(agent: dict, args: dict) -> dict:
    """Search the web with Exa.

    Args:
        agent: Agent context dictionary
        args: query (required), num_results, category, include_domains, exclude_domains, start_published_date,
              end_published_date, text (include the text of each result, default true), max_chars

    Returns:
        dict: ``{'status': 'success', 'query', 'results': [{title, url, published_date?, author?, text?}]}`` or an error
    """
    args = args or {}
    if not str(args.get('query') or '').strip():
        return {'status': 'error', 'error': 'query is required: what to search for.'}
    key = _api_key()
    if not key:
        return {'status': 'error', 'error': "No Exa API key: set EXA_API_KEY on the 'exa-search' skill (Skills › Exa)."}
    body = payload_for(args)
    try:
        resp = requests.post(ENDPOINT, json=body, headers={'x-api-key': key, 'Content-Type': 'application/json'},
                             timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return {'status': 'error', 'error': f'Exa could not be reached ({type(exc).__name__}).'}
    if resp.status_code != 200:
        return {'status': 'error', 'error': f'Exa answered HTTP {resp.status_code}: {resp.text[:200]}'}
    try:
        rows = (resp.json() or {}).get('results') or []
    except ValueError:
        return {'status': 'error', 'error': 'Exa did not answer with JSON.'}
    return {'status': 'success', 'query': body['query'], 'results': [to_result(r) for r in rows if isinstance(r, dict)]}
