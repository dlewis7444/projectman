"""PAA-owned MCP server: the ONLY mutation channel for rw bot turns.

Grok does not fire PreToolUse hooks in headless ``grok -p`` mode (probe-
proven 2026-10-01 against grok 1.0.44), so the old hook-based approval gate
is dead. Enforcement is now: built-in mutators stay blocked by the gate
argv's deny rules (``--deny Edit --deny Write --deny Bash``), and the model
reaches the host ONLY through this stdio MCP server. Every ``run_command``
call is vetted by the :class:`~paa_approve.ApprovalBroker`'s automated
reviewer; the verdict is final (reviewer-final mode, the maintainer 2026-10-01).

The server speaks NDJSON JSON-RPC over stdio (``initialize``,
``notifications/initialized``, ``tools/list``, ``tools/call``, ``ping``).
Protocol note: ``initialize`` echoes the client's ``protocolVersion`` — the
2026-10-01 live capture of grok's handshake is the authority on what grok
sends, and echoing is the compatible negotiation.

Inertness guard: unless BOTH ``PAA_APPROVAL_SOCK`` and ``PAA_APPROVAL_TOKEN``
are set in the spawned process's environment AND the socket connects,
``tools/list`` returns an EMPTY list. Interactive grok sessions and ro bot
turns never see the tool; the ro argv keeps ``--deny MCPTool`` anyway as
belt-and-suspenders. The broker only accepts reviews bearing the active
turn's token, so a bare env leak is not enough to arm the channel.

This file is spawned directly by grok (``python3 <abspath>/paa_mcp.py``);
the server half deliberately imports nothing from the repo so a broken
bot tree cannot break the channel. The install helpers at the bottom
(``ensure_mcp_config`` / ``ensure_trusted_folder``) run inside the bot's
own interpreter at serve() time.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time

log = logging.getLogger('paa_mcp')

SERVER_NAME = 'paa-shell'
TOOL_NAME = 'run_command'
SERVER_VERSION = '1.0.0'
DEFAULT_PROTOCOL_VERSION = '2025-03-26'
OUTPUT_MAX = 15 * 1024
DEFAULT_DEADLINE_SEC = 120
CONNECT_TIMEOUT_SEC = 2.0

ENV_SOCK = 'PAA_APPROVAL_SOCK'
ENV_TOKEN = 'PAA_APPROVAL_TOKEN'
ENV_DEADLINE = 'PAA_MCP_DEADLINE_SEC'

MCP_CONFIG_STARTUP_SEC = 15
MCP_TOOL_TIMEOUT_SEC = 180

TOOL_DESCRIPTION = (
    'Run a shell command on the PAA host after automated review. Built-in '
    'shell/file tools are blocked by policy in this channel; this is the '
    'only way to execute commands. Every call is vetted by an automated '
    'reviewer against the user\'s original request, and its verdict is '
    'final: the command either runs exactly as given or is denied.'
)

REVIEW_UNAVAILABLE = 'review unavailable; command not run'


def server_file_path() -> str:
    """Absolute path to this tree's ``paa_mcp.py`` (the spawned server)."""
    return os.path.abspath(__file__)


def tool_descriptor() -> dict:
    """The single ``run_command`` tool exposed when the channel is live."""
    return {
        'name': TOOL_NAME,
        'description': TOOL_DESCRIPTION,
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'The exact shell command to run (bash -c).',
                },
                'reason': {
                    'type': 'string',
                    'description': ('Why this command is needed for the '
                                    'user\'s request. The reviewer compares '
                                    'this against the original ask.'),
                },
            },
            'required': ['command', 'reason'],
        },
    }


# -- channel guard -------------------------------------------------------------


def _channel_ready() -> bool:
    """True only when the env halves are set AND the socket accepts a probe.

    The probe connection is empty-bytes; the broker's accept loop tolerates
    it (that is the documented liveness-probe pattern).
    """
    sock_path = os.environ.get(ENV_SOCK) or ''
    token = os.environ.get(ENV_TOKEN) or ''
    if not sock_path or not token:
        return False
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(CONNECT_TIMEOUT_SEC)
            probe.connect(sock_path)
        finally:
            probe.close()
    except OSError:
        return False
    return True


# -- broker round trip ----------------------------------------------------------


def _review_round_trip(command: str, reason: str) -> dict:
    """Send one review request to the broker and return its decision dict.

    Returns ``{'decision': 'allow', 'output': ...}`` or
    ``{'decision': 'deny', 'reason': ...}``. Raises on ANY transport
    failure, timeout, or malformed reply — the caller maps that to the
    fail-closed REVIEW-UNAVAILABLE answer. The whole exchange is bounded by
    ``PAA_MCP_DEADLINE_SEC`` (default 120s), which sits under grok's
    ``tool_timeout_sec`` (180s). The BROKER derives its exec timeout from
    the same deadline (review + exec + margin must fit inside it), so this
    guard never fires while a vetted command is still executing — the
    server cannot report "not run" for a command that then runs (M1a).
    """
    deadline = time.monotonic() + float(
        os.environ.get(ENV_DEADLINE) or DEFAULT_DEADLINE_SEC)
    sock_path = os.environ[ENV_SOCK]
    token = os.environ[ENV_TOKEN]
    request = {
        'kind': 'review',
        'token': token,
        'tool': TOOL_NAME,
        'command': command,
        'reason': reason,
    }
    raw = (json.dumps(request) + '\n').encode('utf-8')
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(max(0.1, deadline - time.monotonic()))
        conn.connect(sock_path)
        conn.sendall(raw)
        # Read exactly one reply line inside the remaining budget.
        conn.settimeout(max(0.1, deadline - time.monotonic()))
        buf = b''
        while b'\n' not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > (1 << 20):
                raise ValueError('broker reply oversized')
        line = buf.split(b'\n', 1)[0].strip()
        reply = json.loads(line.decode('utf-8', 'replace'))
        if not isinstance(reply, dict):
            raise ValueError('broker reply is not an object')
        decision = str(reply.get('decision') or '')
        if decision == 'allow':
            return {'decision': 'allow',
                    'output': str(reply.get('output') or '')}
        if decision == 'deny':
            return {'decision': 'deny',
                    'reason': str(reply.get('reason') or 'denied')}
        raise ValueError(f'unknown broker decision {decision!r}')
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _clip(text: str, limit: int = OUTPUT_MAX) -> str:
    text = str(text or '')
    if len(text) > limit:
        keep = max(0, limit - 3)
        text = text[:keep] + ('...' if keep else '')
    return text


# -- JSON-RPC plumbing -----------------------------------------------------------


def _result(msg_id, payload: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': payload}


def _error(msg_id, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id,
            'error': {'code': code, 'message': message}}


def _text_result(text: str, *, is_error: bool) -> dict:
    return {
        'content': [{'type': 'text', 'text': _clip(text)}],
        'isError': is_error,
    }


def handle_message(msg) -> dict | None:
    """Handle one decoded JSON-RPC message.

    Returns the response object to send, or ``None`` for notifications
    (``notifications/initialized`` and any other ``notifications/*``).
    Unknown methods get JSON-RPC -32601; a ``tools/call`` for a foreign
    tool name gets -32602. ``tools/call run_command`` performs the broker
    round trip and never executes anything locally — execution lives only
    in the broker process.
    """
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
        tools = [tool_descriptor()] if _channel_ready() else []
        return _result(msg_id, {'tools': tools})
    if method == 'tools/call':
        name = params.get('name')
        if name != TOOL_NAME:
            return _error(msg_id, -32602, f'unknown tool: {name!r}')
        args = params.get('arguments') or {}
        if not isinstance(args, dict):
            return _error(msg_id, -32602, 'arguments is not an object')
        command = args.get('command')
        reason = args.get('reason')
        if not isinstance(command, str) or not command.strip():
            return _error(msg_id, -32602, 'command is required')
        if not isinstance(reason, str) or not reason.strip():
            return _error(msg_id, -32602, 'reason is required')
        if not _channel_ready():
            # The tool should never have been listed; answer fail-closed
            # anyway so a stale catalog cannot arm a dead channel.
            return _result(msg_id, _text_result(REVIEW_UNAVAILABLE,
                                                is_error=True))
        try:
            reply = _review_round_trip(command, reason)
        except Exception as exc:  # noqa: BLE001 — any failure denies
            log.debug('paa mcp: review round trip failed: %s', exc)
            return _result(msg_id, _text_result(REVIEW_UNAVAILABLE,
                                                is_error=True))
        if reply['decision'] == 'allow':
            return _result(msg_id, _text_result(reply.get('output') or '',
                                                is_error=False))
        return _result(msg_id, _text_result(
            f"command denied by reviewer: {reply.get('reason') or 'denied'}",
            is_error=True))
    return _error(msg_id, -32601, f'method not found: {method}')


def serve(stream_in=None, stream_out=None) -> int:
    """NDJSON JSON-RPC loop over stdio. Returns the process exit code.

    One JSON object per line; a malformed line is ignored (logged at debug
    level on stderr, which grok captures to ``~/.grok/logs/mcp/<server>.stderr.
    log``) so a poisoned byte cannot wedge the session. Broken stdout (grok
    died) exits 0 — there is nothing left to talk to.
    """
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
            log.debug('paa mcp: ignored malformed line: %s', exc)
            continue
        try:
            response = handle_message(msg)
        except Exception as exc:  # noqa: BLE001 — never wedge the pipe
            log.exception('paa mcp: handler crashed')
            response = _error(
                msg.get('id') if isinstance(msg, dict) else None,
                -32603, f'internal error: {exc}')
        if response is None:
            continue
        try:
            stream_out.write((json.dumps(response) + '\n').encode('utf-8'))
            stream_out.flush()
        except (OSError, ValueError):
            return 0


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    return serve()


if __name__ == '__main__':
    sys.exit(main())


# -- serve()-time install helpers -------------------------------------------------
#
# Everything below runs inside the bot's own interpreter (paa_telegram.serve),
# never inside the spawned MCP server. Tests exercise these with redirected
# paths; no live config is touched outside serve().


def mcp_config_block(server_path: str | None = None,
                     python: str | None = None) -> str:
    """The exact TOML block this bot owns inside the PAA cwd's config.

    ``env`` passes the two gate halves through from grok's own environment
    (expanded by grok at config load, ``${VAR}`` form). When the variables
    are unset — interactive sessions, ro turns — they expand empty and the
    server is inert by design.
    """
    server_path = server_path or server_file_path()
    python = python or sys.executable
    return (
        f'[mcp_servers.{SERVER_NAME}]\n'
        f'command = "{python}"\n'
        f'args = ["{server_path}"]\n'
        f'env = {{ {ENV_SOCK} = "${{{ENV_SOCK}}}", '
        f'{ENV_TOKEN} = "${{{ENV_TOKEN}}}" }}\n'
        f'startup_timeout_sec = {MCP_CONFIG_STARTUP_SEC}\n'
        f'tool_timeout_sec = {MCP_TOOL_TIMEOUT_SEC}\n'
    )


def mcp_config_path(paa_cwd: str) -> str:
    return os.path.join(paa_cwd, '.grok', 'config.toml')


def _section_bounds(text: str, header_prefix: str, name: str):
    """Line range of ``[header_prefix."name"]`` ... before the next header.

    The name may be bare (``[mcp_servers.paa-shell]``) or quoted
    (``[folders."/some/path"]`` — grok's ``trusted_folders.toml`` form);
    without the quotes handled the lookup silently misses and an
    "idempotent" rewrite appends a duplicate table — which is a TOML parse
    error that kills the WHOLE file's consumers.
    """
    import re
    pattern = re.compile(
        rf'^[ \t]*\[{re.escape(header_prefix)}\.'
        rf'(?:"{re.escape(name)}"|{re.escape(name)})\][ \t]*$',
        re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        return None
    nxt = re.compile(r'^[ \t]*\[', re.MULTILINE).search(text, match.end())
    end = nxt.start() if nxt else len(text)
    return match.start(), end


def ensure_mcp_config(paa_cwd: str, *, server_path: str | None = None,
                      python: str | None = None) -> tuple:
    """Install/refresh ``.grok/config.toml`` in the PAA cwd; bot owns only
    the ``[mcp_servers.paa-shell]`` block.

    Merge-preserving: content outside our section (other servers, comments,
    unrelated tables) survives verbatim; our block is replaced wholesale
    when its text differs. Returns ``(path, changed)``. Atomic (tmp +
    os.replace).
    """
    block = mcp_config_block(server_path, python)
    os.makedirs(os.path.join(paa_cwd, '.grok'), exist_ok=True)
    path = mcp_config_path(paa_cwd)
    try:
        with open(path, 'r') as fh:
            current = fh.read()
    except OSError:
        current = None
    if current is not None:
        bounds = _section_bounds(current, 'mcp_servers', SERVER_NAME)
        if bounds is not None:
            new_text = current[:bounds[0]] + block + current[bounds[1]:]
        else:
            # Our server may already exist in inline form under
            # [mcp_servers] (``paa-shell = {...}``). Appending our section
            # on top would be a TOML duplicate-key error that kills the
            # WHOLE config, so strip a single-line inline definition
            # first; anything fancier we refuse to guess at (fail closed:
            # leave the file untouched, /status shows down).
            import re
            import tomllib
            try:
                parsed = tomllib.loads(current)
            except tomllib.TOMLDecodeError:
                parsed = {}
            servers = parsed.get('mcp_servers')
            if isinstance(servers, dict) and SERVER_NAME in servers:
                current, n = re.subn(
                    r'(?m)^[ \t]*' + re.escape(SERVER_NAME)
                    + r'[ \t]*=[^\n]*\n?', '', current, count=1)
                if n == 0:
                    log.warning(
                        'paa mcp: %s defines %s inline in a form we cannot '
                        'merge; leaving it untouched', path, SERVER_NAME)
                    return path, False
            new_text = current.rstrip('\n') + '\n\n' + block
    else:
        new_text = block
    # Never leave a config grok would reject: validate the merged text
    # before it hits disk. A bad merge disables EVERY project-scoped
    # server, not just ours — so on parse failure we write nothing.
    import tomllib
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        log.warning('paa mcp: merging %s would produce invalid TOML '
                    '(%s); leaving it untouched', path, exc)
        return path, False
    if new_text == current:
        return path, False
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        fh.write(new_text)
    os.replace(tmp, path)
    return path, True


def default_trusted_folders_path() -> str:
    return os.path.join(os.path.expanduser('~'), '.grok',
                        'trusted_folders.toml')


def ensure_trusted_folder(folder: str,
                          trusted_path: str | None = None) -> tuple:
    """Idempotently mark *folder* trusted in grok's ``trusted_folders.toml``.

    Repo-level grok config (``<cwd>/.grok/config.toml``) applies only once
    its folder is trusted, so serve() trusts the bot-owned PAA cwd the same
    way everything else here is installed: by content, never by hand.
    An existing entry for the folder is upgraded to ``trusted = true``; all
    other entries survive verbatim.

    The merged text is TOML-validated BEFORE ``os.replace`` (L1): on parse
    failure nothing is written, the pre-write bytes are left untouched, and
    the failure is logged + reported as ``changed=False``. Validation
    ahead of the atomic replace is stronger than post-write rollback —
    there is no window in which the store is broken. This also refuses to
    append to a store that was ALREADY broken: we leave it exactly as we
    found it rather than widening the damage. Returns ``(path, changed)``.
    """
    trusted_path = trusted_path or default_trusted_folders_path()
    folder = os.path.abspath(folder)
    quoted = folder.replace('\\', '\\\\').replace('"', '\\"')
    block = (f'[folders."{quoted}"]\n'
             'trusted = true\n'
             f'decided_at = {int(time.time())}\n')
    try:
        with open(trusted_path, 'r') as fh:
            current = fh.read()
    except OSError:
        current = None
    if current is not None:
        bounds = _section_bounds(current, 'folders', quoted)
        if bounds is not None:
            section = current[bounds[0]:bounds[1]]
            if 'trusted = true' in section:
                return trusted_path, False
            upgraded = section.replace('trusted = false', 'trusted = true')
            new_text = current[:bounds[0]] + upgraded + current[bounds[1]:]
        else:
            sep = '' if not current.strip() else (
                '' if current.endswith('\n') else '\n')
            new_text = current + sep + '\n' + block
    else:
        new_text = block
    if new_text == current:
        return trusted_path, False
    import tomllib
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        log.warning('paa mcp: updating %s would produce invalid TOML '
                    '(%s); pre-write bytes left untouched', trusted_path, exc)
        return trusted_path, False
    tmp = trusted_path + '.tmp'
    with open(tmp, 'w') as fh:
        fh.write(new_text)
    os.replace(tmp, trusted_path)
    return trusted_path, True
