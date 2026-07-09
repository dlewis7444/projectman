"""Unit tests for the provider model-reachability probe (models.py).

``list_provider_models`` is the advisory ping the provider editor runs (off the
main loop) to mark an added model green/amber. It tries an Anthropic-compatible
``/v1/models`` first, then Ollama's ``/api/tags``, normalizes a trailing
``[1m]``, and returns ``None`` on any failure. These mock ``urllib.urlopen`` so
the probe is exercised without a live endpoint.
"""
import io

from models import list_provider_models, normalize_model_id


class _FakeResp:
    """A stand-in for the object urllib.request.urlopen returns: a context
    manager whose read() yields the given bytes."""
    def __init__(self, payload_bytes):
        self._buf = io.BytesIO(payload_bytes)

    def __enter__(self):
        return self._buf

    def __exit__(self, *exc):
        return False


def _patch_open(monkeypatch, dispatch):
    """Install a fake urlopen whose behavior is chosen by the URL.

    ``dispatch`` maps a URL substring → either bytes (a 200 response body) or an
    Exception instance/class to raise. A URL matching no key raises AssertionError
    so a misrouted call is loud rather than silent.
    """
    def fake_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        for needle, outcome in dispatch.items():
            if needle in url:
                if isinstance(outcome, type) and issubclass(outcome, BaseException):
                    raise outcome(needle)
                if isinstance(outcome, BaseException):
                    raise outcome
                return _FakeResp(outcome)
        raise AssertionError(f'unexpected urlopen URL: {url}')

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)


# --- normalize_model_id / 1m helpers -----------------------------------------

def test_normalize_model_id_strips_1m():
    assert normalize_model_id('glm-5.2:cloud[1m]') == 'glm-5.2:cloud'


def test_normalize_model_id_passes_through_non_1m():
    assert normalize_model_id('claude-opus-4-8') == 'claude-opus-4-8'
    assert normalize_model_id('') == ''


def test_1m_suffix_helpers():
    from models import is_1m_model_id, with_1m_suffix, without_1m_suffix
    assert is_1m_model_id('m[1m]') is True
    assert is_1m_model_id('m') is False
    assert with_1m_suffix('m') == 'm[1m]'
    assert with_1m_suffix('m[1m]') == 'm[1m]'
    assert without_1m_suffix('m[1m]') == 'm'
    assert without_1m_suffix('m') == 'm'


# --- /v1/models (Anthropic-compatible) ---------------------------------------

def test_v1_models_anthropic(monkeypatch):
    body = b'{"data":[{"id":"claude-opus-4-8"},{"id":"claude-sonnet-4-6"}]}'
    _patch_open(monkeypatch, {'/v1/models': body})
    offered = list_provider_models(
        {'base_url': 'http://x', 'api_key': 'k', 'models': []})
    assert offered == {'claude-opus-4-8', 'claude-sonnet-4-6'}


# --- /api/tags (Ollama) fallback ---------------------------------------------

def test_api_tags_fallback_when_v1_models_fails(monkeypatch):
    from urllib.error import URLError
    _patch_open(monkeypatch,
                {'/v1/models': URLError, '/api/tags': b'{"models":[{"name":"glm-5.2:cloud"}]}'})
    offered = list_provider_models(
        {'base_url': 'http://localhost:11434', 'api_key': '', 'models': []})
    assert offered == {'glm-5.2:cloud'}


def test_api_tags_strips_1m_from_returned_names(monkeypatch):
    from urllib.error import URLError
    _patch_open(monkeypatch,
                {'/v1/models': URLError,
                 '/api/tags': b'{"models":[{"name":"glm-5.2:cloud[1m]"}]}'})
    offered = list_provider_models(
        {'base_url': 'http://x', 'api_key': '', 'models': []})
    # Normalized so a probe comparing the user's 'glm-5.2:cloud[1m]' doesn't
    # false-negative.
    assert offered == {'glm-5.2:cloud'}
    assert 'glm-5.2:cloud[1m]' not in offered


# --- failure → None ----------------------------------------------------------

def test_both_endpoints_fail_returns_none(monkeypatch):
    from urllib.error import URLError
    _patch_open(monkeypatch, {'/v1/models': URLError, '/api/tags': URLError})
    assert list_provider_models(
        {'base_url': 'http://x', 'api_key': '', 'models': []}) is None


def test_v1_models_empty_data_falls_through_to_tags(monkeypatch):
    from urllib.error import URLError
    # /v1/models returns a valid but empty data list → not a hit → fall through.
    _patch_open(monkeypatch,
                {'/v1/models': b'{"data":[]}',
                 '/api/tags': b'{"models":[{"name":"kimi"}]}'})
    offered = list_provider_models(
        {'base_url': 'http://x', 'api_key': '', 'models': []})
    assert offered == {'kimi'}


# --- bad provider shape → None (no network attempted) ------------------------

def test_missing_base_url_returns_none(monkeypatch):
    # If a request were made, AssertionError would fire — proving none was.
    def fake_urlopen(*a, **k):
        raise AssertionError('urlopen should not be called')
    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    assert list_provider_models({'base_url': '', 'api_key': ''}) is None
    assert list_provider_models(None) is None
    assert list_provider_models('not a dict') is None