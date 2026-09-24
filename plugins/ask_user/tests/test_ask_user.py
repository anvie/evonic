"""
Tests for the ask_user plugin: question/answer validation, the answer route,
and the blocking ask_user_question tool (with the runtime, realtime journal
and settings DB stubbed out).
"""

import queue
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from flask import Flask

from plugins.ask_user import routes
from plugins.ask_user.store import (
    QuestionRegistry, build_result, normalize_answers, normalize_questions,
    question_registry,
)


def _q(question='Which database?', header='Database', options=('PostgreSQL', 'SQLite'), multi=False):
    return {
        'question': question,
        'header': header,
        'multiSelect': multi,
        'options': [{'label': label, 'description': f'Use {label}'} for label in options],
    }


class TestNormalizeQuestions(unittest.TestCase):
    def test_valid_question_is_normalized(self):
        questions, error = normalize_questions([_q()])
        self.assertIsNone(error)
        self.assertEqual(questions[0]['header'], 'Database')
        self.assertFalse(questions[0]['multiSelect'])
        self.assertEqual([o['label'] for o in questions[0]['options']], ['PostgreSQL', 'SQLite'])

    def test_rejects_empty_and_too_many_questions(self):
        self.assertIsNotNone(normalize_questions([])[1])
        self.assertIsNotNone(normalize_questions(None)[1])
        five = [_q(question=f'Q{i}?') for i in range(5)]
        self.assertIn('at most 4', normalize_questions(five)[1])

    def test_rejects_bad_option_counts(self):
        self.assertIn('at least 2', normalize_questions([_q(options=('Only',))])[1])
        self.assertIn('maximum is 4', normalize_questions([_q(options=('a', 'b', 'c', 'd', 'e'))])[1])

    def test_model_supplied_other_option_is_dropped(self):
        questions, error = normalize_questions([_q(options=('A', 'B', 'Other'))])
        self.assertIsNone(error)
        self.assertEqual([o['label'] for o in questions[0]['options']], ['A', 'B'])

    def test_rejects_duplicate_labels_and_questions(self):
        self.assertIn('duplicate option', normalize_questions([_q(options=('A', 'A'))])[1])
        self.assertIn('duplicates', normalize_questions([_q(), _q()])[1])

    def test_string_options_and_missing_header_are_accepted(self):
        questions, error = normalize_questions([{'question': 'Pick?', 'options': ['x', 'y']}])
        self.assertIsNone(error)
        self.assertEqual(questions[0]['header'], 'Q1')
        self.assertEqual(questions[0]['options'][0], {'label': 'x', 'description': '', 'recommended': False})

    def test_recommended_flag_is_optional(self):
        questions, _ = normalize_questions([_q()])
        self.assertEqual([o['recommended'] for o in questions[0]['options']], [False, False])
        raw = _q()
        raw['options'][1]['recommended'] = True
        questions, _ = normalize_questions([raw])
        self.assertEqual([o['recommended'] for o in questions[0]['options']], [False, True])

    def test_recommended_label_suffix_becomes_flag(self):
        for label in ('SQLite (Recommended)', 'SQLite (rekomendasi)', 'SQLite [Recommended]'):
            questions, error = normalize_questions([_q(options=(label, 'PostgreSQL'))])
            self.assertIsNone(error)
            self.assertEqual(questions[0]['options'][0], {
                'label': 'SQLite', 'description': f'Use {label}', 'recommended': True})
        # Answers then use the clean label.
        answers, error = normalize_answers(questions, [{'selected': ['SQLite']}])
        self.assertIsNone(error)
        self.assertEqual(answers[0]['answer'], 'SQLite')

    def test_single_choice_keeps_only_first_recommendation(self):
        raw = _q(options=('A', 'B', 'C'))
        for opt in raw['options']:
            opt['recommended'] = True
        questions, _ = normalize_questions([raw])
        self.assertEqual([o['recommended'] for o in questions[0]['options']], [True, False, False])
        multi, _ = normalize_questions([{**raw, 'multiSelect': True}])
        self.assertEqual([o['recommended'] for o in multi[0]['options']], [True, True, True])


class TestNormalizeAnswers(unittest.TestCase):
    def setUp(self):
        self.questions, _ = normalize_questions([
            _q(),
            _q(question='Which features?', header='Features', options=('Auth', 'Search', 'Export'), multi=True),
        ])

    def test_single_and_multi_select(self):
        answers, error = normalize_answers(self.questions, [
            {'selected': ['SQLite']},
            {'selected': ['Export', 'Auth'], 'other': 'Billing'},
        ])
        self.assertIsNone(error)
        self.assertEqual(answers[0]['answer'], 'SQLite')
        # Option order is kept regardless of click order; "other" goes last.
        self.assertEqual(answers[1]['selected'], ['Auth', 'Export'])
        self.assertEqual(answers[1]['answer'], 'Auth, Export, Billing')

    def test_other_only_answer(self):
        answers, error = normalize_answers(self.questions, [
            {'selected': [], 'other': 'MySQL'}, {'selected': ['Auth']},
        ])
        self.assertIsNone(error)
        self.assertEqual(answers[0]['answer'], 'MySQL')

    def test_rejects_missing_or_multiple_single_answers(self):
        self.assertIsNotNone(normalize_answers(self.questions, [{'selected': []}, {'selected': ['Auth']}])[1])
        self.assertIsNotNone(normalize_answers(self.questions, [{'selected': ['SQLite'], 'other': 'x'},
                                                                {'selected': ['Auth']}])[1])
        self.assertIsNotNone(normalize_answers(self.questions, [{'selected': ['SQLite']}])[1])

    def test_unknown_labels_are_ignored(self):
        _, error = normalize_answers(self.questions, [{'selected': ['Oracle']}, {'selected': ['Auth']}])
        self.assertIsNotNone(error)


class TestRegistryAndRoutes(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(routes.create_blueprint())
        self.client = app.test_client()
        self.questions, _ = normalize_questions([_q()])
        self.pending = question_registry.create('sess-1', 'agent-1', self.questions)

    def tearDown(self):
        question_registry.remove(self.pending.question_id)

    def test_answer_wakes_waiter(self):
        res = self.client.post('/api/ask_user/answer', json={
            'question_id': self.pending.question_id, 'answers': [{'selected': ['SQLite']}],
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.pending.event.is_set())
        self.assertEqual(self.pending.answers[0]['answer'], 'SQLite')

        again = self.client.post('/api/ask_user/answer', json={
            'question_id': self.pending.question_id, 'answers': [{'selected': ['SQLite']}],
        })
        self.assertEqual(again.status_code, 400)

    def test_invalid_answer_keeps_question_open(self):
        res = self.client.post('/api/ask_user/answer', json={
            'question_id': self.pending.question_id, 'answers': [{'selected': []}],
        })
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.pending.event.is_set())

    def test_cancel_closes_question(self):
        res = self.client.post('/api/ask_user/cancel', json={'question_id': self.pending.question_id})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.pending.event.is_set())
        self.assertTrue(self.pending.dismissed)
        # Neither an answer nor a second cancel is accepted afterwards.
        answer = self.client.post('/api/ask_user/answer', json={
            'question_id': self.pending.question_id, 'answers': [{'selected': ['SQLite']}],
        })
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(self.client.post('/api/ask_user/cancel', json={
            'question_id': self.pending.question_id}).status_code, 400)
        self.assertEqual(self.client.get('/api/ask_user/pending?session_id=sess-1').get_json()['questions'], [])

    def test_cancel_unknown_question_is_404(self):
        res = self.client.post('/api/ask_user/cancel', json={'question_id': 'nope'})
        self.assertEqual(res.status_code, 404)

    def test_unknown_question_is_404(self):
        res = self.client.post('/api/ask_user/answer', json={'question_id': 'nope', 'answers': []})
        self.assertEqual(res.status_code, 404)

    def test_pending_lists_session_questions(self):
        res = self.client.get('/api/ask_user/pending?session_id=sess-1')
        ids = [q['question_id'] for q in res.get_json()['questions']]
        self.assertIn(self.pending.question_id, ids)
        other = self.client.get('/api/ask_user/pending?session_id=sess-2').get_json()['questions']
        self.assertEqual(other, [])

    def test_pending_reports_how_known_questions_ended(self):
        qid = self.pending.question_id
        url = f'/api/ask_user/pending?session_id=sess-1&known={qid},gone-after-restart'
        # Still open: nothing to report for it; an id the server never saw is expired.
        self.assertEqual(self.client.get(url).get_json()['resolved'], {'gone-after-restart': 'expired'})
        question_registry.dismiss(qid)
        question_registry.remove(qid, 'dismissed')
        body = self.client.get(url).get_json()
        self.assertEqual(body['questions'], [])
        self.assertEqual(body['resolved'][qid], 'dismissed')


class TestAssetInjection(unittest.TestCase):
    PAGE = '<html><body><div id="chat-input"></div></body></html>'

    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(routes.create_blueprint())
        app.add_url_rule('/agents/<agent_id>', 'agent', lambda agent_id: self.PAGE)
        app.add_url_rule('/plugins', 'plugins', lambda: self.PAGE)
        self.client = app.test_client()

    def test_agent_page_gets_plugin_ui(self):
        with patch.object(routes, '_plugin_enabled', return_value=True):
            html = self.client.get('/agents/evonic').get_data(as_text=True)
        self.assertIn('/ask_user/static/ask_user.js?v=', html)
        self.assertIn('/ask_user/static/ask_user.css?v=', html)
        self.assertLess(html.index('ask_user.js'), html.index('</body>'))

    def test_other_pages_and_disabled_plugin_are_untouched(self):
        with patch.object(routes, '_plugin_enabled', return_value=True):
            self.assertEqual(self.client.get('/plugins').get_data(as_text=True), self.PAGE)
        with patch.object(routes, '_plugin_enabled', return_value=False):
            self.assertEqual(self.client.get('/agents/evonic').get_data(as_text=True), self.PAGE)

    def test_static_assets_are_served(self):
        for name in ('ask_user.js', 'ask_user.css'):
            res = self.client.get(f'/ask_user/static/{name}')
            self.assertEqual(res.status_code, 200, name)
            res.close()


class _FakeRuntime:
    def __init__(self):
        self.stop = threading.Event()
        self.queues = {}

    def is_stop_requested(self, session_id):
        return self.stop.is_set()

    def _get_inject_queue(self, session_id):
        return self.queues.setdefault(session_id, queue.Queue())


class _FakeRealtime:
    def __init__(self):
        self.events = []

    def current_turn_id(self, session_id):
        return 'turn-1'

    def publish(self, channel, event_type, payload, **kwargs):
        self.events.append((channel, event_type, payload, kwargs))
        return len(self.events)


class TestAskUserQuestionTool(unittest.TestCase):
    AGENT = {'id': 'agent-1', 'session_id': 'sess-1', 'user_id': 'web_test', 'channel_id': None}

    def setUp(self):
        self.runtime = _FakeRuntime()
        self.realtime = _FakeRealtime()
        self.enabled = '1'
        fake_db = types.SimpleNamespace(get_setting=lambda key, default=None: self.enabled)
        self.modules = patch.dict(sys.modules, {
            'models.db': types.SimpleNamespace(db=fake_db),
            'backend.agent_runtime': types.SimpleNamespace(agent_runtime=self.runtime),
            'backend.realtime_store': types.SimpleNamespace(realtime_store=self.realtime),
        })
        self.modules.start()
        from plugins.ask_user.backend.tools import ask_user_question
        self.tool = ask_user_question

    def tearDown(self):
        self.modules.stop()

    def _run_async(self, agent=None, args=None):
        result = {}
        args = args or {'questions': [_q()]}
        thread = threading.Thread(
            target=lambda: result.update(value=self.tool.execute(agent or self.AGENT, args)))
        thread.start()
        return thread, result

    def _wait_for_question(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            required = [e for e in self.realtime.events if e[1] == 'question_required']
            if required:
                return required[0][2]['question_id']
            time.sleep(0.02)
        self.fail('question_required was never published')

    def test_answered_flow(self):
        thread, result = self._run_async()
        question_id = self._wait_for_question()
        _, error = question_registry.answer(question_id, [{'selected': ['PostgreSQL']}])
        self.assertIsNone(error)
        thread.join(5)

        self.assertEqual(result['value']['status'], 'answered')
        self.assertEqual(result['value']['answers'][0]['answer'], 'PostgreSQL')
        channel, event_type, payload, kwargs = self.realtime.events[-1]
        self.assertEqual((channel, event_type), ('chat', 'question_resolved'))
        self.assertEqual(payload['status'], 'answered')
        self.assertEqual(kwargs['turn_id'], 'turn-1')
        self.assertIsNone(question_registry.get(question_id))

    def test_dismiss_returns_dismissed(self):
        thread, result = self._run_async()
        question_id = self._wait_for_question()
        _, error = question_registry.dismiss(question_id)
        self.assertIsNone(error)
        thread.join(5)
        self.assertEqual(result['value']['status'], 'dismissed')
        self.assertIn('closed the questions', result['value']['message'])
        self.assertEqual(self.realtime.events[-1][2]['status'], 'dismissed')

    def test_works_without_realtime_journal(self):
        # Evonic builds without backend.realtime_store: the tool still blocks
        # and returns the answer, it just journals nothing.
        sys.modules['backend.realtime_store'] = None  # makes the import raise ImportError
        thread, result = self._run_async()
        deadline = time.time() + 5
        while not question_registry.pending_for_session('sess-1') and time.time() < deadline:
            time.sleep(0.02)
        pending = question_registry.pending_for_session('sess-1')
        self.assertEqual(len(pending), 1)
        _, error = question_registry.answer(pending[0].question_id, [{'selected': ['PostgreSQL']}])
        self.assertIsNone(error)
        thread.join(5)
        self.assertEqual(result['value']['status'], 'answered')
        self.assertEqual(self.realtime.events, [])

    def test_stop_cancels_wait(self):
        thread, result = self._run_async()
        self._wait_for_question()
        self.runtime.stop.set()
        thread.join(5)
        self.assertEqual(result['value']['status'], 'cancelled')
        self.assertEqual(self.realtime.events[-1][2]['status'], 'cancelled')

    def test_chat_message_counts_as_reply(self):
        thread, result = self._run_async()
        self._wait_for_question()
        self.runtime._get_inject_queue('sess-1').put({'role': 'user', 'content': 'use mysql'})
        thread.join(5)
        self.assertEqual(result['value']['status'], 'replied_in_chat')
        self.assertEqual(result['value']['reply'], 'use mysql')
        # Left in the queue so the agent loop injects it as a normal user message.
        self.assertEqual(self.runtime._get_inject_queue('sess-1').qsize(), 1)

    def test_heartbeat_journals_on_the_turn(self):
        with patch.object(self.tool, '_HEARTBEAT_SECONDS', 0):
            thread, result = self._run_async()
            question_id = self._wait_for_question()
            time.sleep(0.7)
            question_registry.answer(question_id, [{'selected': ['SQLite']}])
            thread.join(5)
        beats = [e for e in self.realtime.events if e[1] == 'ask_user_waiting']
        self.assertTrue(beats)
        self.assertEqual(beats[0][3]['turn_id'], 'turn-1')

    def test_resolution_is_remembered_for_polling_ui(self):
        thread, result = self._run_async()
        question_id = self._wait_for_question()
        question_registry.dismiss(question_id)
        thread.join(5)
        self.assertIsNone(question_registry.get(question_id))
        self.assertEqual(question_registry.resolved_status(question_id), 'dismissed')

    def test_unavailable_outside_web_chat(self):
        for agent in (
            {**self.AGENT, 'channel_id': 'telegram-1'},
            {**self.AGENT, 'user_id': 'api:key-1'},
            {**self.AGENT, 'user_id': '__scheduler__'},
            {**self.AGENT, 'from_agent_id': 'other-agent'},
        ):
            result = self.tool.execute(agent, {'questions': [_q()]})
            self.assertEqual(result['status'], 'unavailable', agent)
        self.assertEqual(self.realtime.events, [])

    def test_invalid_questions_and_disabled_plugin(self):
        self.assertIn('error', self.tool.execute(self.AGENT, {'questions': []}))
        self.enabled = '0'
        self.assertIn('disabled', self.tool.execute(self.AGENT, {'questions': [_q()]})['error'])


class TestBuildResult(unittest.TestCase):
    def test_messages_reference_answers(self):
        questions, _ = normalize_questions([_q()])
        answers, _ = normalize_answers(questions, [{'selected': ['SQLite']}])
        self.assertIn('"Which database?" = "SQLite"', build_result('answered', questions, answers)['message'])
        self.assertIn('use mysql', build_result('replied_in_chat', questions, chat_reply='use mysql')['message'])
        self.assertEqual(build_result('cancelled', questions)['status'], 'cancelled')


class TestRegistryIsolation(unittest.TestCase):
    def test_pending_excludes_answered(self):
        registry = QuestionRegistry()
        questions, _ = normalize_questions([_q()])
        pq = registry.create('s', 'a', questions)
        self.assertEqual(len(registry.pending_for_session('s')), 1)
        registry.answer(pq.question_id, [{'selected': ['SQLite']}])
        self.assertEqual(registry.pending_for_session('s'), [])


if __name__ == '__main__':
    unittest.main()
