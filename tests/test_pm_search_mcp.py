"""Tests for pm_search_mcp — the provider web_search MCP bridge.

Pins the JSON-RPC handshake shape (mirroring paa_mcp), the request the
server makes (body/headers, key never logged), result formatting + trimming,
and every error path (empty, HTTP 4xx, network, missing search_url).
"""
import io
import json
import os

import pytest

import settings as settings_mod
import pm_search_mcp as mcp


_PROVIDERS = {
    'searchy': {
        'name': 'Searchy', 'base_url': 'https://api.example.com/coding/',
        'api_key': 'test-key-abc', 'models': ['m1'],
        'search_url': 'https://api.example.com/coding/v1/search',
    },
    'nosearch': {
        'name': 'NoSearch', 'base_url': 'https://api.example.com/',
        'api_key': 'k', 'models': ['m1'],
    },
}


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'providers': _PROVIDERS}))
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH', str(path))
    return path


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return json.dumps(self._payload).encode()


@pytest.fixture
def capture_request(monkeypatch):
    """Capture the outgoing urllib request; return (calls, box). ``box``
    carries ``payload`` (set to shape the reply) and ``resps`` (the
    _FakeResp instances the server read from)."""
    calls = []
    box = {'payload': {'search_results': []}, 'resps': []}

    def fake_urlopen(req, timeout=None):
        calls.append({'url': req.full_url,
                      'body': json.loads(req.data.decode()),
                      'auth': req.get_header('Authorization'),
                      'timeout': timeout})
        resp = _FakeResp(box['payload'])
        box['resps'].append(resp)
        return resp

    monkeypatch.setattr(mcp.urllib.request, 'urlopen', fake_urlopen)
    return calls, box


# --- handshake / tool shape ---------------------------------------------------

def test_initialize_and_tools_list():
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-03-26'}})
    assert reply['result']['protocolVersion'] == '2025-03-26'
    assert reply['result']['serverInfo']['name'] == 'pm-search'
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    tools = reply['result']['tools']
    assert [t['name'] for t in tools] == ['web_search']
    # readOnlyHint: grok's read-only capability gate allows dispatch only
    # for MCP tools the server declared read-only.
    assert tools[0]['annotations']['readOnlyHint'] is True
    props = tools[0]['inputSchema']['properties']
    assert 'query' in props and 'limit' in props
    assert session.handle_message(
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
    bad = session.handle_message(
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'nope', 'arguments': {}}})
    assert bad['error']['code'] == -32602


def test_serve_ndjson_over_streams():
    stdin = io.BytesIO(
        (json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}) + '\n'
         + json.dumps({'jsonrpc': '2.0', 'method': 'notifications/x'}) + '\n'
         + 'not json\n').encode())
    stdout = io.BytesIO()
    rc = mcp.serve('searchy', stream_in=stdin, stream_out=stdout)
    assert rc == 0
    replies = [json.loads(l) for l in stdout.getvalue().decode().splitlines()]
    assert len(replies) == 1 and replies[0]['result'] == {}


# --- the search call ----------------------------------------------------------

def test_call_posts_catalog_endpoint_with_bearer(settings_file, capture_request):
    calls, box = capture_request
    box['payload'] = {'search_results': [
        {'title': 'Fedora', 'url': 'https://example.com/f',
         'site_name': 'example', 'date': '2026-10-01',
         'snippet': 'A linux distribution.',
         'content': 'x' * 5000},
    ]}
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'latest fedora', 'limit': 3}}})
    assert reply['result']['isError'] is False
    assert calls[0]['url'] == 'https://api.example.com/coding/v1/search'
    assert calls[0]['body'] == {'text_query': 'latest fedora', 'limit': 3}
    assert calls[0]['auth'] == 'Bearer test-key-abc'
    text = reply['result']['content'][0]['text']
    assert text.startswith('# Fedora\nhttps://example.com/f '
                           '(example, 2026-10-01)')
    assert 'A linux distribution.' in text
    # content trimmed to ~1500 + clip note
    assert 'x' * 1501 not in text
    assert 'more chars]' in text


def test_limit_clamped_and_defaults(settings_file, capture_request):
    calls, _box = capture_request
    session = mcp.SearchSession('searchy')
    session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q'}}})
    assert calls[-1]['body']['limit'] == 5            # default
    session.handle_message(
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q', 'limit': 99}}})
    assert calls[-1]['body']['limit'] == 10           # MAX_LIMIT


def test_empty_results(settings_file, capture_request):
    _calls, box = capture_request
    box['payload'] = {'search_results': []}
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'nothing'}}})
    assert reply['result']['isError'] is False
    assert reply['result']['content'][0]['text'] == 'No results.'


def test_http_error_is_one_line(settings_file, capture_request, monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'unauthorized',
                                     {}, None)
    monkeypatch.setattr(mcp.urllib.request, 'urlopen', boom)
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q'}}})
    assert reply['result']['isError'] is True
    assert 'HTTP 401' in reply['result']['content'][0]['text']


def test_network_error_is_one_line(settings_file, monkeypatch):
    def boom(req, timeout=None):
        raise TimeoutError('timed out')
    monkeypatch.setattr(mcp.urllib.request, 'urlopen', boom)
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q'}}})
    assert reply['result']['isError'] is True
    assert 'TimeoutError' in reply['result']['content'][0]['text']


def test_missing_search_url_responds_not_configured(settings_file,
                                                    capture_request):
    calls, _box = capture_request          # must not even fire a request
    session = mcp.SearchSession('nosearch')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q'}}})
    assert reply['result']['isError'] is False
    assert 'not configured' in reply['result']['content'][0]['text']
    assert calls == []


def test_total_output_capped(settings_file, capture_request):
    _calls, box = capture_request
    box['payload'] = {'search_results': [
        {'title': f'R{i}', 'url': f'https://e.com/{i}',
         'content': 'c' * 2000} for i in range(20)
    ]}
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q', 'limit': 10}}})
    text = reply['result']['content'][0]['text']
    assert len(text) <= mcp.OUTPUT_MAX + 64
    assert 'more chars]' in text


def test_key_never_logged(settings_file, capture_request, caplog):
    import logging
    calls, box = capture_request
    box['payload'] = {'search_results': []}
    session = mcp.SearchSession('searchy')
    with caplog.at_level(logging.DEBUG, logger='pm_search_mcp'):
        session.handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': 'web_search',
                        'arguments': {'query': 'q'}}})
    assert 'test-key-abc' not in caplog.text


def test_non_http_scheme_rejected_without_request(settings_file,
                                                  capture_request, tmp_path,
                                                  monkeypatch):
    """Point-of-use trust boundary: a hand-edited non-http(s) search_url is
    refused without firing any request (UI validation alone is not enough)."""
    path = tmp_path / 'settings.json'
    data = {'providers': dict(_PROVIDERS)}
    data['providers']['searchy'] = dict(
        data['providers']['searchy'], search_url='ftp://evil.example/x')
    path.write_text(json.dumps(data))
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH', str(path))
    calls, _box = capture_request
    session = mcp.SearchSession('searchy')
    reply = session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q'}}})
    assert reply['result']['isError'] is True
    assert 'unsupported scheme' in reply['result']['content'][0]['text']
    assert calls == []


def test_response_read_is_size_bounded(settings_file, capture_request):
    _calls, box = capture_request
    box['payload'] = {'search_results': []}
    session = mcp.SearchSession('searchy')
    session.handle_message(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'web_search',
                    'arguments': {'query': 'q'}}})
    # The server must bound the body read (1 MiB) even though the formatted
    # reply caps at ~12k chars.
    assert box['resps'] and box['resps'][0].read_sizes == [1 << 20]
