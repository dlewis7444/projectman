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
                 provider_overrides={'/p': ''})
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
                 tier_models={'ollama': {'opus': 'b', 'sonnet': 'a', 'haiku': '',
                                         'subagent': ''}})
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'b'
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'a'
    # '' tier → provider's first model
    assert env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'a'
    # Subagent tier unset ('') → NOT forced (opt-in force policy); the env var
    # is omitted so per-call model:"sonnet" can route image subagents and
    # default subagents fall to CC's global default.
    assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in env


def test_tier_models_empty_uses_provider_first_model():
    s = Settings(providers=_provider(models=['first', 'second']),
                 model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    for tier in ('opus', 'sonnet', 'haiku'):
        key = {'opus': 'ANTHROPIC_DEFAULT_OPUS_MODEL',
               'sonnet': 'ANTHROPIC_DEFAULT_SONNET_MODEL',
               'haiku': 'ANTHROPIC_DEFAULT_HAIKU_MODEL'}[tier]
        assert env[key] == 'first'
    # No explicit subagent tier → not forced.
    assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in env


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
                 provider_overrides={'/p': 'mistral'})
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_BASE_URL'] == 'http://b'


def test_per_provider_tiers_resolve_against_override_provider():
    """Tier assignments are per-provider. A project overridden to a non-default
    provider injects THAT provider's tier assignments — not the default's —
    proving TA applies to any added provider, even when it isn't the default
    (and even when the default is native)."""
    s = Settings(providers={**_provider('ollama', base_url='http://a',
                                        models=['glm', 'kimi']),
                            **_provider('openrouter', base_url='http://b',
                                        models=['or-opus', 'or-sonnet'])},
                 model_default='',  # native default — TA still applies to overrides
                 provider_overrides={'/p': 'openrouter'},
                 tier_models={
                     'ollama': {'opus': 'glm', 'sonnet': 'kimi'},
                     'openrouter': {'opus': 'or-opus', 'sonnet': 'or-sonnet'},
                 })
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_BASE_URL'] == 'http://b'
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'or-opus'      # openrouter's
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'or-sonnet'  # openrouter's
    # The default's (ollama's) tiers must NOT leak into the override's env.
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] != 'glm'
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] != 'kimi'


# --- subagent opt-in force --------------------------------------------------

def test_subagent_explicit_is_forced():
    """An explicitly-assigned Subagent tier model is emitted (opt-in force)."""
    s = Settings(providers=_provider(models=['a', 'b']), model_default='ollama',
                 tier_models={'ollama': {'subagent': 'a'}})
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_SUBAGENT_MODEL'] == 'a'


def test_subagent_unset_is_scrubbed_from_parent_env():
    """When the Subagent tier is unset, any inherited CLAUDE_CODE_SUBAGENT_MODEL
    (e.g. from a claude-ollama launcher) is scrubbed — no forced subagent."""
    os.environ['CLAUDE_CODE_SUBAGENT_MODEL'] = 'inherited-glm'
    try:
        s = Settings(providers=_provider(models=['a']), model_default='ollama')
        env, _ = build_spawn_env(s, '/p')
        assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in env
    finally:
        del os.environ['CLAUDE_CODE_SUBAGENT_MODEL']


def test_subagent_stale_value_is_omitted():
    """A stale Subagent tier value (model no longer on the active provider) is
    treated as unset → not forced, and any inherited value is scrubbed."""
    os.environ['CLAUDE_CODE_SUBAGENT_MODEL'] = 'inherited'
    try:
        s = Settings(providers=_provider(models=['a']), model_default='ollama',
                     tier_models={'ollama': {'subagent': 'gone'}})
        env, _ = build_spawn_env(s, '/p')
        assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in env
    finally:
        del os.environ['CLAUDE_CODE_SUBAGENT_MODEL']


# --- [1m] is stored on the model id (UI toggle); spawn is verbatim -----------

def test_tier_model_ids_emitted_verbatim_including_1m():
    """Spawn no longer name-matches glm|deepseek; [1m] must already be stored."""
    s = Settings(providers=_provider(models=['glm-5.2:cloud[1m]', 'other']),
                 model_default='ollama',
                 tier_models={'ollama': {
                     'opus': 'glm-5.2:cloud[1m]',
                     'sonnet': 'glm-5.2:cloud[1m]',
                     'haiku': 'other',
                 }})
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'glm-5.2:cloud[1m]'
    # Same stored id on Sonnet also keeps [1m] (no tier-specific rewrite).
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'glm-5.2:cloud[1m]'
    assert env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'other'


def test_bare_model_id_not_auto_suffixed_at_spawn():
    """Without the stored [1m] flag, spawn leaves the id bare (migration may
    have rewritten load-time settings; in-memory fixtures stay as given)."""
    s = Settings(providers=_provider(models=['qwen-max', 'some-model']),
                 model_default='ollama',
                 tier_models={'ollama': {'opus': 'qwen-max', 'sonnet': 'some-model'}})
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'qwen-max'
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'some-model'


# --- CLAUDE_CODE_MAX_CONTEXT_TOKENS (per custom provider) --------------------

def test_max_context_tokens_injected_when_set():
    prov = _provider()
    prov['ollama']['max_context_tokens'] = 200000
    s = Settings(providers=prov, model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    assert env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] == '200000'


def test_max_context_tokens_omitted_when_unset():
    s = Settings(providers=_provider(), model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    assert 'CLAUDE_CODE_MAX_CONTEXT_TOKENS' not in env


def test_max_context_tokens_scrubs_inherited_parent_env():
    os.environ['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] = '999'
    try:
        s = Settings(providers=_provider(), model_default='ollama')
        env, _ = build_spawn_env(s, '/p')
        assert 'CLAUDE_CODE_MAX_CONTEXT_TOKENS' not in env
    finally:
        del os.environ['CLAUDE_CODE_MAX_CONTEXT_TOKENS']


def test_native_still_injects_nothing_including_max_context():
    s = Settings()  # Anthropic native
    env, reason = build_spawn_env(s, '/p')
    assert env is None
    assert reason is None


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


def test_all_tiers_canonical():
    assert set(TIERS) == {'opus', 'sonnet', 'haiku', 'subagent', 'fable'}


def test_fable_tier_env_emitted():
    """The Fable tier is wired like the others: build_spawn_env emits
    ANTHROPIC_DEFAULT_FABLE_MODEL. Unset → the provider's first model."""
    s = Settings(providers=_provider(models=['glm-5.2:cloud[1m]', 'kimi']),
                 model_default='ollama')
    env, _ = build_spawn_env(s, '/p')
    assert env['ANTHROPIC_DEFAULT_FABLE_MODEL'] == 'glm-5.2:cloud[1m]'
    # Explicit Fable assignment is honored verbatim.
    s2 = Settings(providers=_provider(models=['glm-5.2:cloud[1m]', 'kimi']),
                  model_default='ollama', tier_models={'ollama': {'fable': 'kimi'}})
    e2, _ = build_spawn_env(s2, '/p')
    assert e2['ANTHROPIC_DEFAULT_FABLE_MODEL'] == 'kimi'