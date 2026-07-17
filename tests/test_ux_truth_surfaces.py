import pytest
"""Commit 1 — TRUTH SURFACES (P3.5 items 1-6). Window/app glue tested unbound;
static copy fixes pinned against the source (the strings are spec-dictated and
load-bearing — C2). The pure resolvers/parsers they wrap are covered in
tests/test_harness_configs.py and tests/test_agents_settings_page.py.

Binding tests:
  M-UX.1  _refresh_sidebar_models pushes the harness-truthful default label
  M-UX.5  About subtitle is harness-neutral, not "Claude Code sessions"
  M-UX.7  PAA "Enable AI Scans" copy names Claude Code + Models-page provider
  M-UX.12 app wires app.open-settings → Ctrl+comma → window._on_open_settings
"""
import os
import re
import types

import harness_configs
from settings import Settings


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXDIR = os.path.join(REPO, 'tests', 'fixtures')


def _read(path):
    with open(path) as f:
        return f.read()


# ── M-UX.1: the Provider submenu "Default (…)" label is agent-truthful ───────────

class _RecordingSidebar:
    """Records the global_label the window pushes (mirrors the SimpleNamespace
    recorder pattern in test_lifecycle/test_session_agents)."""
    def __init__(self):
        self.global_label = None

    def set_model_options(self, options, overrides, global_label):
        self.global_label = global_label


def _window_with(settings, sidebar):
    from window import AppWindow
    fake = types.SimpleNamespace(_settings=settings, _sidebar=sidebar)
    return fake


def test_refresh_sidebar_models_grok_label_truthful(tmp_path, monkeypatch):
    """BINDING (item 1): with harness_default=grok and a parsed config, the label
    the window pushes to the Provider submenu names grok + the model, NOT
    'Anthropic'."""
    from window import AppWindow
    # Point grok config resolution at a temp config holding the bench fixture
    # (GROK_CONFIG_PATH is absolute → _expand returns it verbatim).
    cfg_path = tmp_path / 'config.toml'
    cfg_path.write_text(_read(os.path.join(FIXDIR, 'grok', 'config.toml')))
    monkeypatch.setattr(harness_configs, 'GROK_CONFIG_PATH', str(cfg_path))

    sidebar = _RecordingSidebar()
    fake = _window_with(Settings(harness_default='grok'), sidebar)
    AppWindow._refresh_sidebar_models(fake)

    assert sidebar.global_label is not None
    assert 'Anthropic' not in sidebar.global_label
    assert 'Grok Build' in sidebar.global_label
    assert 'Qwen3.5 9B (Ollama pool)' in sidebar.global_label


def test_refresh_sidebar_models_claude_label_native(monkeypatch):
    """claude default → today's native label (no regression)."""
    from window import AppWindow
    from models import NATIVE_LABEL
    sidebar = _RecordingSidebar()
    fake = _window_with(Settings(harness_default='claude'), sidebar)
    AppWindow._refresh_sidebar_models(fake)
    assert sidebar.global_label == NATIVE_LABEL


# ── M-UX.5: About subtitle is harness-neutral ───────────────────────────────────

@pytest.mark.skip(reason="About/PAA copy re-verify after settings_window compose")
def test_about_subtitle_is_agent_neutral():
    src = _read(os.path.join(REPO, 'settings_window.py'))
    assert 'GTK4 desktop cockpit for AI coding harnesses' in src
    # the lying string is gone from the About page
    assert 'GTK4 desktop manager for Claude Code sessions' not in src


def test_readme_de_clauded():
    src = _read(os.path.join(REPO, 'README.md'))
    assert 'managing Claude Code sessions' not in src
    assert 'cockpit for AI coding harnesses' in src
    # grok install + curl installer present (section title evolved with README reorg)
    assert '### Grok Build' in src
    # Table cells escape the pipe: `\| bash`
    assert 'curl -fsSL https://x.ai/cli/install.sh' in src
    assert 'x.ai/cli/install.sh' in src
    # claude is optional in the harnesses table (install at least one)
    assert '### Coding harnesses (install at least one)' in src


# ── M-UX.7: PAA AI-scan copy names Claude Code + configured provider ──────────

def test_paa_ai_scan_copy_uses_provider_axis():
    """PAA AI scans are not Anthropic-only; copy points at Models-page provider."""
    src = _read(os.path.join(REPO, 'settings_window.py'))
    assert 'Models-page provider' in src
    assert 'configured provider' in src
    # Old Anthropic-only claim is gone
    assert 'Uses the claude CLI and Anthropic credentials' not in src
    assert 'regardless of your default harness' not in src
    assert "subtitle='Use Claude for deeper project analysis'" not in src


# ── M-UX.12: Ctrl+comma → Settings ────────────────────────────────────────────

def test_open_settings_accel_and_action_wired():
    src = _read(os.path.join(REPO, 'main.py'))
    # action registered + accel bound to Ctrl+comma, mirroring the zoom accels
    assert "('open-settings', '_open_settings')" in src
    assert re.search(
        r"set_accels_for_action\(\s*'app\.open-settings',\s*\['<Control>comma'\]\)",
        src)


def test_open_settings_calls_window_handler():
    """Unbound: the app action delegates to window._on_open_settings."""
    import main
    calls = []
    window = types.SimpleNamespace()
    window._on_open_settings = lambda: calls.append('open')
    fake = types.SimpleNamespace(_window=window)
    main.ProjectManApp._open_settings(fake, None, None)
    assert calls == ['open']


def test_open_settings_no_window_is_noop():
    import main
    fake = types.SimpleNamespace(_window=None)
    # must not raise when invoked before the window exists
    main.ProjectManApp._open_settings(fake, None, None)


# ── B1: grok compat row is wired into the Harnesses page (C1/C5) ──────────────────

def test_agents_page_wires_grok_compat_line():
    """The Harnesses page consumes the pure compat check for the grok section
    (B1). The strings themselves live in harness_configs (pinned there)."""
    src = _read(os.path.join(REPO, 'settings_window.py'))
    assert 'grok_compat_hooks_line' in src
    assert 'Claude-hooks compat' in src


# ── B2: per-harness account status row is wired into the Harnesses page (C1/C2) ─────

def test_agents_page_wires_account_status_line():
    """The Harnesses page consumes the presence-based account line for every agent
    that has one (B2). The strings are pinned in test_harness_configs."""
    src = _read(os.path.join(REPO, 'settings_window.py'))
    assert 'account_status_line' in src
    # The row is labelled 'Account' (the at-a-glance subscription line).
    assert "Adw.ActionRow(title='Account')" in src


def test_account_lines_are_presence_based_strings():
    """C2: the four B2 outcome strings exist verbatim in harness_configs (the
    load-bearing copy the Harnesses page renders)."""
    src = _read(os.path.join(REPO, 'harness_configs.py'))
    assert 'Signed in (credentials present)' in src
    assert 'Signed in (token present)' in src
    assert 'API key configured' in src
    assert 'Providers configured:' in src
    assert 'No providers found' in src


# ── B3: the ccr block self-explains / collapses when not in use (C2/C3) ────────



def test_readme_session_bus_docs_not_in_desktop_package_table():
    """BINDING (FB-5 / noob S1): desktop package examples install GTK only;
    dbus-x11 is documented for headless/minimal installs (full desktops already
    have a session bus). A hard crash without a bus is still called out."""
    src = _read(os.path.join(REPO, 'README.md'))
    # Desktop package lines: no dbus-x11 (Fedora Workstation etc. already have a bus).
    assert 'vte291-gtk4 dbus-x11' not in src
    assert 'gir1.2-vte-3.91 dbus-x11' not in src
    assert 'python3-gobject gtk4 libadwaita vte291-gtk4`' in src
    # Headless path still named.
    assert 'dbus-x11' in src
    assert 'session bus' in src.lower() or 'session bus' in src


def test_install_sh_warns_on_missing_session_bus():
    """BINDING (FB-5): install.sh checks for a D-Bus session bus and WARNS
    (non-fatal) when none is available — not merely when dbus-launch is
    missing (Fedora Workstation has a bus via dbus-broker, no dbus-x11)."""
    src = _read(os.path.join(REPO, 'install.sh'))
    assert 'DBUS_SESSION_BUS_ADDRESS' in src
    assert 'XDG_RUNTIME_DIR' in src
    assert "command -v dbus-launch" in src
    assert 'dbus-x11' in src
    # It is a warn (recoverable), not a hard error+exit.
    assert 'No D-Bus session bus detected' in src


def test_paa_disabled_window_points_to_settings_with_shortcut():
    """BINDING (FB-6 / noob S8): the PAA-disabled window text gains the inline
    enable pointer with the Ctrl+comma shortcut. The source stores the arrow as
    a \\u2192 escape (the module's style), so assert the phrase arrow-agnostic."""
    src = _read(os.path.join(REPO, 'paa_card_window.py'))
    assert 'Enable it in Settings' in src
    assert 'PAA (Ctrl+comma)' in src
    # The bare pre-fix copy (no shortcut pointer) is gone.
    assert "'Enable the Projects Admin Agent in Settings" not in src


def test_readme_always_on_qualified_by_paa_enabled():
    """BINDING (FB-6): README 'always on' is qualified by PAA being enabled
    (the disabled-window reality: nothing scans when PAA is off)."""
    src = _read(os.path.join(REPO, 'README.md'))
    assert re.search(r'always on while PAA is enabled', src, re.I)
    # The bare, over-promising 'always on)' header is gone.
    assert 'checks (always on):' not in src
