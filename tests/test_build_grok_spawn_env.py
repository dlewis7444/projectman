"""Tests for models.build_grok_spawn_env + resolve_grok_model and the
GrokAdapter wiring that consumes them (spawn/headless/zellij).

Pins the env-only custom-provider shape ProjectMan injects for Grok Build
(probe-verified 2026-10-05, grok 1.0.46): derived GROK_MODELS_BASE_URL
(+ /v1 when the catalog path is unversioned), XAI_API_KEY ('dummy' when the
provider has no key), the explicit -m model resolution ([1m] stripped,
pin-not-in-list → provider's first model), the native no-injection paths
(incl. the PAA '' headless invariant), and the provider menu entries.
"""
import os
from types import SimpleNamespace

import pytest

from settings import Settings
from models import (
    NATIVE_GROK,
    _grok_base_url,
    build_grok_spawn_env,
    build_provider_menu_entries,
    grok_home_dir,
    provider_menu_current,
    resolve_grok_model,
)


def _provider(pid='ollama', base_url='http://localhost:11434', api_key='secret-key',
              models=None):
    return {
        pid: {
            'name': 'Ollama',
            'base_url': base_url,
            'api_key': api_key,
            'models': models if models is not None else ['qwen3.5:cloud'],
        }
    }


# --- base URL derivation -----------------------------------------------------

@pytest.mark.parametrize('base,expected', [
    ('http://localhost:11434', 'http://localhost:11434/v1'),
    ('http://localhost:11434/', 'http://localhost:11434/v1'),
    ('https://api.kimi.com/coding/', 'https://api.kimi.com/coding/v1'),
    ('http://localhost:11434/v1', 'http://localhost:11434/v1'),
    ('http://localhost:11434/v1/', 'http://localhost:11434/v1'),
    ('https://api.foo.com/v2', 'https://api.foo.com/v2'),
    ('http://host/api/v2beta', 'http://host/api/v2beta'),       # alpha suffix
    ('http://host/api/V1', 'http://host/api/V1'),             # case-insensitive
    ('http://host:8080/path?query=1', 'http://host:8080/path/v1?query=1'),
    ('http://host:8080/path?query=1#frag',
     'http://host:8080/path/v1?query=1#frag'),
    ('http://host', 'http://host/v1'),
    ('', ''),
])
def test_grok_base_url_derivation(base, expected):
    assert _grok_base_url(base) == expected


# --- native path -------------------------------------------------------------

def test_native_returns_none_env_and_no_reason():
    env, reason = build_grok_spawn_env(Settings(), '/p')
    assert env is None
    assert reason is None


def test_per_project_override_to_native_returns_none():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'},
                 provider_overrides={'/p': ''})
    env, reason = build_grok_spawn_env(s, '/p')
    assert env is None
    assert reason is None


# --- custom provider path -----------------------------------------------------

def test_custom_provider_injects_grok_env_only():
    os.environ['PM_TEST_PARENT_VAR'] = 'present'
    for junk in ('ANTHROPIC_BASE_URL', 'GROK_MODEL', 'GROK_DEFAULT_MODEL'):
        os.environ.pop(junk, None)
    try:
        s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
        env, reason = build_grok_spawn_env(s, '/p')
        assert reason is None
        assert env['PM_TEST_PARENT_VAR'] == 'present'
        assert env['GROK_MODELS_BASE_URL'] == 'http://localhost:11434/v1'
        assert env['XAI_API_KEY'] == 'secret-key'
        # Never emitted: no GROK_MODEL (does not exist), no default-model var,
        # and no claude-shaped keys either.
        assert 'GROK_MODEL' not in env
        assert 'GROK_DEFAULT_MODEL' not in env
        assert 'ANTHROPIC_BASE_URL' not in env
    finally:
        del os.environ['PM_TEST_PARENT_VAR']


def test_empty_api_key_injects_dummy():
    s = Settings(providers=_provider(api_key=''), provider_defaults={'grok': 'ollama'})
    env, _ = build_grok_spawn_env(s, '/p')
    assert env['XAI_API_KEY'] == 'dummy'


def test_per_project_override_to_provider_uses_it():
    s = Settings(providers={**_provider('ollama', base_url='http://a'),
                            **_provider('mistral', base_url='http://b',
                                        models=['m'])},
                 provider_defaults={'grok': 'ollama'},
                 provider_overrides={'/p': 'mistral'})
    env, _ = build_grok_spawn_env(s, '/p')
    assert env['GROK_MODELS_BASE_URL'] == 'http://b/v1'


# --- misconfiguration fallback (same shape as claude's reasons) ---------------

def test_missing_provider_falls_back_native_with_reason():
    """A provider that exists but has no base_url → fallback reason."""
    s = Settings(providers={'p': {'name': 'P', 'base_url': '', 'api_key': '',
                                  'models': ['m']}},
                 provider_defaults={'grok': 'p'})
    env, reason = build_grok_spawn_env(s, '/p')
    assert env is None
    assert reason is not None
    assert 'base_url' in reason


def test_stale_grok_default_is_silently_native():
    s = Settings(provider_defaults={'grok': 'ghost'})
    assert build_grok_spawn_env(s, '/p') == (None, None)


def test_provider_without_base_url_falls_back_native_with_reason():
    s = Settings(providers={'p': {'name': 'P', 'base_url': '', 'api_key': '',
                                  'models': ['m']}},
                 provider_defaults={'grok': 'p'})
    env, reason = build_grok_spawn_env(s, '/p')
    assert env is None
    assert reason is not None
    assert 'base_url' in reason


# --- resolve_grok_model --------------------------------------------------------

def test_resolve_native_pin_verbatim_and_no_pin_none():
    s = Settings(model_pins={'/p': 'grok-fast'})
    assert resolve_grok_model(s, '/p') == 'grok-fast'
    assert resolve_grok_model(Settings(), '/p') is None


def test_resolve_custom_pin_stripped_of_1m():
    s = Settings(providers=_provider(models=['qwen[1m]', 'other']),
                 provider_defaults={'grok': 'ollama'}, model_pins={'/p': 'qwen[1m]'})
    assert resolve_grok_model(s, '/p') == 'qwen'


def test_resolve_stale_pin_falls_to_first_model():
    s = Settings(providers=_provider(models=['first', 'second']),
                 provider_defaults={'grok': 'ollama'}, model_pins={'/p': 'gone'})
    assert resolve_grok_model(s, '/p') == 'first'


def test_resolve_no_pin_uses_first_model():
    s = Settings(providers=_provider(models=['first', 'second']),
                 provider_defaults={'grok': 'ollama'})
    assert resolve_grok_model(s, '/p') == 'first'


def test_resolve_pin_membership_compares_stripped_ids():
    """Pin without [1m] must still match a [1m]-flagged catalog entry."""
    s = Settings(providers=_provider(models=['qwen[1m]', 'other']),
                 provider_defaults={'grok': 'ollama'}, model_pins={'/p': 'qwen'})
    assert resolve_grok_model(s, '/p') == 'qwen'


def test_resolve_empty_model_list_trusts_pin():
    s = Settings(providers=_provider(models=[]), provider_defaults={'grok': 'ollama'},
                 model_pins={'/p': 'k3[1m]'})
    assert resolve_grok_model(s, '/p') == 'k3'


def test_resolve_empty_model_list_no_pin_is_none():
    s = Settings(providers=_provider(models=[]), provider_defaults={'grok': 'ollama'})
    assert resolve_grok_model(s, '/p') is None


# --- GrokAdapter wiring ---------------------------------------------------------

def _adapter():
    from harnesses import GrokAdapter
    return GrokAdapter()


def _proj(path='/p'):
    return SimpleNamespace(path=path)


def test_spawn_plan_custom_provider_env_and_model():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    plan = _adapter().spawn_plan(s, _proj(), 'fresh')
    assert plan.argv == ['grok', '-m', 'qwen3.5:cloud']
    assert plan.env['GROK_MODELS_BASE_URL'] == 'http://localhost:11434/v1'
    assert plan.env['XAI_API_KEY'] == 'secret-key'
    assert plan.env['GROK_HOME'] == str(grok_home_dir('ollama'))
    assert plan.fallback_reason is None


def test_spawn_plan_native_env_none():
    plan = _adapter().spawn_plan(Settings(), _proj(), 'continue')
    assert plan.env is None
    assert plan.fallback_reason is None
    # 'continue' wraps argv in the bash continue/fallback trap.
    assert any('grok -c' in a for a in plan.argv)


def test_spawn_plan_unusable_provider_falls_back_with_reason():
    s = Settings(providers={'p': {'name': 'P', 'base_url': '', 'api_key': '',
                                  'models': ['m']}},
                 provider_defaults={'grok': 'p'})
    plan = _adapter().spawn_plan(s, _proj(), 'fresh')
    assert plan.env is None
    assert plan.fallback_reason is not None
    assert 'base_url' in plan.fallback_reason


def test_spawn_plan_resume_mode_carries_provider_env_and_model():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    plan = _adapter().spawn_plan(s, _proj(), 'resume', session_id='019f-sid')
    assert plan.argv == ['grok', '-m', 'qwen3.5:cloud', '-r', '019f-sid']
    assert plan.env['GROK_MODELS_BASE_URL'] == 'http://localhost:11434/v1'
    assert plan.env['XAI_API_KEY'] == 'secret-key'
    assert plan.env['GROK_HOME'] == str(grok_home_dir('ollama'))
    assert plan.fallback_reason is None


def test_headless_custom_provider_merges_env_and_keeps_compat_keys():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    plan = _adapter().headless_plan('scan', s, '/p')
    assert plan.env['GROK_MODELS_BASE_URL'] == 'http://localhost:11434/v1'
    assert plan.env['XAI_API_KEY'] == 'secret-key'
    assert plan.env['GROK_HOME'] == str(grok_home_dir('ollama'))
    assert plan.env['GROK_CLAUDE_MCPS_ENABLED'] == '0'
    assert plan.env['GROK_CURSOR_MCPS_ENABLED'] == '0'
    assert plan.argv[plan.argv.index('-m') + 1] == 'qwen3.5:cloud'


def test_headless_native_non_empty_path_unchanged():
    plan = _adapter().headless_plan('scan', Settings(), '/p')
    assert 'GROK_MODELS_BASE_URL' not in plan.env
    assert plan.env['GROK_CLAUDE_MCPS_ENABLED'] == '0'
    assert '-m' not in plan.argv


def test_headless_empty_path_paa_invariant_stays_native():
    """PAA Telegram turns pass project_path='': even with a global custom
    provider the env AND argv stay on the historical native posture (no
    provider env, no provider-driven -m)."""
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    plan = _adapter().headless_plan('scan', s, '')
    assert 'GROK_MODELS_BASE_URL' not in plan.env
    assert 'GROK_HOME' not in plan.env
    assert '-m' not in plan.argv
    assert plan.env['GROK_CLAUDE_MCPS_ENABLED'] == '0'
    assert plan.env['GROK_CURSOR_MCPS_ENABLED'] == '0'


def test_headless_explicit_model_kwarg_wins_over_provider():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    plan = _adapter().headless_plan('scan', s, '/p', model='pool-qwen')
    assert plan.argv[plan.argv.index('-m') + 1] == 'pool-qwen'


def test_headless_gate_keys_merge_over_provider_env():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    plan = _adapter().headless_plan('p', s, '/p', tool_policy='gate',
                                    approval_sock='/tmp/paa.sock',
                                    approval_token='tok')
    assert plan.env['PAA_APPROVAL_SOCK'] == '/tmp/paa.sock'
    assert plan.env['PAA_APPROVAL_TOKEN'] == 'tok'
    assert plan.env['GROK_MODELS_BASE_URL'] == 'http://localhost:11434/v1'
    assert plan.env['GROK_CLAUDE_MCPS_ENABLED'] == '0'


def test_zellij_spawn_env_delegates_to_grok_env():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    env, reason = _adapter().zellij_spawn_env(s, _proj())
    assert reason is None
    assert env['GROK_MODELS_BASE_URL'] == 'http://localhost:11434/v1'
    assert env['GROK_HOME'] == str(grok_home_dir('ollama'))
    env, reason = _adapter().zellij_spawn_env(Settings(), _proj())
    assert env is None
    assert reason is None


def test_zellij_continue_command_carries_resolved_model():
    s = Settings(providers=_provider(), provider_defaults={'grok': 'ollama'})
    cmd = _adapter().zellij_continue_command(s, _proj())
    assert cmd == 'grok -m qwen3.5:cloud -c || grok -m qwen3.5:cloud'


# --- grok provider menu ----------------------------------------------------------

def test_build_provider_menu_entries_grok_native_plus_customs():
    s = Settings(providers=_provider())
    entries = build_provider_menu_entries(s, 'grok')
    ids = [e[0] for e in entries]
    assert ids == [NATIVE_GROK, 'ollama']
    assert all(e[2] for e in entries)


def test_provider_menu_current_grok_reflects_override():
    providers = {**_provider(),
                 **_provider('mistral', base_url='http://b', models=['m'])}
    s = Settings(providers=providers, provider_defaults={'grok': 'ollama'})
    assert provider_menu_current(s, '/p', 'grok') == 'ollama'
    s2 = Settings(providers=providers, provider_defaults={'grok': 'ollama'},
                  provider_overrides={'/p': 'mistral'})
    assert provider_menu_current(s2, '/p', 'grok') == 'mistral'
    assert provider_menu_current(Settings(), '/p', 'grok') == NATIVE_GROK
