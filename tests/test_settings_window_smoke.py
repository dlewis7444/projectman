"""Headless GTK construction smoke for the Models tab + a UI↔spawn invariant.

Purpose: catch the class of UI-surface defects that plain pytest and the
gui-smoke gate miss — a window that crashes when built with realistic state
(e.g. the late Adw.ExpandableRow→ExpanderRow crash only fired once a provider
existed), and a UI that disagrees with the spawn path about which provider
Tier Assignments apply to.

These construct real GTK widgets headless (no display) like the sidebar tests,
with ``SettingsWindow.present`` patched out so the unrealized window never
needs a surface. They run in plain pytest, so they gate every commit AND the
VM gate's pytest phase automatically.
"""
import types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from settings import Settings
import settings_window as sw_mod


# Adw.Application construction (not run) is enough to bootstrap GTK offscreen,
# matching the sidebar tests. The app object only needs an emit() no-op.
_APP = Adw.Application(application_id='com.test.pm.smoke')
_FAKE_APP = types.SimpleNamespace(emit=lambda *a, **k: None,
                                  get_application=lambda: _APP)


def _walk(widget):
    """Yield widget and every descendant (depth-first)."""
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _make_sw(settings):
    """Construct a SettingsWindow without presenting it (no display needed).

    present() realizes the window and needs a surface; patching it out lets the
    full page-build path — including _build_provider_card — run headless, which
    is exactly where the ExpandableRow crash lived.
    """
    sw = None

    def _no_present(_self, *a, **k):
        pass
    orig = sw_mod.SettingsWindow.present
    sw_mod.SettingsWindow.present = _no_present
    try:
        sw = sw_mod.SettingsWindow(settings, _FAKE_APP, None)
    finally:
        sw_mod.SettingsWindow.present = orig
    return sw


def _ollama_provider(models=('glm-5.2:cloud[1m]',)):
    return {'ollama': {'name': 'Ollama', 'base_url': 'http://localhost:11434',
                       'api_key': 'k', 'models': list(models)}}


def _tier_combos(card):
    """The Adw.ComboRow tier combos added to a provider card, keyed by title."""
    return {w.get_title(): w for w in _walk(card)
            if isinstance(w, Adw.ComboRow)}


# --- #1: construction smoke (catches crash-on-build) ------------------------

def test_builds_without_crash_with_provider_defined():
    """The Models page builds with a provider defined — the state that triggered
    the Adw.ExpandableRow crash (the card wasn't built when providers was
    empty, so the first open after adding a provider crashed)."""
    s = Settings(providers=_ollama_provider(), model_default='ollama',
                 tier_models={'ollama': {'opus': 'glm-5.2:cloud[1m]'}})
    sw = _make_sw(s)  # must not raise
    assert sw is not None


def test_builds_without_crash_native_default_no_providers():
    """The original first-open shape: native default, no providers. Must build."""
    sw = _make_sw(Settings())  # must not raise
    assert sw is not None


def test_provider_card_has_five_tier_combos():
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    sw = _make_sw(s)
    card = sw._build_provider_card('ollama', s.providers['ollama'])
    combos = _tier_combos(card)
    assert set(combos) == {'Opus', 'Sonnet', 'Haiku', 'Subagent', 'Fable (future?)'}


def test_fable_combo_disabled_in_provider_card():
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    sw = _make_sw(s)
    card = sw._build_provider_card('ollama', s.providers['ollama'])
    combos = _tier_combos(card)
    assert combos['Fable (future?)'].get_sensitive() is False
    # The real tiers are editable.
    assert combos['Opus'].get_sensitive() is True


def test_tier_combos_sensitive_even_when_default_is_native():
    """The B2 invariant: TA is editable for any defined provider regardless of
    the default. With a native default, the ollama card's tier combos are still
    sensitive (the old standalone TA group disabled them when default='')."""
    s = Settings(providers=_ollama_provider(), model_default='')  # native default
    sw = _make_sw(s)
    card = sw._build_provider_card('ollama', s.providers['ollama'])
    combos = _tier_combos(card)
    assert combos['Opus'].get_sensitive() is True
    assert combos['Sonnet'].get_sensitive() is True
    assert combos['Subagent'].get_sensitive() is True
    assert combos['Fable (future?)'].get_sensitive() is False  # placeholder


# --- #2: UI↔spawn editability invariant --------------------------------------

def test_override_provider_tiers_honored_and_card_editable():
    """The cross-layer agreement: a provider reachable only via a per-project
    override (native default) has its tiers honored by build_spawn_env AND its
    card combos are editable. Locks the per-provider design so the UI and the
    spawn path can't silently disagree again (the TA-disabled-when-native gap)."""
    from models import build_spawn_env
    s = Settings(providers={**_ollama_provider(models=('glm', 'kimi')),
                            **{'openrouter': {'name': 'OR', 'base_url': 'http://b',
                                              'api_key': 'k', 'models': ['or-opus']}}},
                 model_default='',                       # native default
                 model_overrides={'/p': 'openrouter'},  # project on openrouter
                 tier_models={'openrouter': {'opus': 'or-opus'}})
    # Spawn side: the override provider's tiers are injected.
    env, reason = build_spawn_env(s, '/p')
    assert reason is None and env is not None
    assert env['ANTHROPIC_BASE_URL'] == 'http://b'
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'or-opus'
    # UI side: the override provider's card is editable even though it's not
    # the default (and the default is native).
    sw = _make_sw(s)
    card = sw._build_provider_card('openrouter', s.providers['openrouter'])
    combos = _tier_combos(card)
    assert combos['Opus'].get_sensitive() is True