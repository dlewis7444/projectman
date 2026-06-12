# tests/test_sidebar_state.py
import pytest
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

# Bootstrap GTK (required before instantiating widgets)
app = Adw.Application(application_id='com.test.pm')


def _make_row():
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    proj = Project(name='test', path='/tmp/test')
    history = HistoryReader()
    watcher = StatusWatcher()
    return ProjectRow(proj, history, watcher)


def test_initial_state_is_inactive():
    row = _make_row()
    assert row._process_state == 'inactive'


def test_set_process_state_attached():
    row = _make_row()
    row.set_process_state('attached')
    assert row._process_state == 'attached'


def test_set_process_state_detached():
    row = _make_row()
    row.set_process_state('detached')
    assert row._process_state == 'detached'
    # name label should have detached CSS class
    assert row._name_label.has_css_class('project-row-detached')


def test_set_process_state_back_to_inactive_clears_css():
    row = _make_row()
    row.set_process_state('detached')
    row.set_process_state('inactive')
    assert not row._name_label.has_css_class('project-row-detached')


def test_deactivate_button_enabled_only_when_attached():
    row = _make_row()
    row.set_process_state('attached')
    assert row._deactivate_btn.get_sensitive() is True
    row.set_process_state('detached')
    assert row._deactivate_btn.get_sensitive() is False
    row.set_process_state('inactive')
    assert row._deactivate_btn.get_sensitive() is False


def test_update_status_attached_no_file_shows_done():
    """Attached session with no status file falls back to status-done (green)."""
    row = _make_row()
    # watcher has no status file — get_project_status returns 'idle'
    row.set_process_state('attached')
    assert row._status_dot.has_css_class('status-done')
    assert not row._status_dot.has_css_class('status-idle')


# ===========================================================================
# P2 Part A — A1 (expander through adapter.list_sessions) + A5 (caps gating).
# The GTK sliver; the headless contract is in test_agent_seam.py.
# ===========================================================================

def _menu_labels(row):
    from gi.repository import GLib
    out = []
    for i in range(row._menu.get_n_items()):
        v = row._menu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
        if v:
            out.append(v.get_string())
    return out


class _FakeCapsAdapter:
    """Configurable fake adapter for caps/sessions sliver tests."""
    id = 'fake'
    display_name = 'Fake'

    def __init__(self, caps, refs=None):
        self.caps = caps
        self._refs = refs or []

    def list_sessions(self, project, settings=None):
        return list(self._refs)


def _row_with_adapter(monkeypatch, caps, refs=None, path='/tmp/test'):
    import agents
    from settings import Settings
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeCapsAdapter(caps, refs))
    s = Settings(agent_default='fake')
    proj = Project(name='test', path=path)
    return ProjectRow(proj, HistoryReader(), StatusWatcher(), settings=s)


def test_full_caps_adapter_shows_model_submenu_and_arrow(monkeypatch):
    import agents
    caps = agents.AgentCaps(continue_=True, resume_by_id=True, sessions=True,
                            model_select=True)
    row = _row_with_adapter(monkeypatch, caps)
    assert 'Model' in _menu_labels(row)
    assert row._arrow.get_visible() is True


def test_low_caps_adapter_hides_model_submenu(monkeypatch):
    import agents
    caps = agents.AgentCaps(continue_=True, model_select=False, sessions=True,
                            resume_by_id=True)
    row = _row_with_adapter(monkeypatch, caps)
    assert 'Model' not in _menu_labels(row)


def test_no_sessions_caps_hides_expander_arrow(monkeypatch):
    import agents
    caps = agents.AgentCaps(continue_=True, sessions=False, resume_by_id=False,
                            model_select=True)
    row = _row_with_adapter(monkeypatch, caps)
    assert row._arrow.get_visible() is False


def test_expander_rows_come_from_adapter_list_sessions(monkeypatch):
    """A1: the expander renders SessionRefs from adapter.list_sessions, not the
    HistoryReader. SessionHistoryRow reads ref.id."""
    import agents
    from sidebar import SessionHistoryRow, NewSessionRow
    caps = agents.AgentCaps(continue_=True, resume_by_id=True, sessions=True,
                            model_select=True)
    refs = [
        agents.SessionRef(id='sess-A', title='Alpha', last_active=2000),
        agents.SessionRef(id='sess-B', title='Beta', last_active=1000),
    ]
    row = _row_with_adapter(monkeypatch, caps, refs=refs)
    row._load_sessions()
    children = []
    child = row._session_listbox.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    # First child is the New Session row, then the two SessionRefs in order.
    assert isinstance(children[0], NewSessionRow)
    hist_rows = [c for c in children if isinstance(c, SessionHistoryRow)]
    assert [r._ref.id for r in hist_rows] == ['sess-A', 'sess-B']
    # The row exposes ref.id (the canonical contract), not session_id.
    assert hist_rows[0]._ref.id == 'sess-A'
    assert not hasattr(hist_rows[0], '_session')


def test_no_resume_caps_expander_shows_only_new_session(monkeypatch):
    """A5: caps.resume_by_id False → no past-session rows enumerated, only the
    New Session entry."""
    import agents
    from sidebar import SessionHistoryRow, NewSessionRow
    caps = agents.AgentCaps(continue_=True, resume_by_id=False, sessions=True,
                            model_select=True)
    refs = [agents.SessionRef(id='x', title='X', last_active=1)]
    row = _row_with_adapter(monkeypatch, caps, refs=refs)
    row._load_sessions()
    rows = []
    child = row._session_listbox.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    assert len(rows) == 1 and isinstance(rows[0], NewSessionRow)


def test_session_activated_emits_ref_id(monkeypatch):
    """The session-activated signal carries ref.id (the opaque adapter id)."""
    import agents
    from sidebar import SessionHistoryRow
    caps = agents.AgentCaps(continue_=True, resume_by_id=True, sessions=True)
    refs = [agents.SessionRef(id='the-id', title='T', last_active=5)]
    row = _row_with_adapter(monkeypatch, caps, refs=refs, path='/tmp/emit')
    row._load_sessions()
    got = []
    row.connect('session-activated', lambda r, p, sid: got.append((p, sid)))
    hist_row = None
    child = row._session_listbox.get_first_child()
    while child is not None:
        if isinstance(child, SessionHistoryRow):
            hist_row = child
            break
        child = child.get_next_sibling()
    row._on_session_activated(row._session_listbox, hist_row)
    assert got == [('/tmp/emit', 'the-id')]


# ===========================================================================
# P2 Part B — B3 UI: Agent submenu, subtitle/badge, signal rename.
# ===========================================================================

def _make_row_with_settings(settings, path='/tmp/test'):
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    proj = Project(name='test', path=path)
    return ProjectRow(proj, HistoryReader(), StatusWatcher(), settings=settings)


def test_agent_submenu_lists_registered_adapters():
    """The Agent submenu offers Follow default + each registered adapter."""
    from settings import Settings
    row = _make_row_with_settings(Settings(agent_default='claude'))
    labels = []
    from gi.repository import GLib
    for i in range(row._agent_submenu.get_n_items()):
        v = row._agent_submenu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
        if v:
            labels.append(v.get_string())
    assert labels[0].startswith('Follow default')
    assert 'Claude Code' in labels
    assert 'opencode' in labels


def test_agent_submenu_lists_three_agents_including_grok(monkeypatch):
    """T-B4: the third agent (Grok Build) appears in the Agent submenu model by
    construction — the submenu iterates agents.ADAPTERS, so grok shows up with
    no sidebar code change."""
    from settings import Settings
    from gi.repository import GLib
    row = _make_row_with_settings(Settings(agent_default='claude'))
    labels = []
    for i in range(row._agent_submenu.get_n_items()):
        v = row._agent_submenu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
        if v:
            labels.append(v.get_string())
    assert 'Claude Code' in labels
    assert 'opencode' in labels
    assert 'Grok Build' in labels


def test_grok_override_selects_grok_adapter(monkeypatch):
    """T-B4: a per-project grok override resolves the row to the GrokAdapter
    (full caps → Model submenu + expander arrow visible)."""
    from settings import Settings
    s = Settings(agent_default='claude', agent_overrides={'/tmp/g': 'grok'})
    row = _make_row_with_settings(s, path='/tmp/g')
    assert row._adapter().id == 'grok'
    assert 'Model' in _menu_labels(row)
    assert row._arrow.get_visible() is True


def test_f9_settings_threaded_into_get_adapter(monkeypatch):
    """F9 / T-B4: the sidebar's get_adapter call now passes settings, so a
    named-but-missing agent gates on the M-P3.2 fallback (agent_default →
    first-available), NOT a hardcoded claude.

    Modelled against a claude-LESS fleet so 'falls back to the configured
    default' is provably the mechanism: agent_default=grok + a bogus override →
    the row resolves to grok (the default), not claude. Without threading
    settings, get_adapter('bogus') would return the legacy claude default and
    this would fail."""
    import agents
    from settings import Settings
    # Snapshot/restore ADAPTERS so the claude-less fleet doesn't leak.
    saved = dict(agents.ADAPTERS)
    try:
        s = Settings(agent_default='grok', agent_overrides={'/tmp/p': 'bogus'})
        row = _make_row_with_settings(s, path='/tmp/p')
        # effective agent for the project is the bogus override...
        assert s.effective_agent('/tmp/p') == 'bogus'
        # ...but the row's adapter is the settings-aware fallback: grok (the
        # configured default), never a hardcoded claude.
        assert row._adapter().id == 'grok'
    finally:
        agents.ADAPTERS.clear()
        agents.ADAPTERS.update(saved)


def test_f9_settings_threaded_first_available_when_default_also_bogus(monkeypatch):
    """F9: agent_default ALSO bogus → first-available registered adapter (still
    settings-aware, proven against a fleet with claude removed)."""
    import agents
    from settings import Settings
    saved = dict(agents.ADAPTERS)
    try:
        # Remove claude so 'first-available' is provably opencode, not claude.
        opencode = saved['opencode']
        agents.ADAPTERS.clear()
        agents.ADAPTERS['opencode'] = opencode
        agents.ADAPTERS.update({k: v for k, v in saved.items()
                                if k not in ('opencode', 'claude')})
        s = Settings(agent_default='alsobogus', agent_overrides={'/tmp/p': 'bogus'})
        row = _make_row_with_settings(s, path='/tmp/p')
        assert row._adapter().id == 'opencode'  # first-available, NOT claude
    finally:
        agents.ADAPTERS.clear()
        agents.ADAPTERS.update(saved)


def test_agent_submenu_present_in_menu():
    from settings import Settings
    row = _make_row_with_settings(Settings())
    assert 'Agent' in _menu_labels(row)


def test_agent_radio_reflects_override():
    from settings import Settings
    s = Settings(agent_default='claude', agent_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._agent_action.get_state().get_string() == 'opencode'


def test_agent_radio_follow_default_when_no_override():
    from models import FOLLOW_DEFAULT
    from settings import Settings
    row = _make_row_with_settings(Settings(agent_default='opencode'), path='/tmp/p')
    assert row._agent_action.get_state().get_string() == FOLLOW_DEFAULT


def test_agent_select_emits_change_signal():
    from settings import Settings
    from gi.repository import GLib
    row = _make_row_with_settings(Settings(), path='/tmp/p')
    got = []
    row.connect('project-agent-change', lambda r, aid: got.append(aid))
    row._on_agent_select(row._agent_action, GLib.Variant('s', 'opencode'))
    assert got == ['opencode']
    assert row._agent_action.get_state().get_string() == 'opencode'


def test_subtitle_hidden_for_plain_default():
    """Default agent + native model → no subtitle clutter."""
    from settings import Settings
    row = _make_row_with_settings(Settings(agent_default='claude'), path='/tmp/p')
    assert row._subtitle_label.get_visible() is False


def test_subtitle_shows_non_default_agent():
    from settings import Settings
    s = Settings(agent_default='claude', agent_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is True
    assert 'opencode' in row._subtitle_label.get_text()


def test_subtitle_shows_agent_and_model():
    from settings import Settings
    s = Settings(agent_default='opencode',
                 model_overrides={'/tmp/p': 'ollama/qwen3.5:cloud'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is True
    txt = row._subtitle_label.get_text()
    assert 'opencode' in txt and 'ollama/qwen3.5:cloud' in txt


# ===========================================================================
# P3.5d Item 1 (FINDING 3 / C5): the subtitle tells the truth about NOW.
# A restored saved-agent-wins session (A2) can RUN a different agent than the
# one configured for the next session; the subtitle must lead with what is
# actually running. T1 = the mismatch string verbatim; T2 = byte-identical
# golden when running == configured (the pin that breaks if we always show the
# running form); T3 = no live session → today's string; T4 = model suffix in
# both shapes.
# ===========================================================================

def test_subtitle_running_agent_mismatch_leads_with_running():
    """T1: live child runs grok while the row is configured for opencode →
    '<Running> (next: <Configured>)'. Reverting the running-first builder (always
    showing the configured agent) yields 'opencode' and FAILS this."""
    from settings import Settings
    s = Settings(agent_default='claude', agent_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_agent('grok')
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'Grok Build (next: opencode)'


def test_subtitle_running_equals_configured_is_byte_identical():
    """T2 (GOLDEN pin): running == configured → today's exact string, byte for
    byte. If the builder ALWAYS rendered the running-first form this would read
    'opencode (next: opencode)' and FAIL."""
    from settings import Settings
    s = Settings(agent_default='claude', agent_overrides={'/tmp/p': 'opencode'})
    # Baseline: no running agent → today's string.
    baseline = _make_row_with_settings(s, path='/tmp/p')
    golden = baseline._subtitle_label.get_text()
    assert golden == 'opencode'
    # Running agent EQUALS the configured one → identical to the baseline.
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_agent('opencode')
    assert row._subtitle_label.get_text() == golden
    assert row._subtitle_label.get_text() == 'opencode'


def test_subtitle_no_running_session_is_todays_string():
    """T3: no live session (running is None) → today's string unchanged, and a
    plain default row stays clean (no subtitle)."""
    from settings import Settings
    # Non-default agent, no running session → shows the configured agent.
    s = Settings(agent_default='claude', agent_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._running_agent is None
    assert row._subtitle_label.get_text() == 'opencode'
    # Plain default + no model + no running session → hidden (clean).
    plain = _make_row_with_settings(Settings(agent_default='claude'), path='/tmp/q')
    assert plain._subtitle_label.get_visible() is False


def test_subtitle_model_suffix_preserved_in_both_shapes():
    """T4: the ' · <model>' suffix is preserved in BOTH the byte-identical shape
    and the running-mismatch shape."""
    from settings import Settings
    s = Settings(agent_default='claude',
                 agent_overrides={'/tmp/p': 'opencode'},
                 model_overrides={'/tmp/p': 'ollama/qwen3.5:cloud'})
    # No mismatch → today's 'agent · model' shape, byte-identical.
    matched = _make_row_with_settings(s, path='/tmp/p')
    assert matched._subtitle_label.get_text() == 'opencode · ollama/qwen3.5:cloud'
    # Mismatch → running-first head, model suffix still appended.
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_agent('grok')
    assert (row._subtitle_label.get_text()
            == 'Grok Build (next: opencode) · ollama/qwen3.5:cloud')


def test_subtitle_mismatch_overrides_clean_default_hide():
    """C5 corollary: a default-configured row (normally hidden) still shows the
    truth when a live child runs a NON-default agent."""
    from settings import Settings
    s = Settings(agent_default='claude')          # default → normally hidden
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is False   # clean while idle
    row.set_running_agent('grok')                  # live child runs grok
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'Grok Build (next: Claude Code)'


def test_set_running_agent_none_restores_clean_subtitle():
    """Clearing the running agent (session ended) restores the configured-only
    subtitle — the mismatch form is gone."""
    from settings import Settings
    s = Settings(agent_default='claude', agent_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_agent('grok')
    assert row._subtitle_label.get_text() == 'Grok Build (next: opencode)'
    row.set_running_agent(None)
    assert row._subtitle_label.get_text() == 'opencode'


def test_sidebar_set_running_agent_unknown_path_is_noop():
    """Sidebar.set_running_agent for a path with no row must not raise (window.py
    fires it unconditionally)."""
    from settings import Settings
    from sidebar import Sidebar
    from model import HistoryReader, StatusWatcher

    class _EmptyStore:
        def load_projects(self):
            return []

    sb = Sidebar(_EmptyStore(), HistoryReader(), StatusWatcher(),
                 settings=Settings())
    sb.set_running_agent('/no/such/path', 'grok')   # must be a silent no-op


def test_new_session_signal_rename():
    """The signal is project-new-session (renamed from project-new-claude)."""
    from settings import Settings
    from sidebar import Sidebar, ProjectRow
    # ProjectRow exposes the renamed signal.
    assert GObject_signal_exists(ProjectRow, 'project-new-session')
    assert not GObject_signal_exists(ProjectRow, 'project-new-claude')
    assert GObject_signal_exists(Sidebar, 'project-new-session')


# ===========================================================================
# P2 review fix — MAJOR-2: the attached idle→done dot remap is gated on
# caps.rich_status (its first consumer). A bridgeless agent must not wear a
# permanent fake-green "work finished" dot.
# ===========================================================================

def test_attached_idle_dot_stays_idle_for_rich_status_false(monkeypatch):
    """T5 — rich_status=False, attached, watcher says 'idle' (no status file)
    → the dot is status-idle, NOT status-done."""
    import agents
    caps = agents.AgentCaps(continue_=True, rich_status=False)
    row = _row_with_adapter(monkeypatch, caps)
    row.set_process_state('attached')
    assert row._status_dot.has_css_class('status-idle')
    assert not row._status_dot.has_css_class('status-done')


def test_attached_idle_dot_remaps_to_done_for_rich_status_true(monkeypatch):
    """T6 — no-regression pin: rich_status=True keeps today's behavior — an
    attached row with no status file yet renders status-done (green)."""
    import agents
    caps = agents.AgentCaps(continue_=True, rich_status=True)
    row = _row_with_adapter(monkeypatch, caps)
    row.set_process_state('attached')
    assert row._status_dot.has_css_class('status-done')
    assert not row._status_dot.has_css_class('status-idle')


class _ExplodingCapsAdapter:
    """Caps access can be made to raise AFTER construction — exercises the
    dot path's never-throw fallback without breaking _apply_caps at init."""
    id = 'fake'
    display_name = 'Fake (exploding caps)'

    def __init__(self, caps):
        self._caps = caps
        self.explode = False

    @property
    def caps(self):
        if self.explode:
            raise RuntimeError('caps unavailable')
        return self._caps

    def list_sessions(self, project, settings=None):
        return []


def test_attached_dot_survives_adapter_resolution_failure(monkeypatch):
    """Spec failure mode: if adapter resolution raises, preserve the historic
    remap (done) — the dot path never throws."""
    import agents
    from settings import Settings
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    adapter = _ExplodingCapsAdapter(
        agents.AgentCaps(continue_=True, rich_status=False))
    monkeypatch.setitem(agents.ADAPTERS, 'fake', adapter)
    row = ProjectRow(Project(name='test', path='/tmp/test'),
                     HistoryReader(), StatusWatcher(),
                     settings=Settings(agent_default='fake'))
    adapter.explode = True
    row.set_process_state('attached')   # must not raise
    assert row._status_dot.has_css_class('status-done')


def GObject_signal_exists(cls, name):
    from gi.repository import GObject
    return GObject.signal_lookup(name, cls) != 0


# ── M-UX.11 (S12): the two header icon buttons carry tooltips ─────────────────

def _make_sidebar():
    from model import ProjectStore, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    settings = Settings(projects_dir='/tmp/pm-nonexistent-sidebar-test')
    store = ProjectStore(settings)
    history = HistoryReader()
    watcher = StatusWatcher()
    return Sidebar(store, history, watcher, version='test', settings=settings)


def test_header_icon_buttons_have_tooltips():
    """S12 audit: the adjacent header icon buttons (New Project + the PAA
    sparkle) are labeled for keyboard/screen-reader users."""
    sb = _make_sidebar()
    assert sb._paa_btn.get_tooltip_text() == 'Projects Admin Agent'
    # the "New Project" (+) button is the first child of the header row; assert a
    # button with that tooltip exists in the sidebar.
    found = _find_button_with_tooltip(sb, 'New Project')
    assert found, 'the New Project (+) header button has no tooltip'


def test_settings_gear_has_tooltip():
    sb = _make_sidebar()
    assert _find_button_with_tooltip(sb, 'Settings'), 'Settings gear has no tooltip'


def _find_button_with_tooltip(widget, tooltip):
    if isinstance(widget, Gtk.Button) and widget.get_tooltip_text() == tooltip:
        return True
    child = widget.get_first_child() if hasattr(widget, 'get_first_child') else None
    while child is not None:
        if _find_button_with_tooltip(child, tooltip):
            return True
        child = child.get_next_sibling()
    return False


# ===========================================================================
# P3.5e FB-1a: the per-project Model submenu lists the EFFECTIVE agent's
# NATIVE models (grok config keys / opencode provider-model ids), not the
# ccr/providers list. claude submenu stays byte-identical to today.
# ===========================================================================

def _model_submenu_labels(row):
    from gi.repository import GLib
    out = []
    for i in range(row._model_submenu.get_n_items()):
        v = row._model_submenu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
        if v:
            out.append(v.get_string())
    return out


def _model_submenu_targets(row):
    """The set-model target VALUES (what a pick writes to model_overrides)."""
    from gi.repository import GLib
    out = []
    for i in range(row._model_submenu.get_n_items()):
        v = row._model_submenu.get_item_attribute_value(i, 'target', GLib.VariantType('s'))
        if v:
            out.append(v.get_string())
    return out


def test_grok_project_model_submenu_lists_native_models(tmp_path, monkeypatch):
    """BINDING (FB-1a): a grok project's Model submenu contains pool-qwen — the
    config KEY — and offering it as the set-model target (the -m value). The ccr
    providers list is NOT what's shown."""
    import agent_configs
    from settings import Settings
    cfg = tmp_path / 'config.toml'
    cfg.write_text(_read_fixture('grok', 'config.toml'))
    monkeypatch.setattr(agent_configs, 'GROK_CONFIG_PATH', str(cfg))
    from models import FOLLOW_DEFAULT, NATIVE_LABEL
    s = Settings(agent_default='grok')
    row = _make_row_with_settings(s, path='/tmp/grokproj')
    # The sidebar pushes the ccr/providers option list; a grok row REPLACES it
    # with grok's native models (this is what window._refresh_sidebar_models
    # drives for every row).
    row.set_model_options([('openrouter/foo', 'OpenRouter — Foo')],
                          FOLLOW_DEFAULT, NATIVE_LABEL)
    labels = _model_submenu_labels(row)
    targets = _model_submenu_targets(row)
    assert any('pool-qwen' in lbl for lbl in labels)
    assert 'pool-qwen' in targets   # the exact -m value GrokAdapter passes
    assert 'grok-4' in targets
    # The ccr option did NOT survive — the native list replaced it.
    assert 'openrouter/foo' not in targets


def test_claude_project_model_submenu_lists_ccr_options_unchanged(tmp_path):
    """REGRESSION (FB-1a): a claude project's submenu is the ccr/providers list —
    byte-identical to before (native resolution returns None for claude)."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL
    s = Settings(agent_default='claude')
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    # Push a ccr option list; for claude it must be shown verbatim.
    row.set_model_options([('openrouter/foo', 'OpenRouter — Foo')],
                          FOLLOW_DEFAULT, NATIVE_LABEL)
    targets = _model_submenu_targets(row)
    assert 'openrouter/foo' in targets
    # No grok/opencode native keys leak in.
    assert 'pool-qwen' not in targets


def _read_fixture(*parts):
    import os
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
    with open(os.path.join(base, *parts)) as f:
        return f.read()


# ===========================================================================
# P3.5e FB-8 (C9): the PAA button ships a BUNDLED symbolic icon, not a bare
# U+2728 Gtk.Label; the count/scanning indicator renders in an adjacent label.
# ===========================================================================

def test_paa_button_child_is_an_icon_not_a_text_label():
    """BINDING (FB-8): the sparkle button's child is a Gtk.Image bound to
    pm-sparkle-symbolic — NOT a bare glyph Gtk.Label (which rendered as tofu
    without an emoji font). Reverting to the Gtk.Label FAILS this."""
    sb = _make_sidebar()
    child = sb._paa_btn.get_child()
    assert isinstance(child, Gtk.Image)
    assert child.get_icon_name() == 'pm-sparkle-symbolic'
    # The button no longer carries a text label attribute at all.
    assert not hasattr(sb, '_paa_btn_label')


def test_paa_count_label_renders_count_and_scanning_states():
    """BINDING (FB-8): count/scanning render via the adjacent count label
    (string-pinned), and a clean state hides it (icon-only)."""
    sb = _make_sidebar()
    # Clean state: nothing pending, not scanning → label hidden, empty string.
    assert sb._paa_count_label.get_label() == ''
    assert sb._paa_count_label.get_visible() is False
    # Pending findings → the count shows.
    sb.set_paa_pending_count(3)
    assert sb._paa_count_label.get_label() == '3'
    assert sb._paa_count_label.get_visible() is True
    # Scanning adds the ⟳ glyph alongside the count.
    sb.set_paa_scanning('alpha, beta')
    assert '3' in sb._paa_count_label.get_label()
    assert '⟳' in sb._paa_count_label.get_label()
    # Scanning ends, count goes to zero → label hides again.
    sb.set_paa_scanning('')
    sb.set_paa_pending_count(0)
    assert sb._paa_count_label.get_label() == ''
    assert sb._paa_count_label.get_visible() is False
