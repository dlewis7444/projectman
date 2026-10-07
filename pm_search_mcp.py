"""ProjectMan-owned MCP bridge: provider web search for custom-provider sessions.

Grok's built-in ``web_search`` is disabled on custom providers (the hosted
tool POSTs ``{base}/responses`` with hardcoded sampling and is broken on any
third-party endpoint), so this stdio MCP server is the replacement path: a
thin ``web_search`` tool that POSTs the PROVIDER's own search endpoint
(``providers[pid].search_url`` from ``~/.ProjectMan/settings.json``) and
formats compact markdown. The provider-specific part is catalog data, not
code — any provider that offers a ``POST {search_url}`` taking
``{"text_query", "limit"}`` and answering ``{"search_results": [...]}`` gets
the tool for free (the maintainer's universality directive).

Security shape mirrors paa_mcp.py: NO key material anywhere new — the server
reads the 0600 settings.json at call time and nothing else; the managed-home
registration carries only the provider id. Results are trimmed (per-result
content ~1500 chars, total ~12k) so huge page extractions cannot blow the
MCP tool-result budget. HTTP/network failures return one-line error text
(isError) and never crash the tool call.

The server speaks NDJSON JSON-RPC over stdio (``initialize``,
``notifications/initialized``, ``tools/list``, ``tools/call``, ``ping``);
``initialize`` echoes the client's ``protocolVersion`` (the paa-shell live
capture proved that is the compatible negotiation). Spawned by grok as
``python3 <abspath>/pm_search_mcp.py --provider <pid>``; the script lives in
the repo root so ``import settings`` resolves exactly like the app itself.

Reachability caveat (grok 1.0.46, probe-verified 2026-10-06): main sessions
and ``spawn_subagent`` children inherit MCP tools, but workflow ``agent()``
children only see MCP tools under ``capability_mode`` ``"execute"``/``"all"``
— ``"read-only"`` strips every MCP tool (servers show "connected" but
toolless). Workflow scripts whose agents need web_search must use
``"execute"``; this is grok-side, not fixable from config.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

log = logging.getLogger('pm_search_mcp')

SERVER_NAME = 'pm-search'
TOOL_NAME = 'web_search'
SERVER_VERSION = '1.0.0'
DEFAULT_PROTOCOL_VERSION = '2025-03-26'

CONTENT_TRIM = 1500          # per-result content excerpt
OUTPUT_MAX = 12 * 1024       # whole-tool reply cap
SEARCH_TIMEOUT_SEC = 30
MAX_LIMIT = 10

TOOL_DESCRIPTION = (
    'Search the web via this project\'s custom-provider search endpoint. '
    'Replaces the built-in web_search tool (unavailable on custom '
    'providers). Returns a compact markdown list: title, url, site/date, '
    'snippet, and a trimmed content excerpt per result.'
)


def server_file_path() -> str:
    """Absolute path to this tree's ``pm_search_mcp.py`` (the spawned server)."""
    return os.path.abspath(__file__)


def tool_descriptor() -> dict:
    return {
        'name': TOOL_NAME,
        'description': TOOL_DESCRIPTION,
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'The search query text.',
                },
                'limit': {
                    'type': 'integer',
                    'description': 'Maximum results (default 5, max 10).',
                    'default': 5,
                },
            },
            'required': ['query'],
        },
        # Declares the tool read-only so capability-gated harnesses (grok
        # read-only subagents via the pm/read-only-mcp patch) allow dispatch.
        'annotations': {'readOnlyHint': True, 'openWorldHint': True},
    }


def _settings_path() -> str:
    """Same resolution settings.py uses (the repo is on sys.path — the
    script's own directory — exactly like the app)."""
    try:
        from settings import DEFAULT_SETTINGS_PATH
        return DEFAULT_SETTINGS_PATH
    except Exception:
        return os.path.join(os.path.expanduser('~'), '.ProjectMan',
                            'settings.json')


def _load_provider(provider_id: str) -> dict | None:
    try:
        with open(_settings_path(), 'r') as f:
            data = json.load(f)
        prov = (data.get('providers') or {}).get(provider_id)
        return prov if isinstance(prov, dict) else None
    except Exception as exc:
        log.debug('pm-search: settings load failed: %s', exc)
        return None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n…[{len(text) - limit} more chars]'


def _format_results(results) -> str:
    out = []
    total = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        title = str(r.get('title') or '')
        link = str(r.get('url') or '')
        meta = ', '.join(str(x) for x in (r.get('site_name'), r.get('date'))
                         if x)
        snippet = str(r.get('snippet') or '')
        content = _clip(str(r.get('content') or ''), CONTENT_TRIM)
        block = f'# {title}\n{link}' + (f' ({meta})' if meta else '')
        if snippet:
            block += f'\n{snippet}'
        if content:
            block += f'\n{content}'
        out.append(block)
        total += len(block) + 2
        if total > OUTPUT_MAX:
            break
    return '\n\n'.join(out)


def run_web_search(provider_id: str, query: str, limit: int = 5) -> tuple[str, bool]:
    """One search call. Returns ``(text, is_error)`` — the tool call NEVER
    raises; failures are one-line text. No key material is ever logged."""
    prov = _load_provider(provider_id)
    if prov is None:
        return (f'provider "{provider_id}" is not configured in ProjectMan.',
                True)
    url = (prov.get('search_url') or '').strip()
    if not url:
        return ('web search is not configured for this provider '
                '(Settings → Models → provider → Search endpoint).', False)
    import urllib.parse
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        # Trust boundary: the editor validates, but settings.json can be
        # hand-edited — never fire a request at a non-http(s) endpoint.
        return (f'search error: unsupported scheme in provider search_url '
                f'({urllib.parse.urlsplit(url).scheme!r}).', True)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, MAX_LIMIT))
    body = json.dumps({'text_query': query, 'limit': limit}).encode('utf-8')
    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {prov.get("api_key") or ""}'})
    try:
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT_SEC) as resp:
            # Size-bounded read: the formatted reply caps at ~12k chars
            # anyway; never buffer an unbounded provider response.
            payload = json.loads(resp.read(1 << 20).decode('utf-8', 'replace'))
    except urllib.error.HTTPError as exc:
        return (f'search error: HTTP {exc.code} from provider endpoint.',
                True)
    except Exception as exc:  # noqa: BLE001 — network errors are tool text
        log.debug('pm-search: request failed: %s', type(exc).__name__)
        return (f'search error: {type(exc).__name__}.', True)
    results = payload.get('search_results') if isinstance(payload, dict) \
        else None
    if not results:
        return ('No results.', False)
    return (_clip(_format_results(results), OUTPUT_MAX), False)


def _result(msg_id, payload: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': payload}


def _error(msg_id, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id,
            'error': {'code': code, 'message': message}}


def _text_result(text: str, *, is_error: bool) -> dict:
    return {
        'content': [{'type': 'text', 'text': _clip(text, OUTPUT_MAX)}],
        'isError': is_error,
    }


class SearchSession:
    """Bound to one provider id (the ``--provider`` argv)."""

    def __init__(self, provider_id: str):
        self.provider_id = provider_id

    def handle_message(self, msg) -> dict | None:
        if not isinstance(msg, dict):
            return _error(None, -32600, 'request is not a JSON object')
        method = msg.get('method')
        msg_id = msg.get('id')
        if not isinstance(method, str):
            return _error(msg_id, -32600, 'missing method')
        if method.startswith('notifications/'):
            return None
        params = msg.get('params') or {}

        if method == 'initialize':
            version = params.get('protocolVersion') or DEFAULT_PROTOCOL_VERSION
            return _result(msg_id, {
                'protocolVersion': version,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            })
        if method == 'ping':
            return _result(msg_id, {})
        if method == 'tools/list':
            return _result(msg_id, {'tools': [tool_descriptor()]})
        if method == 'tools/call':
            name = params.get('name')
            if name != TOOL_NAME:
                return _error(msg_id, -32602, f'unknown tool: {name!r}')
            args = params.get('arguments') or {}
            if not isinstance(args, dict):
                return _error(msg_id, -32602, 'arguments is not an object')
            query = args.get('query')
            if not isinstance(query, str) or not query.strip():
                return _error(msg_id, -32602, 'query is required')
            try:
                text, is_error = run_web_search(
                    self.provider_id, query, args.get('limit', 5))
            except Exception as exc:  # noqa: BLE001 — never wedge the pipe
                log.exception('pm-search: handler crashed')
                return _result(msg_id, _text_result(
                    f'search error: {type(exc).__name__}.', is_error=True))
            return _result(msg_id, _text_result(text, is_error=is_error))
        return _error(msg_id, -32601, f'method not found: {method}')


def serve(provider_id: str, stream_in=None, stream_out=None) -> int:
    """NDJSON JSON-RPC loop over stdio (mirrors paa_mcp.serve)."""
    session = SearchSession(provider_id)
    stream_in = stream_in or sys.stdin.buffer
    stream_out = stream_out or sys.stdout.buffer
    while True:
        line = stream_in.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode('utf-8', 'replace'))
        except ValueError as exc:
            log.debug('pm-search: ignored malformed line: %s', exc)
            continue
        response = session.handle_message(msg)
        if response is None:
            continue
        try:
            stream_out.write((json.dumps(response) + '\n').encode('utf-8'))
            stream_out.flush()
        except (OSError, ValueError):
            return 0


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True,
                        help='Settings provider id whose search_url/api_key '
                             'this server serves.')
    args = parser.parse_args(argv)
    return serve(args.provider)


if __name__ == '__main__':
    sys.exit(main())
