"""Tests for the classifier temperature lever in models.build_spawn_env."""
import os

from settings import Settings
from models import build_spawn_env


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


def test_unset_classifier_temperature_is_omitted():
    """When no classifier temperature is configured, the env dict must NOT
    contain CLAUDE_CODE_AUTO_MODE_TEMPERATURE so CC uses its own default."""
    s = Settings(providers=_provider(models=['a', 'b']), model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    assert 'CLAUDE_CODE_AUTO_MODE_TEMPERATURE' not in env


def test_classifier_temperature_present():
    s = Settings(
        providers=_provider(),
        model_default='ollama',
        classifier_temperature={'ollama': 0.25},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] == '0.25'


def test_classifier_temperature_zero_is_emitted():
    """Zero is a valid explicit temperature and must be emitted, not treated
    as unset."""
    s = Settings(
        providers=_provider(),
        model_default='ollama',
        classifier_temperature={'ollama': 0.0},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] == '0.0'


def test_classifier_temperature_unset_omitted():
    s = Settings(providers=_provider(), model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    assert 'CLAUDE_CODE_AUTO_MODE_TEMPERATURE' not in env


def test_classifier_temperature_resolves_against_override_provider():
    """Classifier temperature is per-provider, so a per-project override uses
    the override provider's temperature, not the default's."""
    s = Settings(
        providers={
            **_provider('ollama', base_url='http://a'),
            **_provider('openrouter', base_url='http://b', models=['or1']),
        },
        model_default='ollama',
        model_overrides={'/p': 'openrouter'},
        classifier_temperature={'ollama': 0.1, 'openrouter': 0.9},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_BASE_URL'] == 'http://b'
    assert env['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] == '0.9'


def test_classifier_temperature_scrubs_inherited_parent_env():
    """When unset, an inherited CLAUDE_CODE_AUTO_MODE_TEMPERATURE is scrubbed
    so a stale launcher value doesn't leak into the spawned session."""
    os.environ['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] = '9.9'
    try:
        s = Settings(providers=_provider(models=['a']), model_default='ollama')
        env, _ = build_spawn_env(s, '/p')
        assert 'CLAUDE_CODE_AUTO_MODE_TEMPERATURE' not in env
    finally:
        del os.environ['CLAUDE_CODE_AUTO_MODE_TEMPERATURE']
