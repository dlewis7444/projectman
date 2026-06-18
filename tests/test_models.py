import pytest
from settings import Settings, TIERS
from models import (
    build_provider_options, provider_label, build_tier_options,
    resolve_tier_model, validate_providers,
    NATIVE_LABEL, TIER_DEFAULT_LABEL, FOLLOW_DEFAULT,
)


def _providers():
    return {
        'ollama': {
            'name': 'Ollama',
            'base_url': 'http://localhost:11434',
            'api_key': 'k',
            'models': ['qwen', 'gemma'],
        },
        'mistral': {
            'name': 'Mistral AI',
            'base_url': 'http://mistral',
            'api_key': 'k',
            'models': ['large'],
        },
    }


# --- build_provider_options -------------------------------------------------

def test_build_provider_options_native_first():
    ids, labels = build_provider_options({})
    assert ids == ['']
    assert labels == [NATIVE_LABEL]


def test_build_provider_options_lists_providers():
    ids, labels = build_provider_options(_providers())
    assert ids[0] == ''
    assert set(ids[1:]) == {'ollama', 'mistral'}
    assert len(ids) == len(labels)
    assert labels[ids.index('mistral')] == 'Mistral AI'


def test_build_provider_options_sorted_and_stable():
    ids, _ = build_provider_options(_providers())
    # native sentinel first, then provider ids sorted
    assert ids == ['', 'mistral', 'ollama']


def test_build_provider_options_malformed_degrades():
    # not a dict, provider not a dict — none should raise
    assert build_provider_options(None) == ([''], [NATIVE_LABEL])
    assert build_provider_options({'bad': 'x'}) == ([''], [NATIVE_LABEL])


def test_build_provider_options_name_falls_back_to_id():
    ids, labels = build_provider_options({'p': {'models': []}})
    assert labels == [NATIVE_LABEL, 'p']


# --- provider_label ---------------------------------------------------------

def test_provider_label():
    p = _providers()
    assert provider_label(p, '') == NATIVE_LABEL
    assert provider_label(p, 'ollama') == 'Ollama'
    # stale id falls back to the raw string
    assert provider_label(p, 'gone') == 'gone'
    assert provider_label(None, 'ollama') == 'ollama'


# --- build_tier_options -----------------------------------------------------

def test_build_tier_options_default_first():
    ids, labels = build_tier_options(_providers(), 'ollama')
    assert ids == ['', 'qwen', 'gemma']
    assert labels == [TIER_DEFAULT_LABEL, 'qwen', 'gemma']


def test_build_tier_options_unknown_provider_just_default():
    ids, labels = build_tier_options(_providers(), 'nope')
    assert ids == ['']
    assert labels == [TIER_DEFAULT_LABEL]


def test_build_tier_options_native_no_provider():
    ids, labels = build_tier_options(_providers(), '')
    assert ids == ['']
    assert labels == [TIER_DEFAULT_LABEL]


# --- resolve_tier_model -----------------------------------------------------

def test_resolve_tier_model_explicit_value_used():
    s = Settings(providers={'p': {'models': ['a', 'b']}},
                 model_default='p', tier_models={'opus': 'b'})
    assert resolve_tier_model(s, 'p', 'opus') == 'b'


def test_resolve_tier_model_empty_falls_to_first_model():
    s = Settings(providers={'p': {'models': ['a', 'b']}},
                 model_default='p', tier_models={'opus': ''})
    assert resolve_tier_model(s, 'p', 'opus') == 'a'


def test_resolve_tier_model_stale_value_falls_to_first():
    # tier value not on the active provider → first model (defensive fallback)
    s = Settings(providers={'p': {'models': ['a', 'b']}},
                 model_default='p', tier_models={'opus': 'gone'})
    assert resolve_tier_model(s, 'p', 'opus') == 'a'


def test_resolve_tier_model_no_models_empty():
    s = Settings(providers={'p': {'models': []}}, model_default='p')
    assert resolve_tier_model(s, 'p', 'opus') == ''


# --- validate_providers -----------------------------------------------------

def test_validate_providers_accepts_good_shapes():
    assert validate_providers({}) == {}
    validate_providers(_providers())  # no raise


def test_validate_providers_rejects_bad_shapes():
    with pytest.raises(ValueError):
        validate_providers([])
    with pytest.raises(ValueError):
        validate_providers({'p': 'not-an-object'})
    with pytest.raises(ValueError):
        validate_providers({'p': {'models': 'not-a-list'}})
    with pytest.raises(ValueError):
        validate_providers({'p': {'models': [123]}})  # non-string model id


def test_validate_providers_allows_partial_provider():
    # missing base_url / api_key is fine — user may save work in progress
    validate_providers({'p': {'name': 'P'}})


# --- sentinels ---------------------------------------------------------------

def test_follow_default_sentinel_is_not_a_provider_id():
    # FOLLOW_DEFAULT must never collide with '' or a provider id
    assert FOLLOW_DEFAULT != ''
    assert FOLLOW_DEFAULT not in TIERS