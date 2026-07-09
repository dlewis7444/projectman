"""Tests for Settings._migrate_old_model_shape — the pre-pivot → provider-axis
migration. Pre-pivot providers used a dict models map + transformer; model_default
/ model_overrides held 'provider/model' strings; tier_models did not exist.

tier_models is now PER-PROVIDER: ``{provider_id: {tier: model_id|''}}``. The
legacy GLOBAL shape ``{tier: model_id}`` is folded into one provider on migrate.
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
    # Legacy glm|deepseek auto-[1m] migration rewrites the list + tier pins.
    assert s.providers['ollama']['models'] == ['glm[1m]']
    for tier in TIERS:
        assert s.tier_models['ollama'][tier] == 'glm[1m]'


def test_old_model_overrides_slashes_keep_provider_only():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': {'glm': {'name': 'GLM'}}}
    }, model_overrides={'/p': 'ollama/glm', '/q': ''})
    s._migrate_old_model_shape()
    assert s.model_overrides == {'/p': 'ollama', '/q': ''}


def test_tier_models_scrubbed_when_not_on_active_provider():
    # Legacy global tier_models points at a model the provider doesn't have → ''.
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm']}},
        model_default='ollama',
        tier_models={'opus': 'gone', 'sonnet': 'glm', 'haiku': 'glm',
                     'subagent': 'glm'})
    s._migrate_old_model_shape()
    assert s.tier_models['ollama']['opus'] == ''   # 'gone' not in models
    # glm matched legacy 1M heuristic → stored as glm[1m] on list + pins.
    assert s.providers['ollama']['models'] == ['glm[1m]']
    assert s.tier_models['ollama']['sonnet'] == 'glm[1m]'


def test_tier_models_dropped_when_native_default_and_no_providers():
    # Native default + no custom provider → nothing to fold the legacy global
    # tiers into, so they're dropped (they were inert under native anyway).
    s = Settings(model_default='',
                 tier_models={'opus': 'x', 'sonnet': 'y'})
    s._migrate_old_model_shape()
    assert s.tier_models == {}


def test_legacy_global_tier_models_fold_into_default_when_custom():
    # Legacy global tiers fold into model_default when it's a custom provider.
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm', 'qwen']},
        'openrouter': {'name': 'OR', 'base_url': 'http://y', 'api_key': 'k2',
                       'models': ['m']}},
        model_default='ollama',
        tier_models={'opus': 'glm', 'sonnet': '', 'haiku': '', 'subagent': ''})
    s._migrate_old_model_shape()
    assert set(s.tier_models.keys()) == {'ollama'}   # not spread to openrouter
    assert s.tier_models['ollama']['opus'] == 'glm[1m]'
    assert s.tier_models['ollama']['sonnet'] == ''


def test_legacy_global_tier_models_fold_into_first_custom_when_native_default():
    # Native default but a custom provider exists → fold into the first (sorted)
    # custom provider so the user's prior assignments aren't lost.
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm']},
        'openrouter': {'name': 'OR', 'base_url': 'http://y', 'api_key': 'k2',
                       'models': ['m']}},
        model_default='',
        tier_models={'opus': 'glm', 'sonnet': 'glm', 'haiku': '',
                     'subagent': ''})
    s._migrate_old_model_shape()
    assert set(s.tier_models.keys()) == {'ollama'}   # sorted-first custom
    assert s.tier_models['ollama']['opus'] == 'glm[1m]'
    assert s.tier_models['ollama']['sonnet'] == 'glm[1m]'
    assert 'openrouter' not in s.tier_models


def test_stray_tier_keys_dropped_per_provider():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': []}},
        tier_models={'ollama': {'opus': '', 'bogus': 'x', 'sonnet': ''}})
    s._migrate_old_model_shape()
    assert set(s.tier_models['ollama'].keys()) == set(TIERS)
    assert 'bogus' not in s.tier_models['ollama']


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
        tier_models={'ollama': {'opus': 'qwen', 'sonnet': '', 'haiku': '',
                                'subagent': '', 'fable': ''}})
    s._migrate_old_model_shape()
    # Bare glm still gets the one-shot legacy [1m] migration; qwen does not.
    assert s.providers['ollama']['models'] == ['glm[1m]', 'qwen']
    assert s.model_default == 'ollama'
    assert s.model_overrides == {'/p': 'ollama'}
    assert s.tier_models['ollama']['opus'] == 'qwen'
    assert s.tier_models['ollama']['sonnet'] == ''
    assert s.tier_models['ollama']['fable'] == ''


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
    assert s.tier_models == {}   # 'opus' isn't a known provider id → dropped


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
    assert s.providers['ollama']['models'] == ['glm[1m]']
    assert s.model_default == 'ollama'
    assert s.model_overrides == {'/p': 'ollama'}
    assert s.tier_models['ollama']['opus'] == 'glm[1m]'


def test_legacy_1m_migration_appends_suffix_and_rewrites_tiers():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm-5.2:cloud', 'deepseek-v3', 'kimi']}},
        model_default='ollama',
        tier_models={'ollama': {
            'opus': 'glm-5.2:cloud', 'sonnet': 'kimi', 'haiku': '',
            'subagent': '', 'fable': 'deepseek-v3',
        }})
    s._migrate_old_model_shape()
    assert s.providers['ollama']['models'] == [
        'glm-5.2:cloud[1m]', 'deepseek-v3[1m]', 'kimi']
    assert s.tier_models['ollama']['opus'] == 'glm-5.2:cloud[1m]'
    assert s.tier_models['ollama']['fable'] == 'deepseek-v3[1m]'
    assert s.tier_models['ollama']['sonnet'] == 'kimi'


def test_legacy_1m_migration_idempotent_when_already_suffixed():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['glm-5.2:cloud[1m]']}},
        model_default='ollama',
        tier_models={'ollama': {
            'opus': 'glm-5.2:cloud[1m]', 'sonnet': '', 'haiku': '',
            'subagent': '', 'fable': '',
        }})
    s._migrate_old_model_shape()
    assert s.providers['ollama']['models'] == ['glm-5.2:cloud[1m]']
    assert s.tier_models['ollama']['opus'] == 'glm-5.2:cloud[1m]'


def test_max_context_tokens_normalized_on_migrate():
    s = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['m'], 'max_context_tokens': '128000'}})
    s._migrate_old_model_shape()
    assert s.providers['ollama']['max_context_tokens'] == 128000
    s2 = Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://x', 'api_key': 'k',
                   'models': ['m'], 'max_context_tokens': 0}})
    s2._migrate_old_model_shape()
    assert 'max_context_tokens' not in s2.providers['ollama']