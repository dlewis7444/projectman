"""Tests for the classifier env-var levers in models.build_spawn_env."""
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


def test_unset_classifier_vars_are_omitted():
    """When no classifier levers are configured, the env dict must NOT
    contain the classifier env vars so CC uses its own defaults."""
    s = Settings(providers=_provider(models=['a', 'b']), model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    for key in ('CLAUDE_CODE_AUTO_MODE_MODEL',
                'CLAUDE_CODE_BG_CLASSIFIER_MODEL',
                'CLAUDE_CODE_AUTO_MODE_TEMPERATURE',
                'CLAUDE_CODE_TWO_STAGE_CLASSIFIER'):
        assert key not in env


def test_classifier_model_picks_per_provider():
    s = Settings(
        providers=_provider(models=['a', 'b', 'c']),
        model_default='ollama',
        classifier_models={'ollama': {'auto_mode': 'b', 'bg_classifier': 'c'}},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_AUTO_MODE_MODEL'] == 'b'
    assert env['CLAUDE_CODE_BG_CLASSIFIER_MODEL'] == 'c'


def test_classifier_models_stale_value_omitted():
    """A classifier model id no longer on the provider is treated as unset."""
    s = Settings(
        providers=_provider(models=['a']),
        model_default='ollama',
        classifier_models={'ollama': {'auto_mode': 'gone', 'bg_classifier': ''}},
    )
    env, _ = build_spawn_env(s, '/p')
    assert 'CLAUDE_CODE_AUTO_MODE_MODEL' not in env
    assert 'CLAUDE_CODE_BG_CLASSIFIER_MODEL' not in env


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


def test_classifier_two_stage_true_emits_one():
    s = Settings(
        providers=_provider(),
        model_default='ollama',
        classifier_two_stage={'ollama': True},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_TWO_STAGE_CLASSIFIER'] == '1'


def test_classifier_two_stage_false_emits_zero():
    s = Settings(
        providers=_provider(),
        model_default='ollama',
        classifier_two_stage={'ollama': False},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_TWO_STAGE_CLASSIFIER'] == '0'


def test_classifier_two_stage_unset_omitted():
    s = Settings(providers=_provider(), model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    assert 'CLAUDE_CODE_TWO_STAGE_CLASSIFIER' not in env


def test_classifier_vars_resolve_against_override_provider():
    """Classifier levers are per-provider, so a per-project override uses the
    override provider's classifier config, not the default's."""
    s = Settings(
        providers={
            ** _provider('ollama', base_url='http://a', models=['oa']),
            ** _provider('openrouter', base_url='http://b', models=['or1', 'or2']),
        },
        model_default='ollama',
        model_overrides={'/p': 'openrouter'},
        classifier_models={
            'ollama': {'auto_mode': 'oa'},
            'openrouter': {'auto_mode': 'or2'},
        },
        classifier_temperature={'ollama': 0.1, 'openrouter': 0.9},
        classifier_two_stage={'ollama': True, 'openrouter': False},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_BASE_URL'] == 'http://b'
    assert env['CLAUDE_CODE_AUTO_MODE_MODEL'] == 'or2'
    assert env['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] == '0.9'
    assert env['CLAUDE_CODE_TWO_STAGE_CLASSIFIER'] == '0'


def test_classifier_vars_scrub_inherited_parent_env():
    """When unset, inherited classifier env vars are scrubbed so a stale
    launcher value doesn't leak into the spawned session."""
    os.environ['CLAUDE_CODE_AUTO_MODE_MODEL'] = 'inherited-auto'
    os.environ['CLAUDE_CODE_BG_CLASSIFIER_MODEL'] = 'inherited-bg'
    os.environ['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] = '9.9'
    os.environ['CLAUDE_CODE_TWO_STAGE_CLASSIFIER'] = '1'
    try:
        s = Settings(providers=_provider(models=['a']), model_default='ollama')
        env, _ = build_spawn_env(s, '/p')
        assert 'CLAUDE_CODE_AUTO_MODE_MODEL' not in env
        assert 'CLAUDE_CODE_BG_CLASSIFIER_MODEL' not in env
        assert 'CLAUDE_CODE_AUTO_MODE_TEMPERATURE' not in env
        assert 'CLAUDE_CODE_TWO_STAGE_CLASSIFIER' not in env
    finally:
        for key in ('CLAUDE_CODE_AUTO_MODE_MODEL',
                    'CLAUDE_CODE_BG_CLASSIFIER_MODEL',
                    'CLAUDE_CODE_AUTO_MODE_TEMPERATURE',
                    'CLAUDE_CODE_TWO_STAGE_CLASSIFIER'):
            del os.environ[key]


def test_classifier_glm_auto_mode_keeps_verbatim_id():
    """The classifier model id is emitted verbatim (no [1m] suffix logic).
    Only the Opus tier gets the GLM 1M suffix."""
    s = Settings(
        providers=_provider(models=['glm-5.2:cloud']),
        model_default='ollama',
        classifier_models={'ollama': {'auto_mode': 'glm-5.2:cloud'}},
    )
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_AUTO_MODE_MODEL'] == 'glm-5.2:cloud'
