import pytest
from models import (
    build_model_options, model_label, validate_providers,
    NATIVE_LABEL, FOLLOW_DEFAULT,
)


def _providers():
    return {
        'ollama': {
            'name': 'Ollama',
            'models': {'qwen': {'name': 'Qwen'}, 'gemma': {'name': 'Gemma'}},
        },
        'mistral': {
            'name': 'Mistral AI',
            'models': {'large': {'name': 'Mistral Large'}},
        },
    }


def test_build_model_options_native_first():
    ids, labels = build_model_options({})
    assert ids == ['']
    assert labels == [NATIVE_LABEL]


def test_build_model_options_lists_all_models():
    ids, labels = build_model_options(_providers())
    assert ids[0] == ''
    assert set(ids[1:]) == {'ollama/qwen', 'ollama/gemma', 'mistral/large'}
    # parallel lists stay aligned
    assert len(ids) == len(labels)
    assert labels[ids.index('mistral/large')] == 'Mistral AI — Mistral Large'


def test_build_model_options_sorted_and_stable():
    ids, _ = build_model_options(_providers())
    # native sentinel first, then providers+models in sorted order
    assert ids == ['', 'mistral/large', 'ollama/gemma', 'ollama/qwen']


def test_build_model_options_malformed_degrades():
    # not a dict, provider not a dict, models not a dict — none should raise
    assert build_model_options(None) == ([''], [NATIVE_LABEL])
    assert build_model_options({'bad': 'x'}) == ([''], [NATIVE_LABEL])
    assert build_model_options({'p': {'models': 'x'}}) == ([''], [NATIVE_LABEL])


def test_model_label():
    p = _providers()
    assert model_label(p, '') == NATIVE_LABEL
    assert model_label(p, 'ollama/qwen') == 'Ollama — Qwen'
    # stale id falls back to the raw string
    assert model_label(p, 'gone/x') == 'gone/x'


def test_validate_providers_accepts_good_shapes():
    assert validate_providers({}) == {}
    validate_providers(_providers())  # no raise


def test_validate_providers_rejects_bad_shapes():
    with pytest.raises(ValueError):
        validate_providers([])
    with pytest.raises(ValueError):
        validate_providers({'p': 'not-an-object'})
    with pytest.raises(ValueError):
        validate_providers({'p': {'models': 'not-an-object'}})
    with pytest.raises(ValueError):
        validate_providers({'p': {'models': {'m': 'not-an-object'}}})


def test_validate_providers_allows_partial_provider():
    # missing base_url / api_key is fine — user may save work in progress
    validate_providers({'p': {'name': 'P'}})


def test_follow_default_sentinel_is_not_a_model_id():
    # FOLLOW_DEFAULT must never collide with '' or a 'provider/model' id
    assert FOLLOW_DEFAULT != ''
    assert '/' not in FOLLOW_DEFAULT
