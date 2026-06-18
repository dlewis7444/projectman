"""Tests for models.build_spawn_env + aggregate_fallback_notices.

Pins the ollama-style env dict that ProjectMan injects when a custom provider
is active, the native no-injection path, the misconfiguration fallback, and the
fallback-toast aggregation.
"""
import os

from settings import Settings, TIERS
from models import build_spawn_env, aggregate_fallback_notices


def _provider(pid='ollama', base_url='http://localhost:11434', api_key='secret-key',
              models=None):
    return {
        pid: {
            'name': 'Ollama',
            'base_url': base_url,
            'api_key': api_key,
            'models': models if models is not None else ['glm-5.2:cloud[1m]'],
        }
    }


# --- native path -------------------------------------------------------------

def test_native_returns_none_env_and_no_reason():
    s = Settings()
    env, reason = build_spawn_env(s, '/p')
    assert env is None
    assert reason is None


def test_per_project_override_to_native_returns_none():
    s = Settings(providers=_provider(), model_default='ollama',
                 model_overrides={'/p': ''})
    env, reason = build_spawn_env(s, '/p')
    assert env is None
    assert reason is None


# --- custom provider path ---------------------------------------------------

def test_custom_provider_injects_full_env():
    s = Settings(providers=_provider(), model_default='ollama')
    env, reason = build_spawn_env(s, '/p')
    assert reason is None
    assert env is not None
    assert env['ANTHROPIC_BASE_URL'] == 'http://localhost:11434'
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'secret-key'
    assert env['ANTHROPIC_API_KEY'] == ''            # anti-block shape (empty)
    assert env['CLAUDE_CODE_ATTRIBUTION_HEADER'] == '0'
    assert env['OLLAMA_HOST'] == 'http://localhost:11434'
    assert env['DISABLE_AUTOUPDATER'] == '1'


def test_custom_provider_resolves_all_four_tiers():
    s = Settings(providers=_provider(models=['a', 'b']), model_default='ollama',
                 tier_models={'opus': 'b', 'sonnet': 'a', 'haiku': '', 'subagent': ''})
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'b'
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'a'
    # '' tier → provider's first model
    assert env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'a'
    assert env['CLAUDE_CODE_SUBAGENT_MODEL'] == 'a'


def test_tier_models_empty_uses_provider_first_model():
    s = Settings(providers=_provider(models=['first', 'second']),
                 model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    for tier in ('opus', 'sonnet', 'haiku', 'subagent'):
        if tier == 'opus':
            key = 'ANTHROPIC_DEFAULT_OPUS_MODEL'
        elif tier == 'sonnet':
            key = 'ANTHROPIC_DEFAULT_SONNET_MODEL'
        elif tier == 'haiku':
            key = 'ANTHROPIC_DEFAULT_HAIKU_MODEL'
        else:
            key = 'CLAUDE_CODE_SUBAGENT_MODEL'
        assert env[key] == 'first'


def test_custom_provider_inherits_parent_environ():
    os.environ['PM_TEST_PARENT_VAR'] = 'present'
    try:
        s = Settings(providers=_provider(), model_default='ollama')
        env, _ = build_spawn_env(s, '/p')
        assert env['PM_TEST_PARENT_VAR'] == 'present'
    finally:
        del os.environ['PM_TEST_PARENT_VAR']


def test_per_project_override_to_provider_uses_it():
    s = Settings(providers={**_provider('ollama', base_url='http://a'),
                            **_provider('mistral', base_url='http://b',
                                        models=['m'])},
                 model_default='ollama',
                 model_overrides={'/p': 'mistral'})
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_BASE_URL'] == 'http://b'


# --- misconfiguration fallback ----------------------------------------------

def test_missing_provider_falls_back_native_with_reason():
    s = Settings(model_default='ghost')  # provider not defined
    env, reason = build_spawn_env(s, '/p')
    assert env is None
    assert reason is not None
    assert 'ghost' in reason


def test_provider_without_base_url_falls_back_native_with_reason():
    s = Settings(providers={'p': {'name': 'P', 'base_url': '', 'api_key': '',
                                   'models': ['m']}},
                 model_default='p')
    env, reason = build_spawn_env(s, '/p')
    assert env is None
    assert reason is not None
    assert 'base_url' in reason


# --- aggregate_fallback_notices --------------------------------------------

def test_aggregate_empty_returns_none():
    assert aggregate_fallback_notices([]) is None


def test_aggregate_single_project_verbatim_format():
    text = aggregate_fallback_notices([('proj', 'provider X has no base_url')])
    assert text == ('provider unavailable — running native Claude. '
                    'provider X has no base_url')


def test_aggregate_same_reason_collapses():
    events = [('a', 'r1'), ('b', 'r1'), ('c', 'r1')]
    text = aggregate_fallback_notices(events)
    assert text == ('provider unavailable — 3 projects running native Claude. r1')


def test_aggregate_distinct_reasons_return_list():
    events = [('a', 'r1'), ('b', 'r2')]
    out = aggregate_fallback_notices(events)
    assert isinstance(out, list)
    assert len(out) == 2
    assert ('provider unavailable — running native Claude. r1') in out
    assert ('provider unavailable — running native Claude. r2') in out


def test_all_four_tiers_canonical():
    assert set(TIERS) == {'opus', 'sonnet', 'haiku', 'subagent'}