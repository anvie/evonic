"""Absolute point scoring.

A test is worth `level * weight` points and earns `score * max_points`. A run's
total is the plain sum of what its own tests earned, so adding a test to the
test set never changes the total of a run that has already been done.
"""
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional


def max_points(level: int, weight: Optional[float] = 1.0) -> float:
    w = weight if weight and weight > 0 else 1.0
    return float(level) * float(w)


def earned_points(score: Optional[float], level: int, weight: Optional[float] = 1.0) -> float:
    return float(score or 0.0) * max_points(level, weight)


def definition_hash(test: Dict[str, Any]) -> str:
    """Fingerprint of everything that decides how a test is asked and judged."""
    fields = {
        'prompt': test.get('prompt'),
        'expected': test.get('expected'),
        'evaluator_id': test.get('evaluator_id'),
        'level': test.get('level'),
        'weight': test.get('weight', 1.0),
        'system_prompt': test.get('system_prompt'),
        'system_prompt_mode': test.get('system_prompt_mode'),
        'tools': test.get('tools'),
        'tool_ids': test.get('tool_ids'),
    }
    blob = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def run_set_hash(rows: Iterable[Dict[str, Any]]) -> str:
    """Fingerprint of the set of tests a run executed.

    Rows without a definition hash (runs from before points existed) give a
    `legacy:` hash that only looks at the test ids.
    """
    rows = list(rows)
    legacy = any(not r.get('definition_hash') for r in rows)
    parts = sorted(
        r['test_id'] if legacy else f"{r['test_id']}:{r['definition_hash']}"
        for r in rows
    )
    digest = hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()[:16]
    return f'legacy:{digest}' if legacy else digest


def calculate_points(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum points over individual_test_results rows.

    Rows with `max_points`/`points` use them; otherwise they are derived from
    `level`, `weight` and `score`. Skipped tests are ignored.
    """
    total = 0.0
    maximum = 0.0
    passed = 0
    count = 0
    by_domain: Dict[str, Dict[str, float]] = {}
    by_level: Dict[str, Dict[str, float]] = {}

    for r in rows:
        if r.get('status') == 'skipped':
            continue
        mp = r.get('max_points')
        if mp is None:
            mp = max_points(r.get('level') or 1, r.get('weight'))
        pts = r.get('points')
        if pts is None:
            pts = float(r.get('score') or 0.0) * mp

        total += pts
        maximum += mp
        count += 1
        is_pass = r.get('status') == 'passed'
        passed += is_pass

        for bucket, key in ((by_domain, r.get('domain')),
                            (by_level, f"{r.get('domain')}:{r.get('level')}")):
            b = bucket.setdefault(key, {'points': 0.0, 'max_points': 0.0, 'passed': 0, 'count': 0})
            b['points'] += pts
            b['max_points'] += mp
            b['passed'] += is_pass
            b['count'] += 1

    return {
        'total': total,
        'max_points': maximum,
        'passed': passed,
        'count': count,
        'by_domain': by_domain,
        'by_level': by_level,
    }


def compare_runs(rows_a: Iterable[Dict[str, Any]], rows_b: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare two runs on the tests they share.

    A test is `common` when both runs ran it with the same definition. If either
    run predates definition hashes it is still compared, but counted under
    `unverified` because its definition may have changed. A test whose hashes
    differ is `changed` and left out of the points.
    """
    a = {r['test_id']: r for r in rows_a if r.get('status') != 'skipped'}
    b = {r['test_id']: r for r in rows_b if r.get('status') != 'skipped'}

    common: List[Dict[str, Any]] = []
    changed: List[str] = []
    unverified = 0
    for test_id in sorted(a.keys() & b.keys()):
        ha, hb = a[test_id].get('definition_hash'), b[test_id].get('definition_hash')
        if ha and hb and ha != hb:
            changed.append(test_id)
            continue
        if not (ha and hb):
            unverified += 1
        common.append({'test_id': test_id, 'a': a[test_id], 'b': b[test_id]})

    def side(key):
        return calculate_points([c[key] for c in common])

    pa, pb = side('a'), side('b')
    flipped = [
        {'test_id': c['test_id'], 'domain': c['a'].get('domain'), 'level': c['a'].get('level'),
         'a_status': c['a'].get('status'), 'b_status': c['b'].get('status')}
        for c in common
        if (c['a'].get('status') == 'passed') != (c['b'].get('status') == 'passed')
    ]
    return {
        'common': {'count': len(common), 'unverified': unverified, 'a': pa, 'b': pb,
                   'diff': pb['total'] - pa['total']},
        'changed': changed,
        'only_a': sorted(a.keys() - b.keys()),
        'only_b': sorted(b.keys() - a.keys()),
        'flipped': flipped,
    }


def attach_level_points(matrix: Dict[str, Dict[int, Dict[str, Any]]], rows: Iterable[Dict[str, Any]]) -> None:
    """Add points / max_points / passed / count to every matrix cell that has results."""
    by_level = calculate_points(rows)['by_level']
    for domain, levels in matrix.items():
        for level, cell in levels.items():
            b = by_level.get(f"{domain}:{level}")
            cell['points'] = b['points'] if b else None
            cell['max_points'] = b['max_points'] if b else None
            cell['passed'] = b['passed'] if b else None
            cell['count'] = b['count'] if b else None
