"""Absolute point scoring: a run's total depends only on its own tests."""
import threading

from evaluator.points import calculate_points, definition_hash, max_points, run_set_hash
from models.db import db


def _row(test_id, level, score, status='passed', weight=1.0, dh='h'):
    return {'test_id': test_id, 'domain': 'math', 'level': level, 'score': score,
            'status': status, 'weight': weight, 'definition_hash': dh}


def test_max_points_is_level_times_weight():
    assert max_points(1) == 1
    assert max_points(5) == 5
    assert max_points(3, 2.0) == 6
    assert max_points(3, 0) == 3  # a non-positive weight falls back to 1


def test_calculate_points_sums_score_times_points():
    rows = [_row('a', 1, 1.0), _row('b', 4, 0.5, status='failed'), _row('c', 2, 1.0, weight=2.0)]
    totals = calculate_points(rows)
    assert totals['total'] == 1 + 2 + 4
    assert totals['max_points'] == 1 + 4 + 4
    assert totals['passed'] == 2 and totals['count'] == 3
    assert totals['by_domain']['math']['points'] == 7
    assert totals['by_level']['math:4']['max_points'] == 4


def test_skipped_tests_are_ignored():
    totals = calculate_points([_row('a', 3, 0.0, status='skipped'), _row('b', 1, 1.0)])
    assert totals['count'] == 1 and totals['max_points'] == 1


def test_adding_a_test_does_not_change_an_old_total():
    old = [_row('a', 1, 1.0), _row('b', 2, 0.5)]
    before = calculate_points(old)['total']
    calculate_points(old + [_row('new', 5, 1.0)])  # a later run with more tests
    assert calculate_points(old)['total'] == before == 2


def test_definition_hash_is_stable_and_sensitive():
    t = {'prompt': 'p', 'expected': {'answer': 1}, 'evaluator_id': 'e', 'level': 1}
    assert definition_hash(t) == definition_hash(dict(t))
    assert definition_hash(t) != definition_hash({**t, 'prompt': 'q'})
    assert definition_hash(t) != definition_hash({**t, 'expected': {'answer': 2}})


def test_run_set_hash_ignores_order_and_flags_legacy():
    a, b = _row('a', 1, 1), _row('b', 1, 1)
    assert run_set_hash([a, b]) == run_set_hash([b, a])
    assert run_set_hash([a]) != run_set_hash([a, b])
    assert run_set_hash([a, {**b, 'definition_hash': None}]).startswith('legacy:')
    assert run_set_hash([_row('a', 1, 1, dh='x')]) != run_set_hash([_row('a', 1, 1, dh='y')])


def _fresh_db():
    db._tls = threading.local()
    db._init_tables()


def _save(run_id, test_id, level, score, status='passed', weight=1.0, dh='h'):
    db.save_individual_test_result(
        run_id=run_id, test_id=test_id, domain='math', level=level, prompt='p', response='r',
        expected=None, score=score, status=status, details=None, duration_ms=1,
        model_name='m', weight=weight, evaluator_id='e', definition_hash=dh)


def test_saved_result_carries_points_and_run_totals_refresh():
    _fresh_db()
    run_id = db.create_evaluation_run('m')
    _save(run_id, 'a', 1, 1.0)
    _save(run_id, 'b', 4, 0.5, status='failed', weight=2.0)
    totals = db.refresh_run_points(run_id)
    assert totals['total'] == 1 + 4 and totals['max_points'] == 1 + 8
    run = db.get_evaluation_run(run_id)
    assert (run['total_points'], run['max_points'], run['tests_run']) == (5, 9, 2)
    assert run['test_set_hash'] and not run['test_set_hash'].startswith('legacy:')


def test_level_scores_upsert_leaves_one_row():
    _fresh_db()
    run_id = db.create_evaluation_run('m')
    db.save_level_score(run_id, 'math', 1, 0.5, 2, 1)
    db.save_level_score(run_id, 'math', 1, 1.0, 2, 2)  # what resume does
    rows = db.get_level_scores(run_id)
    assert len(rows) == 1 and rows[0]['average_score'] == 1.0


def test_backfill_gives_old_runs_points_and_is_idempotent():
    _fresh_db()
    run_id = db.create_evaluation_run('m')
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO individual_test_results (run_id, test_id, domain, level, score, status) "
            "VALUES (?, 'old1', 'math', 3, 0.5, 'failed')", (run_id,))
        conn.commit()
    _fresh_db()
    _fresh_db()
    run = db.get_evaluation_run(run_id)
    assert (run['total_points'], run['max_points'], run['tests_run']) == (1.5, 3, 1)
    assert run['test_set_hash'].startswith('legacy:')


def test_compare_runs_groups_tests():
    from evaluator.points import compare_runs
    run_a = [_row('same', 2, 1.0, dh='x'), _row('edit', 1, 1.0, dh='old'),
             _row('flip', 3, 1.0, dh='f'), _row('gone', 1, 1.0, dh='g')]
    run_b = [_row('same', 2, 0.5, status='failed', dh='x'), _row('edit', 1, 0.0, status='failed', dh='new'),
             _row('flip', 3, 0.0, status='failed', dh='f'), _row('added', 5, 1.0, dh='n')]
    out = compare_runs(run_a, run_b)
    assert out['changed'] == ['edit']
    assert out['only_a'] == ['gone'] and out['only_b'] == ['added']
    assert out['common']['count'] == 2  # same + flip, the new test never enters
    assert out['common']['a']['total'] == 2 + 3
    assert out['common']['b']['total'] == 1 + 0
    assert out['common']['diff'] == -4
    assert {f['test_id'] for f in out['flipped']} == {'same', 'flip'}


def test_compare_runs_marks_legacy_hashes_unverified():
    from evaluator.points import compare_runs
    out = compare_runs([_row('t', 1, 1.0, dh=None)], [_row('t', 1, 1.0, dh='h')])
    assert out['common']['count'] == 1 and out['common']['unverified'] == 1


def test_compare_api_joins_two_runs():
    from app import app
    _fresh_db()
    a, b = db.create_evaluation_run('m1'), db.create_evaluation_run('m2')
    _save(a, 't1', 2, 1.0, dh='x')
    _save(b, 't1', 2, 0.5, status='failed', dh='x')
    _save(b, 't2', 1, 1.0, dh='y')
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        resp = client.get(f'/api/history/compare?a={a}&b={b}')
        body = resp.get_json()
        assert resp.status_code == 200 and body['success']
        assert body['common']['count'] == 1 and body['common']['diff'] == -1
        assert body['only_b'] == ['t2']
        assert client.get('/api/history/compare?a=1').status_code == 400
        assert client.get(f'/api/history/compare?a={a}&b=999999').status_code == 404


def test_leaderboard_ranks_points_within_one_test_set():
    _fresh_db()

    def finished_run(model, test_ids, hash_tag, score):
        run_id = db.create_evaluation_run(model)
        for t in test_ids:
            _save(run_id, t, 2, score, dh=f'{t}-{hash_tag}')
        db.complete_evaluation_run(run_id, 's', score)
        db.refresh_run_points(run_id)
        return run_id

    finished_run('old-model', ['a'], 'v1', 1.0)                 # 2 pts on the old set
    finished_run('model-x', ['a', 'b'], 'v1', 1.0)              # 4 pts on the newer set
    finished_run('model-y', ['a', 'b'], 'v1', 0.5)              # 2 pts on the newer set
    board = db.get_model_leaderboard()
    assert [m['model_name'] for m in board] == ['model-x', 'model-y']
    assert board[0]['best_points'] == 4 and board[0]['max_points'] == 4
    old_hash = next(t['test_set_hash'] for t in db.list_test_sets() if t['tests_run'] == 1)
    assert [m['model_name'] for m in db.get_model_leaderboard(test_set_hash=old_hash)] == ['old-model']


def test_compare_page_and_history_pages_render_points_without_percent():
    from app import app
    _fresh_db()
    a, b = db.create_evaluation_run('m1'), db.create_evaluation_run('m2')
    _save(a, 't1', 2, 1.0, dh='x')
    _save(b, 't1', 2, 0.5, status='failed', dh='x')
    _save(b, 't2', 1, 1.0, dh='y')
    for run_id in (a, b):
        db.refresh_run_points(run_id)
        db.complete_evaluation_run(run_id, 'summary', 0.5)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        page = client.get(f'/history/compare?a={a}&b={b}')
        html = page.get_data(as_text=True)
        assert page.status_code == 200
        assert 'Compared on 1 shared test.' in html and '−1' in html and 'Only in run B (1)' in html
        assert client.get('/history/compare').status_code == 302
        assert client.get(f'/history/compare?a={a}&b=999999').status_code == 404

        listing = client.get('/history').get_data(as_text=True)
        import re
        badge = re.search(r'class="score-badge[^>]*>\s*([^<]+?)\s*<', listing).group(1)
        assert badge == '2'  # one absolute number: no max, no percent
        db.update_test_result(b, 'math', 2, score=0.5, status='failed')  # a level row makes the domain appear
        detail = client.get(f'/history/{b}').get_data(as_text=True)
        assert 'Total Points' in detail and 'Overall Score' not in detail
        assert 'mx-box' in detail and 'status-dot' not in detail


def test_current_test_set_prefers_a_real_fingerprint_over_a_newer_legacy_one():
    _fresh_db()
    real = db.create_evaluation_run('m')
    _save(real, 'a', 1, 1.0, dh='h')
    db.complete_evaluation_run(real, 's', 1.0)
    db.refresh_run_points(real)
    old = db.create_evaluation_run('m')
    with db._connect() as conn:
        conn.execute("INSERT INTO individual_test_results (run_id, test_id, domain, level, score, status) "
                     "VALUES (?, 'a', 'math', 1, 1.0, 'passed')", (old,))
        conn.commit()
    db.complete_evaluation_run(old, 's', 1.0)
    _fresh_db()  # backfill gives the newer run a legacy: fingerprint
    assert db.get_evaluation_run(old)['test_set_hash'].startswith('legacy:')
    assert db.current_test_set_hash() == db.get_evaluation_run(real)['test_set_hash']


def test_summary_api_carries_what_the_run_card_shows():
    from app import app
    _fresh_db()
    run_id = db.create_evaluation_run('card-model')
    _save(run_id, 'a', 2, 1.0, dh='x')
    _save(run_id, 'b', 1, 0.0, status='failed', dh='y')
    db.complete_evaluation_run(run_id, 'A short summary.', 0.5, 1000, 20000)
    db.refresh_run_points(run_id)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        body = client.get(f'/api/summary/{run_id}').get_json()
    assert body['success'] and body['run_id'] == run_id and body['model_name'] == 'card-model'
    assert body['total_points'] == 2 and body['summary'] == 'A short summary.'
    assert body['by_domain']['math'] == {'points': 2.0, 'max_points': 3.0, 'passed': 1, 'count': 2}
    assert body['completed_at']
    # heatmap input: one slim entry per test, ordered by domain then level
    assert body['tests'] == [
        {'id': 'b', 'd': 'math', 'l': 1, 's': 0.0, 'st': 'failed', 'e': False},
        {'id': 'a', 'd': 'math', 'l': 2, 's': 1.0, 'st': 'passed', 'e': False},
    ]
