"""A one-off session on an override model walks the fallbacks it was given with it."""

from unittest.mock import patch

from backend.agent_runtime import llm_loop


class _FakeDb:
    models = {
        'm-b': {'id': 'm-b', 'enabled': True, 'provider': 'p-on'},
        'm-off': {'id': 'm-off', 'enabled': False, 'provider': 'p-on'},
        'm-prov-off': {'id': 'm-prov-off', 'enabled': True, 'provider': 'p-off'},
        'm-c': {'id': 'm-c', 'enabled': True, 'provider': 'p-on'},
    }
    providers = {'p-on': {'id': 'p-on', 'enabled': True}, 'p-off': {'id': 'p-off', 'enabled': False}}

    def get_model_by_id(self, model_id):
        return self.models.get(model_id)

    def get_provider(self, provider_id):
        return self.providers.get(provider_id)


def test_only_usable_models_of_the_list_are_tried_in_order():
    data = {'model_override_id': 'm-a',
            'model_override_fallback_ids': ['m-a', 'm-gone', 'm-off', 'm-prov-off', 'm-c', 'm-b', 'm-c']}
    with patch.object(llm_loop, 'db', _FakeDb()):
        chain = llm_loop._session_override_fallbacks(data, {'id': 'm-a'})
    assert [m['id'] for m in chain] == ['m-c', 'm-b']


def test_no_list_gives_no_candidates():
    with patch.object(llm_loop, 'db', _FakeDb()):
        assert llm_loop._session_override_fallbacks({'model_override_id': 'm-a'}, {'id': 'm-a'}) == []
        assert llm_loop._session_override_fallbacks(None, {'id': 'm-a'}) == []
