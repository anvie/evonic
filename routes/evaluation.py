import json
import os
import queue
from flask import Blueprint, render_template, jsonify, request, abort

from evaluator.engine import evaluation_engine
from models.db import db
import config

evaluation_bp = Blueprint('evaluation', __name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@evaluation_bp.route('/evaluate/domains')
def evaluate_domains():
    """Domains management page"""
    return render_template('evaluate_domains.html')


@evaluation_bp.route('/evaluate/evaluators')
def evaluate_evaluators():
    """Evaluators management page"""
    return render_template('evaluate_evaluators.html')


@evaluation_bp.route('/evaluate/docs/two-pass')
def evaluate_two_pass_docs():
    """Serve two-pass evaluation documentation (markdown source)."""
    doc_path = os.path.join(_ROOT, 'docs', 'two-pass-evaluation.md')
    if not os.path.isfile(doc_path):
        abort(404)
    with open(doc_path, encoding='utf-8') as f:
        body = f.read()
    return render_template('evaluate_doc.html', title='Two-Pass Evaluation', body=body)


@evaluation_bp.route('/evaluate')
def evaluate():
    """LLM Evaluation runner page"""
    from evaluator.test_manager import test_manager
    status = evaluation_engine.get_status()

    # If engine is idle, fetch last completed run from DB so summary persists on refresh
    if status.get('status') == 'idle':
        runs = db.get_all_runs(limit=1)
        if runs:
            last_run = runs[0]
            run_id = last_run.get('run_id')
            test_results = db.get_test_results(run_id)
            stats = db.get_run_stats(run_id)
            overall_score = last_run.get('overall_score')
            if overall_score is not None:
                total_duration_ms = last_run.get('total_duration_ms', 0)
                total_tokens = last_run.get('total_tokens', 0)
                tok_per_sec = None
                if total_duration_ms and total_duration_ms > 0:
                    tok_per_sec = round((total_tokens / total_duration_ms) * 1000, 1)
                # Inject tok_per_sec into run_info so template can access it as status.run_info.tok_per_sec
                last_run['tok_per_sec'] = tok_per_sec
                status = {
                    "status": "completed",
                    "run_id": run_id,
                    "run_info": last_run,
                    "test_results": test_results,
                    "stats": stats,
                    "tok_per_sec": tok_per_sec,
                    "error_message": None
                }

    # Get enabled domains for the test matrix
    domains = [d for d in test_manager.list_domains() if d.get('enabled', True)]
    domain_ids = [d['id'] for d in domains]
    domain_names = {d['id']: d.get('name', d['id']) for d in domains}
    # Build per-domain enabled test counts for accurate progress display
    domain_test_counts = {}
    for d in domains:
        count = 0
        for lvl in range(1, 6):
            tests = test_manager.list_tests(d['id'], lvl)
            count += sum(1 for t in tests if t.get('enabled', True))
        domain_test_counts[d['id']] = count
    return render_template('evaluate.html', status=status, domains=domain_ids, domain_names=domain_names, domain_test_counts=domain_test_counts)


@evaluation_bp.route('/api/status')
def api_status():
    """Get evaluation status"""
    status = evaluation_engine.get_status()
    return jsonify(status)


@evaluation_bp.route('/api/start', methods=['POST'])
def api_start():
    """Start evaluation"""
    try:
        data = request.get_json()
        model_name = data.get('model_name', 'default')
        domains = data.get('domains', None)  # None means all domains

        run_id = evaluation_engine.start_evaluation(model_name, domains=domains)
        return jsonify({
            'success': True,
            'run_id': run_id,
            'message': 'Evaluation started'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400


@evaluation_bp.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop evaluation"""
    try:
        evaluation_engine.stop_evaluation()
        return jsonify({
            'success': True,
            'message': 'Evaluation stopped'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400


@evaluation_bp.route('/api/reset', methods=['POST'])
def api_reset():
    """Reset engine state to idle"""
    try:
        evaluation_engine.reset_state()
        return jsonify({
            'success': True,
            'message': 'State reset'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400


@evaluation_bp.route('/api/test_matrix')
def api_test_matrix():
    """Get test matrix for current run"""
    run_id = request.args.get('run_id', type=int)
    matrix = evaluation_engine.get_test_matrix(run_id)
    return jsonify(matrix)


@evaluation_bp.route('/api/config')
def api_config():
    """Get current configuration"""
    try:
        from models.db import db as _db
        _dm = _db.get_default_model()
        _llm_base_url = _dm.get('base_url') if _dm else None
        _llm_model = _dm.get('model_name') if _dm else None
    except Exception:
        _llm_base_url = None
        _llm_model = None
    return jsonify({
        'llm_base_url': _llm_base_url,
        'llm_model': _llm_model,
        'debug': config.DEBUG
    })


@evaluation_bp.route('/api/config/model')
def api_config_model():
    """Get ONLY the model name (safe for client-side)"""
    from backend.llm_client import llm_client
    actual_model = llm_client.get_actual_model_name()
    try:
        from models.db import db as _db
        _dm = _db.get_default_model()
        _config_model = _dm.get('model_name') if _dm else None
    except Exception:
        _config_model = None
    return jsonify({
        'model': actual_model,
        'config_model': _config_model
    })


@evaluation_bp.route('/api/replay-test', methods=['POST'])
def api_replay_test():
    """Replay a single test and replace its result in the DB"""
    try:
        data = request.get_json()
        test_id = data.get('test_id')
        run_id = data.get('run_id')

        if not test_id or not run_id:
            return jsonify({'success': False, 'error': 'test_id and run_id are required'}), 400

        if evaluation_engine.is_running:
            return jsonify({'success': False, 'error': 'Evaluation is currently running. Wait for it to complete.'}), 409

        # Load the test definition
        from evaluator.test_manager import test_manager
        test = test_manager.get_test(test_id)
        if not test:
            return jsonify({'success': False, 'error': f'Test not found: {test_id}'}), 404

        domain = test.get('domain_id')
        level = test.get('level')

        # Get model_name from the original run
        run_info = db.get_evaluation_run(run_id)
        if not run_info:
            return jsonify({'success': False, 'error': f'Run not found: {run_id}'}), 404
        model_name = run_info.get('model_name', 'default')

        # Delete the old result so the engine's save creates a fresh row
        db.delete_individual_test_result(run_id, test_id)

        # Run the test using the global engine (reuses global llm_client)
        result = evaluation_engine._run_single_configurable_test(test, domain, level, model_name, run_id)

        # Fetch the saved result to return the full object the modal expects
        results = db.get_individual_test_results(run_id, domain=domain, level=level)
        saved = next((r for r in results if r['test_id'] == test_id), None)

        if saved:
            # Parse JSON fields
            if saved.get('details') and isinstance(saved['details'], str):
                saved['details'] = json.loads(saved['details'])
            if saved.get('expected') and isinstance(saved['expected'], str):
                try:
                    saved['expected'] = json.loads(saved['expected'])
                except (json.JSONDecodeError, ValueError):
                    pass

        return jsonify({
            'success': True,
            'result': saved or {
                'test_id': test_id,
                'domain': domain,
                'level': level,
                'score': result.score,
                'status': result.status,
            }
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@evaluation_bp.route('/api/summary/<int:run_id>')
def api_summary(run_id):
    """Get summary for a specific run (for dynamic rendering after modal close)"""
    try:
        runs = db.get_all_runs(limit=1)
        for run in runs:
            if run.get('run_id') == run_id:
                run_info = run
                break
        else:
            return jsonify({'success': False, 'error': 'Run not found'}), 404

        test_results = db.get_test_results(run_id)
        stats = db.get_run_stats(run_id)

        total_tokens = run_info.get('total_tokens', 0)
        total_duration_ms = run_info.get('total_duration_ms', 0)
        tok_per_sec = None
        if total_duration_ms and total_duration_ms > 0:
            tok_per_sec = round((total_tokens / total_duration_ms) * 1000, 1)

        return jsonify({
            'success': True,
            'summary': run_info.get('summary'),
            'overall_score': run_info.get('overall_score'),
            'total_tokens': total_tokens,
            'tok_per_sec': tok_per_sec,
            'total_duration_ms': total_duration_ms,
            'test_results': test_results,
            'stats': stats,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@evaluation_bp.route('/api/log_poll')
def log_poll():
    """Poll for new log messages - returns batch of pending messages"""
    messages = []
    # Drain up to 100 messages per poll to avoid huge responses
    for _ in range(100):
        try:
            message = evaluation_engine.log_queue.get_nowait()
            messages.append(message)
            if message == "EVAL_COMPLETE":
                break
        except queue.Empty:
            break
    return jsonify({
        'messages': messages,
        'is_running': evaluation_engine.is_running
    })
