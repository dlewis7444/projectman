"""Tests for Settings._migrate_old_model_shape — the pre-pivot → provider-axis
migration. Pre-pivot providers used a dict models map + transformer; model_default
/ model_overrides held 'provider/model' strings; tier_models did not exist.
"""
from settings import Settings, TIERS


def test_old_providers_dict_models_becomes_list():
    s = Settings(providers={
        'ollama': {
            'name': 'Ollama', 'base_url': 'http://x', 'api_key': 'k',
            'transformer': 'litellm',
            'models': {'qwen': {'name': 'Qwen'}, 'gemma': {'name': 'Gemma'}},
        }
    })
    s._migrate_old_model_shape()
    assert s.providers['ollama']['models'] == ['qwen', 'gemma']
    assert 'transformer' not in s.providers['ollama']


def test_old_model_default_slashes_split_into_provider_and_tiers():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': {'glm': {'name': 'GLM'}}}
    }, model_default='ollama/glm')
    s._migrate_old_model_shape()
    assert s.model_default == 'ollama'
    for tier in TIERS:
        assert s.tier_models[tier] == 'glm'


def test_old_model_overrides_slashes_keep_provider_only():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': {'glm': {'name': 'GLM'}}}
    }, model_overrides={'/p': 'ollama/glm', '/q': ''})
    s._migrate_old_model_shape()
    assert s.model_overrides == {'/p': 'ollama', '/q': ''}


def test_tier_models_scrubbed_when_not_on_active_provider():
    # tier_models points at a model the active provider doesn't have → ''
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm']}},
        model_default='ollama',
        tier_models={'opus': 'gone', 'sonnet': 'glm', 'haiku': 'glm',
                     'subagent': 'glm'})
    s._migrate_old_model_shape()
    assert s.tier_models['opus'] == ''   # 'gone' not in ['glm']
    assert s.tier_models['sonnet'] == 'glm'


def test_tier_models_scrubbed_to_empty_when_native_default():
    s = Settings(model_default='',
                 tier_models={'opus': 'x', 'sonnet': 'y'})
    s._migrate_old_model_shape()
    for tier in TIERS:
        assert s.tier_models[tier] == ''


def test_stray_tier_keys_dropped():
    s = Settings(tier_models={'opus': '', 'bogus': 'x', 'sonnet': ''})
    s._migrate_old_model_shape()
    assert set(s.tier_models.keys()) <= set(TIERS)


def test_dropped_fields_silently_vanish():
    # ccr_* / agent_default / agent_overrides are no longer dataclass fields,
    # so the known-field filter drops them on load. Constructing Settings with
    # them must not raise (unknown kwargs would, but these are simply absent
    # from the dataclass now).
    s = Settings()
    assert not hasattr(s, 'ccr_managed')
    assert not hasattr(s, 'agent_default')
    assert not hasattr(s, 'agent_overrides')
    assert not hasattr(s, 'resolved_ccr_binary')


def test_new_shape_passes_through_unchanged():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm', 'qwen']}},
        model_default='ollama',
        model_overrides={'/p': 'ollama'},
        tier_models={'opus': 'qwen', 'sonnet': '', 'haiku': '', 'subagent': ''})
    s._migrate_old_model_shape()
    assert s.providers['ollama']['models'] == ['glm', 'qwen']
    assert s.model_default == 'ollama'
    assert s.model_overrides == {'/p': 'ollama'}
    assert s.tier_models['opus'] == 'qwen'
    assert s.tier_models['sonnet'] == ''


def test_malformed_old_shape_degrades_to_defaults():
    # providers value not a dict, models not a list/dict, weird tier value
    s = Settings(providers={'bad': 'x'},
                 model_default=123,
                 model_overrides=None,
                 tier_models={'opus': 9})
    # must not raise
    s._migrate_old_model_shape()
    assert s.model_default == ''
    assert s.model_overrides == {}
    for tier in TIERS:
        assert s.tier_models[tier] == ''


def test_load_runs_migration_on_old_file(tmp_path):
    import json
    p = tmp_path / 'settings.json'
    p.write_text(json.dumps({
        'providers': {'ollama': {'name': 'O', 'base_url': 'http://x',
                                  'api_key': 'k',
                                  'models': {'glm': {'name': 'GLM'}}}},
        'model_default': 'ollama/glm',
        'model_overrides': {'/p': 'ollama/glm'},
    }))
    s = Settings.load(str(p))
    assert s.providers['ollama']['models'] == ['glm']
    assert s.model_default == 'ollama'
    assert s.model_overrides == {'/p': 'ollama'}
    assert s.tier_models['opus'] == 'glm'