"""Remote SSH + grok custom-provider spawns are REFUSED (terminal.py
_maybe_wrap_ssh).

The managed GROK_HOME is a laptop-local path; on a remote host the spawn
would either die (self-healed sessionless tree lacks the BYOK blocks/hooks)
or — with a logged-in remote ~/.grok — send the xAI session JWT to a
third-party endpoint. Native (non-provider) remote grok must keep working.

Unbound-call tests against a duck-typed TerminalView (the test_terminal_lifetime
pattern) — no widget construction, no subprocess.
"""
import types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk  # noqa: F401  (import pins the GI types the module needs)

import harnesses
import terminal
from terminal import TerminalView

from settings import Settings


def _fake_tv(adapter_id, settings):
    return types.SimpleNamespace(
        _settings=settings,
        _adapter=harnesses.get_adapter(adapter_id),
        _project=types.SimpleNamespace(path='/remote/proj', spawn_cwd=None),
        _fallback_reason=None,
    )


def _settings_with_host():
    return Settings(hosts={
        'h1': {'ssh_target': 'user@box.example', 'display_name': 'Box'},
    })


def test_local_host_passes_through():
    tv = _fake_tv('grok', _settings_with_host())
    out = TerminalView._maybe_wrap_ssh(tv, ['grok'], {'GROK_HOME': '/x'}, 'localhost')
    assert out == (['grok'], {'GROK_HOME': '/x'})
    assert tv._fallback_reason is None


def test_remote_unknown_host_refused():
    tv = _fake_tv('grok', _settings_with_host())
    out = TerminalView._maybe_wrap_ssh(tv, ['grok'], {'GROK_HOME': '/x'}, 'nope')
    assert out == (None, None)
    assert tv._fallback_reason == "Unknown remote host 'nope'"


def test_remote_grok_custom_provider_refused():
    """adapter grok + env carries GROK_HOME → refused, exact message pinned."""
    tv = _fake_tv('grok', _settings_with_host())
    env = {'GROK_HOME': '/home/u/.ProjectMan/grok-homes/ollama',
           'GROK_MODELS_BASE_URL': 'http://x/v1', 'XAI_API_KEY': 'k',
           'HOME': '/home/u'}
    out = TerminalView._maybe_wrap_ssh(tv, ['grok', '-m', 'm'], env, 'h1')
    assert out == (None, None)
    assert tv._fallback_reason == (
        'custom Grok providers are local-only (managed GROK_HOME '
        'cannot cross hosts)')


def test_remote_grok_native_still_wraps():
    """Native grok (no GROK_HOME in env) keeps spawning remotely."""
    tv = _fake_tv('grok', _settings_with_host())
    out = TerminalView._maybe_wrap_ssh(tv, ['grok'], None, 'h1')
    argv, env = out
    assert argv is not None and argv[0] == 'ssh'
    assert env is None
    assert tv._fallback_reason is None


def test_remote_claude_provider_still_wraps():
    """The refusal is grok-only: claude provider env crosses as before."""
    tv = _fake_tv('claude', _settings_with_host())
    env = {'ANTHROPIC_BASE_URL': 'http://x', 'HOME': '/home/u'}
    argv, out_env = TerminalView._maybe_wrap_ssh(tv, ['claude'], env, 'h1')
    assert argv is not None and argv[0] == 'ssh'
    assert out_env is None
    assert tv._fallback_reason is None
