# tests/test_sidebar_state.py
import types
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


def test_set_active_only_switches_host_to_active_mode():
    """Spawn/restore call sites flip only the affected host to active-only."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar

    class FakeStore:
        def load_projects(self):
            return [
                Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost'),
                Project(name='beta', path='/tmp/pm-beta', host_id='localhost'),
            ]

    settings = Settings()
    settings.set_section_mode('localhost', 'all')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)
    sb.set_active_only(True, path='/tmp/pm-alpha')
    assert settings.section_mode('localhost') == 'active'
    assert sb._section_headers['localhost']._filter_mode == 'active'


def test_set_active_only_false_reveals_only_active_host():
    """C7: spawn failure drops active-only on the failed host only."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar

    class FakeStore:
        def load_projects(self):
            return [
                Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost'),
            ]

    settings = Settings()
    settings.set_section_mode('localhost', 'active')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)
    sb.set_active_only(False, path='/tmp/pm-alpha')
    assert settings.section_mode('localhost') == 'all'


def test_set_active_only_paths_restore_touches_each_host():
    """Restore arms active-only per host that has sessions to reopen."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from hosts import encode_project_ref

    class FakeStore:
        def load_projects(self):
            return [
                Project(name='local', path='/tmp/pm-local', host_id='localhost'),
            ]

    settings = Settings(hosts={'bench': {'name': 'Bench', 'hostname': 'bench'}})
    settings.set_section_mode('localhost', 'all')
    settings.set_section_mode('bench', 'all')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)
    remote = encode_project_ref('bench', 'remote-proj')
    sb.set_active_only(True, paths=['/tmp/pm-local', remote])
    assert settings.section_mode('localhost') == 'active'
    assert settings.section_mode('bench') == 'active'


def test_sidebar_uses_per_host_sections_with_sticky_header():
    """Host chrome lives outside project ListBoxes; sticky pin exists."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar, HostSection, HostSectionHeader, ProjectRow

    class FakeStore:
        def load_projects(self):
            return [
                Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost'),
                Project(name='beta', path='/tmp/pm-beta', host_id='localhost'),
            ]

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    assert list(sb._sections.keys()) == ['localhost']
    section = sb._sections['localhost']
    assert isinstance(section, HostSection)
    assert isinstance(section.header, HostSectionHeader)
    assert section.header.get_parent() is section
    assert section.listbox.get_parent() is section
    # Header is not a list row; projects are.
    assert section.listbox.get_row_at_index(0) is not None
    assert isinstance(section.listbox.get_row_at_index(0), ProjectRow)
    assert sb._sticky_header is not None
    assert sb._sticky_header.get_visible() is False
    sb.select_project('/tmp/pm-beta')
    assert section.listbox.get_selected_row() is sb._rows['/tmp/pm-beta']


def test_rename_mode_ignores_focus_leave_until_armed():
    """Context-menu popover close must not cancel rename before the entry settles."""
    row = _make_row()
    exits = []
    orig = row._exit_rename_mode

    def tracked():
        exits.append(1)
        return orig()

    row._exit_rename_mode = tracked
    row._enter_rename_mode()
    assert row._rename_entry.get_visible() is True
    assert row._name_box.get_visible() is False
    assert row._rename_ignore_leave is True
    # Synthetic leave while still armed (popover chatter) must no-op.
    row._on_rename_focus_leave()
    assert exits == []
    assert row._rename_entry.get_visible() is True
    # After arming, leave cancels.
    row._rename_ignore_leave = False
    row._on_rename_focus_leave()
    assert exits == [1]
    assert row._rename_entry.get_visible() is False
    assert row._name_box.get_visible() is True


def test_rename_activate_emits_new_name():
    row = _make_row()
    got = []
    row.connect('project-rename', lambda r, name: got.append(name))
    row._enter_rename_mode()
    row._rename_entry.set_text('new-name')
    row._on_rename_activate(row._rename_entry)
    assert got == ['new-name']
    assert row._rename_entry.get_visible() is False


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
    import harnesses
    from settings import Settings
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    monkeypatch.setitem(harnesses.ADAPTERS, 'fake', _FakeCapsAdapter(caps, refs))
    s = Settings(harness_default='fake')
    proj = Project(name='test', path=path)
    return ProjectRow(proj, HistoryReader(), StatusWatcher(), settings=s)


def test_full_caps_adapter_shows_model_submenu_and_arrow(monkeypatch):
    import harnesses
    caps = harnesses.HarnessCaps(continue_=True, resume_by_id=True, sessions=True,
                            model_select=True)
    row = _row_with_adapter(monkeypatch, caps)
    assert 'Provider' in _menu_labels(row)
    assert row._arrow.get_visible() is True


def test_low_caps_adapter_hides_model_submenu(monkeypatch):
    import harnesses
    caps = harnesses.HarnessCaps(continue_=True, model_select=False, sessions=True,
                            resume_by_id=True)
    row = _row_with_adapter(monkeypatch, caps)
    assert 'Provider' not in _menu_labels(row)


def test_no_sessions_caps_hides_expander_arrow(monkeypatch):
    import harnesses
    caps = harnesses.HarnessCaps(continue_=True, sessions=False, resume_by_id=False,
                            model_select=True)
    row = _row_with_adapter(monkeypatch, caps)
    assert row._arrow.get_visible() is False


def test_expander_rows_come_from_adapter_list_sessions(monkeypatch):
    """A1: the expander renders SessionRefs from adapter.list_sessions, not the
    HistoryReader. SessionHistoryRow reads ref.id."""
    import harnesses
    from sidebar import SessionHistoryRow, NewSessionRow
    caps = harnesses.HarnessCaps(continue_=True, resume_by_id=True, sessions=True,
                            model_select=True)
    refs = [
        harnesses.SessionRef(id='sess-A', title='Alpha', last_active=2000),
        harnesses.SessionRef(id='sess-B', title='Beta', last_active=1000),
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
    import harnesses
    from sidebar import SessionHistoryRow, NewSessionRow
    caps = harnesses.HarnessCaps(continue_=True, resume_by_id=False, sessions=True,
                            model_select=True)
    refs = [harnesses.SessionRef(id='x', title='X', last_active=1)]
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
    import harnesses
    from sidebar import SessionHistoryRow
    caps = harnesses.HarnessCaps(continue_=True, resume_by_id=True, sessions=True)
    refs = [harnesses.SessionRef(id='the-id', title='T', last_active=5)]
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
# P2 Part B — B3 UI: Harness submenu, subtitle/badge, signal rename.
# ===========================================================================

def _make_row_with_settings(settings, path='/tmp/test'):
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    proj = Project(name='test', path=path)
    return ProjectRow(proj, HistoryReader(), StatusWatcher(), settings=settings)


def test_harness_submenu_lists_registered_adapters():
    """The Harness submenu offers one radio per adapter; default is marked."""
    from settings import Settings
    row = _make_row_with_settings(Settings(harness_default='claude'))
    labels = []
    from gi.repository import GLib
    for i in range(row._harness_submenu.get_n_items()):
        v = row._harness_submenu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
        if v:
            labels.append(v.get_string())
    assert not any(l.startswith('Follow default') for l in labels)
    assert any(l == 'Claude Code (default)' for l in labels)
    assert any('OpenCode' in l for l in labels)


def test_harness_submenu_lists_four_agents_including_kimi(monkeypatch):
    """Harness submenu lists all four first-class backends (incl. Kimi Code)."""
    from settings import Settings
    from gi.repository import GLib
    row = _make_row_with_settings(Settings(harness_default='claude'))
    labels = []
    for i in range(row._harness_submenu.get_n_items()):
        v = row._harness_submenu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
        if v:
            labels.append(v.get_string())
    assert len(labels) == 4
    assert any('Claude Code' in l for l in labels)
    assert any('OpenCode' in l for l in labels)
    assert any('Grok Build' in l for l in labels)
    assert any('Kimi Code' in l for l in labels)


def test_grok_override_selects_grok_adapter(monkeypatch):
    """T-B4: a per-project grok override resolves the row to the GrokAdapter
    (full caps → Provider submenu + expander arrow visible)."""
    from settings import Settings
    s = Settings(harness_default='claude', harness_overrides={'/tmp/g': 'grok'})
    row = _make_row_with_settings(s, path='/tmp/g')
    assert row._adapter().id == 'grok'
    assert 'Provider' in _menu_labels(row)
    assert row._arrow.get_visible() is True


def test_f9_settings_threaded_into_get_adapter(monkeypatch):
    """F9 / T-B4: the sidebar's get_adapter call now passes settings, so a
    named-but-missing agent gates on the M-P3.2 fallback (harness_default →
    first-available), NOT a hardcoded claude.

    Modelled against a claude-LESS fleet so 'falls back to the configured
    default' is provably the mechanism: harness_default=grok + a bogus override →
    the row resolves to grok (the default), not claude. Without threading
    settings, get_adapter('bogus') would return the legacy claude default and
    this would fail."""
    import harnesses
    from settings import Settings
    # Snapshot/restore ADAPTERS so the claude-less fleet doesn't leak.
    saved = dict(harnesses.ADAPTERS)
    try:
        s = Settings(harness_default='grok', harness_overrides={'/tmp/p': 'bogus'})
        row = _make_row_with_settings(s, path='/tmp/p')
        # effective harness for the project is the bogus override...
        assert s.effective_harness('/tmp/p') == 'bogus'
        # ...but the row's adapter is the settings-aware fallback: grok (the
        # configured default), never a hardcoded claude.
        assert row._adapter().id == 'grok'
    finally:
        harnesses.ADAPTERS.clear()
        harnesses.ADAPTERS.update(saved)


def test_f9_settings_threaded_first_available_when_default_also_bogus(monkeypatch):
    """F9: harness_default ALSO bogus → first-available registered adapter (still
    settings-aware, proven against a fleet with claude removed)."""
    import harnesses
    from settings import Settings
    saved = dict(harnesses.ADAPTERS)
    try:
        # Remove claude so 'first-available' is provably opencode, not claude.
        opencode = saved['opencode']
        harnesses.ADAPTERS.clear()
        harnesses.ADAPTERS['opencode'] = opencode
        harnesses.ADAPTERS.update({k: v for k, v in saved.items()
                                if k not in ('opencode', 'claude')})
        s = Settings(harness_default='alsobogus', harness_overrides={'/tmp/p': 'bogus'})
        row = _make_row_with_settings(s, path='/tmp/p')
        assert row._adapter().id == 'opencode'  # first-available, NOT claude
    finally:
        harnesses.ADAPTERS.clear()
        harnesses.ADAPTERS.update(saved)


def test_harness_submenu_present_in_menu():
    from settings import Settings
    row = _make_row_with_settings(Settings())
    assert 'Harness' in _menu_labels(row)


def test_agent_radio_reflects_override():
    from settings import Settings
    s = Settings(harness_default='claude', harness_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._harness_action.get_state().get_string() == 'opencode'


def test_agent_radio_follow_default_when_no_override():
    """No override → radio checks the concrete default harness id (not FOLLOW)."""
    from settings import Settings
    row = _make_row_with_settings(Settings(harness_default='opencode'), path='/tmp/p')
    assert row._harness_action.get_state().get_string() == 'opencode'


def test_agent_select_emits_change_signal():
    from settings import Settings
    from gi.repository import GLib
    row = _make_row_with_settings(Settings(), path='/tmp/p')
    got = []
    row.connect('project-harness-change', lambda r, aid: got.append(aid))
    row._on_harness_select(row._harness_action, GLib.Variant('s', 'opencode'))
    assert got == ['opencode']
    assert row._harness_action.get_state().get_string() == 'opencode'


def test_subtitle_hidden_for_plain_default():
    """Default harness + native model → no subtitle clutter."""
    from settings import Settings
    row = _make_row_with_settings(Settings(harness_default='claude'), path='/tmp/p')
    assert row._subtitle_label.get_visible() is False


def test_subtitle_shows_non_default_agent():
    from settings import Settings
    s = Settings(harness_default='claude', harness_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is True
    assert 'OpenCode' in row._subtitle_label.get_text()


def test_subtitle_shows_agent_and_model():
    from settings import Settings
    s = Settings(harness_default='opencode',
                 model_pins={'/tmp/p': 'ollama/qwen3.5:cloud'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is True
    txt = row._subtitle_label.get_text()
    assert 'OpenCode' in txt and 'ollama/qwen3.5:cloud' in txt


# ===========================================================================
# P3.5d Item 1 (FINDING 3 / C5): the subtitle tells the truth about NOW.
# A restored saved-harness-wins session (A2) can RUN a different agent than the
# one configured for the next session; the subtitle must lead with what is
# actually running. T1 = the mismatch string verbatim; T2 = byte-identical
# golden when running == configured (the pin that breaks if we always show the
# running form); T3 = no live session → today's string; T4 = model suffix in
# both shapes.
# ===========================================================================

def test_subtitle_running_harness_mismatch_leads_with_running():
    """T1: live child runs grok while the row is configured for opencode →
    '<Running> (next: <Configured>)'. Reverting the running-first builder (always
    showing the configured agent) yields 'opencode' and FAILS this."""
    from settings import Settings
    s = Settings(harness_default='claude', harness_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_harness('grok')
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'Grok Build (next: OpenCode)'


def test_subtitle_running_equals_configured_is_byte_identical():
    """T2 (GOLDEN pin): running == configured → today's exact string, byte for
    byte. If the builder ALWAYS rendered the running-first form this would read
    'opencode (next: opencode)' and FAIL."""
    from settings import Settings
    s = Settings(harness_default='claude', harness_overrides={'/tmp/p': 'opencode'})
    # Baseline: no running harness → today's string.
    baseline = _make_row_with_settings(s, path='/tmp/p')
    golden = baseline._subtitle_label.get_text()
    assert golden == 'OpenCode'
    # Running harness EQUALS the configured one → identical to the baseline.
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_harness('opencode')
    assert row._subtitle_label.get_text() == golden
    assert row._subtitle_label.get_text() == 'OpenCode'


def test_subtitle_no_running_session_is_todays_string():
    """T3: no live session (running is None) → today's string unchanged, and a
    plain default row stays clean (no subtitle)."""
    from settings import Settings
    # Non-default harness, no running session → shows the configured agent.
    s = Settings(harness_default='claude', harness_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._running_harness is None
    assert row._subtitle_label.get_text() == 'OpenCode'
    # Plain default + no model + no running session → hidden (clean).
    plain = _make_row_with_settings(Settings(harness_default='claude'), path='/tmp/q')
    assert plain._subtitle_label.get_visible() is False


def test_subtitle_model_suffix_preserved_in_both_shapes():
    """T4: the ' · <model>' suffix is preserved in BOTH the byte-identical shape
    and the running-mismatch shape."""
    from settings import Settings
    s = Settings(harness_default='claude',
                 harness_overrides={'/tmp/p': 'opencode'},
                 model_pins={'/tmp/p': 'ollama/qwen3.5:cloud'})
    # No mismatch → today's 'agent · model' shape, byte-identical.
    matched = _make_row_with_settings(s, path='/tmp/p')
    assert matched._subtitle_label.get_text() == 'OpenCode · ollama/qwen3.5:cloud'
    # Mismatch → running-first head, model suffix still appended.
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_harness('grok')
    assert (row._subtitle_label.get_text()
            == 'Grok Build (next: OpenCode) · ollama/qwen3.5:cloud')


def test_subtitle_mismatch_overrides_clean_default_hide():
    """C5 corollary: a default-configured row (normally hidden) still shows the
    truth when a live child runs a NON-default harness."""
    from settings import Settings
    s = Settings(harness_default='claude')          # default → normally hidden
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is False   # clean while idle
    row.set_running_harness('grok')                  # live child runs grok
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'Grok Build (next: Claude Code)'


def test_set_running_harness_none_restores_clean_subtitle():
    """Clearing the running harness (session ended) restores the configured-only
    subtitle — the mismatch form is gone."""
    from settings import Settings
    s = Settings(harness_default='claude', harness_overrides={'/tmp/p': 'opencode'})
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_harness('grok')
    assert row._subtitle_label.get_text() == 'Grok Build (next: OpenCode)'
    row.set_running_harness(None)
    assert row._subtitle_label.get_text() == 'OpenCode'


def test_sidebar_set_running_harness_unknown_path_is_noop():
    """Sidebar.set_running_harness for a path with no row must not raise (window.py
    fires it unconditionally)."""
    from settings import Settings
    from sidebar import Sidebar
    from model import HistoryReader, StatusWatcher

    class _EmptyStore:
        def load_projects(self):
            return []

    sb = Sidebar(_EmptyStore(), HistoryReader(), StatusWatcher(),
                 settings=Settings())
    sb.set_running_harness('/no/such/path', 'grok')   # must be a silent no-op


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
    import harnesses
    caps = harnesses.HarnessCaps(continue_=True, rich_status=False)
    row = _row_with_adapter(monkeypatch, caps)
    row.set_process_state('attached')
    assert row._status_dot.has_css_class('status-idle')
    assert not row._status_dot.has_css_class('status-done')


def test_attached_idle_dot_remaps_to_done_for_rich_status_true(monkeypatch):
    """T6 — no-regression pin: rich_status=True keeps today's behavior — an
    attached row with no status file yet renders status-done (green)."""
    import harnesses
    caps = harnesses.HarnessCaps(continue_=True, rich_status=True)
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
    import harnesses
    from settings import Settings
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    adapter = _ExplodingCapsAdapter(
        harnesses.HarnessCaps(continue_=True, rich_status=False))
    monkeypatch.setitem(harnesses.ADAPTERS, 'fake', adapter)
    row = ProjectRow(Project(name='test', path='/tmp/test'),
                     HistoryReader(), StatusWatcher(),
                     settings=Settings(harness_default='fake'))
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
    """S12 audit: header icon controls (host + menu + PAA sparkle) have tooltips."""
    from sidebar import _TIP_ADD_HOST
    sb = _make_sidebar()
    assert sb._paa_btn.get_tooltip_text() == 'Projects Admin Agent'
    # Host-line + MenuButton: explanatory Project vs Group tooltip
    found = _find_control_with_tooltip(sb, _TIP_ADD_HOST)
    assert found, 'the host + MenuButton has no tooltip'


def test_settings_gear_has_tooltip():
    sb = _make_sidebar()
    assert _find_control_with_tooltip(sb, 'Settings'), 'Settings gear has no tooltip'


def _find_control_with_tooltip(widget, tooltip):
    if isinstance(widget, (Gtk.Button, Gtk.MenuButton)):
        if widget.get_tooltip_text() == tooltip:
            return True
    child = widget.get_first_child() if hasattr(widget, 'get_first_child') else None
    while child is not None:
        if _find_control_with_tooltip(child, tooltip):
            return True
        child = child.get_next_sibling()
    return False


# ===========================================================================
# P3.5e FB-1a: the per-project Provider submenu lists the EFFECTIVE harness's
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
    """The set-model target VALUES (what a pick writes to provider_overrides)."""
    from gi.repository import GLib
    out = []
    for i in range(row._model_submenu.get_n_items()):
        v = row._model_submenu.get_item_attribute_value(i, 'target', GLib.VariantType('s'))
        if v:
            out.append(v.get_string())
    return out


def test_grok_project_model_submenu_lists_native_models(tmp_path, monkeypatch):
    """Grok harness: only Grok (native) — no other natives or customs listed."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, NATIVE_GROK
    s = Settings(
        harness_default='grok',
        providers={'ollama': {'name': 'Ollama', 'base_url': 'http://x', 'models': []}},
    )
    row = _make_row_with_settings(s, path='/tmp/grokproj')
    row.set_model_options([], FOLLOW_DEFAULT, NATIVE_LABEL)
    labels = _model_submenu_labels(row)
    targets = _model_submenu_targets_all(row)
    assert labels == ['Grok (native)']
    assert targets == [NATIVE_GROK]
    assert row._model_action.get_state().get_string() == NATIVE_GROK


def test_claude_project_model_submenu_lists_ccr_options_unchanged(tmp_path):
    """Claude harness: Anthropic + customs only; Settings default is checked."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, NATIVE_GROK
    s = Settings(
        harness_default='claude',
        model_default='ollama',
        providers={'ollama': {'name': 'Ollama', 'base_url': 'http://x', 'models': []}},
    )
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    row.set_model_options([], FOLLOW_DEFAULT, NATIVE_LABEL)
    targets = _model_submenu_targets_all(row)
    labels = _model_submenu_labels(row)
    assert '' in targets  # Anthropic native
    assert 'ollama' in targets
    assert NATIVE_GROK not in targets
    assert 'Grok (native)' not in labels
    assert row._model_action.get_state().get_string() == 'ollama'


def test_claude_override_row_default_label_is_per_row_not_global(tmp_path, monkeypatch):
    """Claude override on a grok-default bench: Anthropic menu, not Grok native."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, NATIVE_GROK
    s = Settings(harness_default='grok',
                 harness_overrides={'/tmp/claudeoverride': 'claude'})
    row = _make_row_with_settings(s, path='/tmp/claudeoverride')
    row.set_model_options([], FOLLOW_DEFAULT, NATIVE_LABEL)
    targets = _model_submenu_targets_all(row)
    labels = _model_submenu_labels(row)
    assert '' in targets
    assert NATIVE_GROK not in targets
    assert 'Grok (native)' not in labels
    assert row._model_action.get_state().get_string() == ''


def test_grok_row_default_label_keeps_grok_story(tmp_path, monkeypatch):
    """Grok project on a grok bench keeps Grok (native) as the only/checked item."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, NATIVE_GROK
    s = Settings(harness_default='grok')
    row = _make_row_with_settings(s, path='/tmp/grokproj')
    row.set_model_options([], FOLLOW_DEFAULT, NATIVE_LABEL)
    assert row._model_action.get_state().get_string() == NATIVE_GROK
    assert _model_submenu_labels(row) == ['Grok (native)']


# ===========================================================================
# G1 (reveal-3 item 1, C2/C4): the Provider submenu must not offer the same
# choice twice. A native option whose LABEL restates the Default story is a
# redundant pin the user hasn't taken — suppress it UNLESS it is the live
# selection (a pin the user DID take stays visible and checked).
# ===========================================================================

def _model_submenu_targets_all(row):
    """Like _model_submenu_targets but None-safe: the native sentinel's target
    is the EMPTY string '', whose GLib.Variant is falsy — the `if v` form above
    would silently drop it. Test the native-sentinel presence with this."""
    from gi.repository import GLib
    out = []
    for i in range(row._model_submenu.get_n_items()):
        v = row._model_submenu.get_item_attribute_value(i, 'target', GLib.VariantType('s'))
        if v is not None:
            out.append(v.get_string())
    return out


@pytest.mark.skip(reason="G1 model-list dedup rewritten for provider axis; re-enable with provider menu cases")
def test_g1a_claude_no_providers_default_native_dedups(monkeypatch):
    """T-G1a (the verbatim repro): claude effective harness, NO providers, global
    default native, current=FOLLOW_DEFAULT → the submenu contains EXACTLY one
    item, 'Default (Anthropic (native Claude))'. The bare native sentinel that
    duplicated the Default story is gone. Reverting the suppression FAILS here
    (the bare 'Anthropic (native Claude)' entry reappears)."""
    import harness_configs
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    # claude default, no providers → ccr option list is just the native sentinel.
    s = Settings(harness_default='claude', providers={})
    ids, labels = build_provider_options(s.providers)
    options = list(zip(ids, labels))           # [('', 'Anthropic (native Claude)')]
    assert options == [('', NATIVE_LABEL)]
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    row.set_model_options(options, FOLLOW_DEFAULT, NATIVE_LABEL)
    menu = _model_submenu_labels(row)
    assert menu == [f'Default ({NATIVE_LABEL})']
    # No bare native entry survives.
    assert NATIVE_LABEL not in menu
    assert _model_submenu_targets(row) == [FOLLOW_DEFAULT]


@pytest.mark.skip(reason="G1 model-list dedup rewritten for provider axis; re-enable with provider menu cases")
def test_g1b_providers_native_suppressed_provider_entries_intact():
    """T-G1b: providers configured, global default native → menu = Default +
    provider entries; the native sentinel (whose label == the Default story) is
    suppressed; the provider entries survive intact."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    providers = {
        'openrouter': {'name': 'OpenRouter', 'base_url': '', 'api_key': '',
                       'models': {'foo': {'name': 'Foo'}}},
    }
    s = Settings(harness_default='claude', providers=providers)
    options = [(i, l) for i, l in zip(*build_provider_options(s.providers))]
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    row.set_model_options(options, FOLLOW_DEFAULT, NATIVE_LABEL)
    targets = _model_submenu_targets(row)
    labels = _model_submenu_labels(row)
    # native sentinel ('') suppressed; provider entry intact.
    assert '' not in targets
    assert 'openrouter/foo' in targets
    assert NATIVE_LABEL not in labels        # only inside the 'Default (…)' label
    assert labels[0] == f'Default ({NATIVE_LABEL})'


@pytest.mark.skip(reason="G1 model-list dedup rewritten for provider axis; re-enable with provider menu cases")
def test_g1c_default_is_provider_model_native_sentinel_present():
    """T-G1c: the Default story resolves to a PROVIDER model's label → the native
    sentinel no longer duplicates and IS present; the provider entry whose label
    equals the Default story is the one suppressed.

    Uses a settings-less row so the row's Default label is exactly the pushed
    ``global_label`` (a claude-settings row always tells the native story per
    P3.5f, which would re-suppress the native sentinel — not the case under
    test). build_provider_options gives the native sentinel + the provider entry."""
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    providers = {
        'openrouter': {'name': 'OpenRouter', 'base_url': '', 'api_key': '',
                       'models': {'foo': {'name': 'Foo'}}},
    }
    options = [(i, l) for i, l in zip(*build_provider_options(providers))]
    story = 'OpenRouter — Foo'                 # the global default's resolved label
    row = _make_row()                          # no settings → Default label == global_label
    row.set_model_options(options, FOLLOW_DEFAULT, story)
    targets = _model_submenu_targets_all(row)
    labels = _model_submenu_labels(row)
    # The native sentinel is present (its label != the provider Default story).
    assert '' in targets
    assert NATIVE_LABEL in labels
    # The provider entry whose label == the Default story is suppressed.
    assert 'openrouter/foo' not in targets
    assert labels[0] == f'Default ({story})'


@pytest.mark.skip(reason="G1 model-list dedup rewritten for provider axis; re-enable with provider menu cases")
def test_g1d_live_pin_to_suppressed_id_stays_present_and_checked():
    """T-G1d: when current is PINNED to the would-be-suppressed id, the entry is
    PRESENT and the action state points at it — a pin the user took must stay
    visible and checked, never silently dropped."""
    from settings import Settings
    from models import NATIVE_LABEL, build_provider_options
    s = Settings(harness_default='claude', providers={})
    options = list(zip(*build_provider_options(s.providers)))  # [('', NATIVE_LABEL)]
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    # current pinned to '' (the native sentinel that equals the Default story).
    row.set_model_options(options, '', NATIVE_LABEL)
    targets = _model_submenu_targets_all(row)
    assert '' in targets                      # the pinned entry survives
    assert row._model_action.get_state().get_string() == ''  # and is the active state


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


# ===========================================================================
# Deferred deactivate + UNDO grace period
# ===========================================================================

def _sidebar_with_projects(paths=None):
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar

    paths = paths or ['/tmp/pm-alpha']
    names = [p.rsplit('/', 1)[-1] for p in paths]

    class FakeStore:
        def load_projects(self):
            return [
                Project(name=n, path=p, host_id='localhost')
                for n, p in zip(names, paths)
            ]

    settings = Settings()
    return Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)


def test_pending_deactivate_shows_italic_and_undo():
    row = _make_row()
    row.set_process_state('attached')
    row.set_pending_deactivate(True)
    # Row class → theme bg wash; name label class → italic.
    assert row.has_css_class('project-row-pending-deactivate')
    assert row._name_label.has_css_class('project-row-pending-deactivate')
    assert row._actions_box.has_css_class('project-row-actions-pending')
    # Hover-hide class must be gone so UNDO stays visible without hover.
    assert not row._actions_box.has_css_class('project-row-actions')
    assert row._undo_btn.get_visible() is True
    assert row._deactivate_btn.get_visible() is False
    assert row._process_state == 'attached'


def test_undo_clears_pending_ui():
    row = _make_row()
    row.set_process_state('attached')
    row.set_pending_deactivate(True)
    row.set_pending_deactivate(False)
    assert not row.has_css_class('project-row-pending-deactivate')
    assert not row._name_label.has_css_class('project-row-pending-deactivate')
    assert not row._actions_box.has_css_class('project-row-actions-pending')
    # Hover-hide restored after UNDO / cancel.
    assert row._actions_box.has_css_class('project-row-actions')
    assert row._undo_btn.get_visible() is False
    assert row._deactivate_btn.get_visible() is True
    assert row._deactivate_btn.get_sensitive() is True


def test_begin_pending_deactivate_schedules_timer_not_immediate(monkeypatch):
    captured = {}

    def fake_timeout_add(ms, fn, *args):
        captured['ms'] = ms
        captured['fn'] = fn
        captured['args'] = args
        return 99

    monkeypatch.setattr('sidebar.GLib.timeout_add', fake_timeout_add)
    sb = _sidebar_with_projects(['/tmp/pm-alpha'])
    path = '/tmp/pm-alpha'
    sb.set_project_state(path, 'attached')
    emitted = []
    sb.connect('project-deactivate', lambda s, p: emitted.append(p))

    sb._begin_pending_deactivate(path)

    assert path in sb._pending_deactivates
    assert emitted == []
    assert captured['ms'] == sb.PENDING_DEACTIVATE_MS
    assert captured['args'] == (path,)
    row = sb._rows[path]
    assert row._pending_deactivate is True


def test_fire_pending_deactivate_emits_signal(monkeypatch):
    monkeypatch.setattr('sidebar.GLib.source_remove', lambda tid: None)
    sb = _sidebar_with_projects(['/tmp/pm-alpha'])
    path = '/tmp/pm-alpha'
    sb.set_project_state(path, 'attached')
    sb._pending_deactivates.add(path)
    sb._rows[path].set_pending_deactivate(True)
    emitted = []
    sb.connect('project-deactivate', lambda s, p: emitted.append(p))

    sb._fire_pending_deactivate(path)

    assert emitted == [path]
    assert path not in sb._pending_deactivates
    assert sb._rows[path]._pending_deactivate is False


def test_cancel_pending_deactivate_on_undo(monkeypatch):
    removed = []
    monkeypatch.setattr('sidebar.GLib.source_remove',
                        lambda tid: removed.append(tid))
    monkeypatch.setattr('sidebar.GLib.timeout_add', lambda *a, **k: 77)

    sb = _sidebar_with_projects(['/tmp/pm-alpha'])
    path = '/tmp/pm-alpha'
    sb.set_project_state(path, 'attached')
    sb._begin_pending_deactivate(path)
    sb.cancel_pending_deactivate(path)

    assert path not in sb._pending_deactivates
    assert removed == [77]
    assert sb._rows[path]._pending_deactivate is False


def test_set_project_state_inactive_clears_pending(monkeypatch):
    monkeypatch.setattr('sidebar.GLib.source_remove', lambda tid: None)
    monkeypatch.setattr('sidebar.GLib.timeout_add', lambda *a, **k: 1)

    sb = _sidebar_with_projects(['/tmp/pm-alpha'])
    path = '/tmp/pm-alpha'
    sb.set_project_state(path, 'attached')
    sb._begin_pending_deactivate(path)
    sb.set_project_state(path, 'inactive')

    assert path not in sb._pending_deactivates
    assert sb._rows[path]._pending_deactivate is False


def test_pending_deactivate_survives_populate(monkeypatch):
    monkeypatch.setattr('sidebar.GLib.source_remove', lambda tid: None)
    monkeypatch.setattr('sidebar.GLib.timeout_add', lambda *a, **k: 5)

    sb = _sidebar_with_projects(['/tmp/pm-alpha'])
    path = '/tmp/pm-alpha'
    sb.set_project_state(path, 'attached')
    sb._begin_pending_deactivate(path)
    sb.refresh()

    row = sb._rows[path]
    assert path in sb._pending_deactivates
    assert row._pending_deactivate is True
    assert row._undo_btn.get_visible() is True
    assert row._process_state == 'attached'


def test_active_filter_keeps_pending_row_visible(monkeypatch):
    monkeypatch.setattr('sidebar.GLib.timeout_add', lambda *a, **k: 1)
    monkeypatch.setattr('sidebar.GLib.source_remove', lambda tid: None)

    sb = _sidebar_with_projects(['/tmp/pm-alpha', '/tmp/pm-beta'])
    sb.set_host_section_mode('localhost', 'active')
    sb.set_project_state('/tmp/pm-alpha', 'attached')
    sb.set_project_state('/tmp/pm-beta', 'inactive')
    sb._begin_pending_deactivate('/tmp/pm-alpha')

    filt = sb._filter_func_for('localhost')
    assert filt(sb._rows['/tmp/pm-alpha']) is True
    assert filt(sb._rows['/tmp/pm-beta']) is False


def test_deactivate_signals_exist():
    from sidebar import ProjectRow
    assert GObject_signal_exists(ProjectRow, 'deactivate-requested')
    assert GObject_signal_exists(ProjectRow, 'deactivate-undo')


def test_on_project_deactivate_without_terminal_sets_inactive():
    """Timer-fired deactivate with no TerminalView must not leave a stuck row."""
    from window import AppWindow
    states = []
    fake = types.SimpleNamespace(
        _terminals={},
        _sidebar=types.SimpleNamespace(
            cancel_pending_deactivate=lambda p: None,
            set_project_state=lambda p, s, is_zellij=False: states.append((p, s)),
        ),
    )
    AppWindow._on_project_deactivate(fake, None, '/missing')
    assert states == [('/missing', 'inactive')]


def test_spawn_begin_cancels_pending_deactivate(monkeypatch):
    """Respawn must cancel grace before kill so the timer cannot hit the new child."""
    monkeypatch.setattr('sidebar.GLib.source_remove', lambda tid: None)
    monkeypatch.setattr('sidebar.GLib.timeout_add', lambda *a, **k: 42)

    sb = _sidebar_with_projects(['/tmp/pm-alpha'])
    path = '/tmp/pm-alpha'
    sb.set_project_state(path, 'attached')
    sb._begin_pending_deactivate(path)

    # window.py wires spawn-begin → cancel_pending_deactivate
    sb.cancel_pending_deactivate(path)

    assert path not in sb._pending_deactivates
    assert sb._rows[path]._pending_deactivate is False


def test_migrate_pending_deactivate_rewrites_path(monkeypatch):
    monkeypatch.setattr('sidebar.GLib.source_remove', lambda tid: None)
    added = []
    monkeypatch.setattr('sidebar.GLib.timeout_add',
                        lambda ms, fn, path: added.append(path) or 3)

    sb = _sidebar_with_projects(['/tmp/old'])
    sb._pending_deactivates.add('/tmp/old')
    sb._pending_deactivate_timers['/tmp/old'] = 2
    sb.migrate_pending_deactivate('/tmp/old', '/tmp/new')

    assert '/tmp/old' not in sb._pending_deactivates
    assert '/tmp/new' in sb._pending_deactivates
    assert added == ['/tmp/new']


# ===========================================================================
# Slice C — virtual project groups in the sidebar
# ===========================================================================

def test_empty_forest_is_flat_project_list():
    """No groups → project rows only, same count as store projects."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar, GroupRow, ProjectRow

    class FakeStore:
        def load_projects(self):
            return [
                Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost'),
                Project(name='beta', path='/tmp/pm-beta', host_id='localhost'),
            ]

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    assert len(sb._rows) == 2
    assert sb._group_rows == {}
    section = sb._sections['localhost']
    # Host listbox children are ProjectRows only (no GroupRows).
    for i in range(2):
        row = section.listbox.get_row_at_index(i)
        assert isinstance(row, ProjectRow)
        assert not isinstance(row, GroupRow)


def test_group_forest_nests_project_under_group():
    """One group + membership → GroupRow present; project in nested listbox."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar, GroupRow, ProjectRow
    from project_groups import empty_forest, add_group, set_membership

    alpha = Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')
    beta = Project(name='beta', path='/tmp/pm-beta', host_id='localhost')

    class FakeStore:
        def load_projects(self):
            return [alpha, beta]

    forest = empty_forest()
    g = add_group(forest, 'Work')
    assert set_membership(forest, alpha.project_ref, g.id)

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    assert ('localhost', g.id) in sb._group_rows
    grow = sb._group_rows[('localhost', g.id)]
    assert isinstance(grow, GroupRow)
    assert grow._name_label.get_label() == 'Work'
    assert '/tmp/pm-alpha' in sb._rows
    assert '/tmp/pm-beta' in sb._rows
    # Grouped project lives under the group's nested listbox.
    assert sb._rows['/tmp/pm-alpha'].get_parent() is grow.child_listbox
    assert isinstance(sb._rows['/tmp/pm-alpha'], ProjectRow)
    # Ungrouped stays on the host listbox.
    assert sb._rows['/tmp/pm-beta'].get_parent() is sb._sections['localhost'].listbox
    # Nested selection still works via _rows.
    sb.select_project('/tmp/pm-alpha')
    assert grow.child_listbox.get_selected_row() is sb._rows['/tmp/pm-alpha']


def test_group_expand_toggles_forest_and_emits_signal():
    """Toggle updates forest.groups[id].expanded and emits group-expanded."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group, set_membership

    proj = Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')

    class FakeStore:
        def load_projects(self):
            return [proj]

    forest = empty_forest()
    g = add_group(forest, 'Work')
    set_membership(forest, proj.project_ref, g.id)

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    got = []
    sb.connect('group-expanded',
               lambda s, hid, gid, exp: got.append((hid, gid, exp)))

    grow = sb._group_rows[('localhost', g.id)]
    assert forest.groups[g.id].expanded is True
    assert grow._expanded is True

    grow.toggle_expanded()
    assert forest.groups[g.id].expanded is False
    assert grow._expanded is False
    assert grow._revealer.get_reveal_child() is False
    assert got == [('localhost', g.id, False)]

    grow.toggle_expanded()
    assert forest.groups[g.id].expanded is True
    assert got[-1] == ('localhost', g.id, True)


def test_group_row_activate_does_not_emit_project_activated():
    """Activating a GroupRow toggles expand only — never project-activated."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group

    class FakeStore:
        def load_projects(self):
            return [Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')]

    forest = empty_forest()
    g = add_group(forest, 'Empty-ish')

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    activated = []
    sb.connect('project-activated', lambda s, p: activated.append(p))
    grow = sb._group_rows[('localhost', g.id)]
    was = grow._expanded
    sb._on_row_activated(sb._sections['localhost'].listbox, grow)
    assert grow._expanded is not was
    assert activated == []


def test_set_remote_projects_rebuild_false_batches():
    """rebuild=False only caches; does not populate until caller refreshes."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from hosts import encode_project_ref, HostProfile

    class FakeStore:
        def load_projects(self):
            return []

    settings = Settings()
    settings.hosts = {
        'bench': {'id': 'bench', 'ssh_target': 'b', 'display_name': 'bench'},
        'lab': {'id': 'lab', 'ssh_target': 'l', 'display_name': 'lab'},
    }

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)
    h1 = [
        Project(name='a', path=encode_project_ref('bench', 'a'),
                host_id='bench', remote_cwd='~/a'),
    ]
    h2 = [
        Project(name='b', path=encode_project_ref('lab', 'b'),
                host_id='lab', remote_cwd='~/b'),
    ]
    assert sb.set_remote_projects('bench', h1, rebuild=False) is True
    assert sb.set_remote_projects('lab', h2, rebuild=False) is True
    # Not rebuilt yet — remote rows not in _rows until refresh
    assert encode_project_ref('bench', 'a') not in sb._rows
    assert encode_project_ref('lab', 'b') not in sb._rows
    sb.refresh()
    assert encode_project_ref('bench', 'a') in sb._rows
    assert encode_project_ref('lab', 'b') in sb._rows
    # Unchanged names → False, no need to rebuild
    assert sb.set_remote_projects('bench', h1, rebuild=False) is False


def test_localhost_groups_load_status_exposed():
    """Sidebar startup load exposes status so window need not re-load/refresh."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar

    class FakeStore:
        def load_projects(self):
            return []

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    status, err = sb.localhost_groups_load_status()
    assert status in ('ok', 'missing', 'invalid', 'error')
    # conftest isolates DEFAULT_GROUPS_PATH to empty temp → missing
    assert status == 'missing'
    assert err is None
    assert sb.get_group_forest('localhost') is not None


def test_set_get_group_forest_by_reference():
    """set_group_forest stores by reference; expand mutates caller's forest."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group

    class FakeStore:
        def load_projects(self):
            return []

    forest = empty_forest()
    g = add_group(forest, 'G')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    assert sb.get_group_forest('localhost') is forest
    sb.set_group_forest('bench', None)
    assert sb.get_group_forest('bench') is not None
    assert sb.get_group_forest('bench').groups == {}


def test_group_rows_keyed_by_host_and_group_id():
    """Same group_id on two hosts → two GroupRows; no overwrite."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group, set_membership
    from hosts import encode_project_ref

    local_proj = Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')
    remote_ref = encode_project_ref('bench', 'remote-alpha')
    remote_proj = Project(
        name='remote-alpha', path=remote_ref, host_id='bench',
        remote_cwd='/home/user/projects/remote-alpha',
    )

    class FakeStore:
        def load_projects(self):
            return [local_proj]

    shared_gid = 'same-group-id-across-hosts'
    local_forest = empty_forest()
    g_local = add_group(local_forest, 'Local Work', group_id=shared_gid)
    set_membership(local_forest, local_proj.project_ref, g_local.id)

    remote_forest = empty_forest()
    g_remote = add_group(remote_forest, 'Remote Work', group_id=shared_gid)
    set_membership(remote_forest, remote_proj.project_ref, g_remote.id)

    settings = Settings(hosts={
        'bench': {
            'id': 'bench',
            'ssh_target': 'user@bench',
            'display_name': 'Bench',
        },
    })
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)
    sb.set_group_forest('localhost', local_forest)
    sb.set_group_forest('bench', remote_forest)
    sb.set_remote_projects('bench', [remote_proj])
    sb.refresh()

    assert ('localhost', shared_gid) in sb._group_rows
    assert ('bench', shared_gid) in sb._group_rows
    grow_local = sb._group_rows[('localhost', shared_gid)]
    grow_remote = sb._group_rows[('bench', shared_gid)]
    assert grow_local is not grow_remote
    assert grow_local.host_id == 'localhost'
    assert grow_remote.host_id == 'bench'
    assert grow_local._name_label.get_label() == 'Local Work'
    assert grow_remote._name_label.get_label() == 'Remote Work'
    assert grow_local.group_id == grow_remote.group_id == shared_gid


def test_select_project_expands_collapsed_ancestor_groups():
    """select_project on a nested project expands collapsed GroupRow ancestors.

    Durable expand is updated, but group-expanded fires **once per host**
    (not once per ancestor) so remote persist is a single push.
    """
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import (
        empty_forest, add_group, set_membership, set_group_expanded,
    )

    proj = Project(name='nested', path='/tmp/pm-nested', host_id='localhost')

    class FakeStore:
        def load_projects(self):
            return [proj]

    forest = empty_forest()
    parent = add_group(forest, 'Parent')
    child = add_group(forest, 'Child', parent_id=parent.id)
    set_membership(forest, proj.project_ref, child.id)
    set_group_expanded(forest, parent.id, False)
    set_group_expanded(forest, child.id, False)

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    grow_parent = sb._group_rows[('localhost', parent.id)]
    grow_child = sb._group_rows[('localhost', child.id)]
    assert grow_parent._revealer.get_reveal_child() is False
    assert grow_child._revealer.get_reveal_child() is False
    assert forest.groups[parent.id].expanded is False
    assert forest.groups[child.id].expanded is False

    got = []
    sb.connect('group-expanded',
               lambda s, hid, gid, exp: got.append((hid, gid, exp)))

    sb.select_project('/tmp/pm-nested')

    assert grow_parent._revealer.get_reveal_child() is True
    assert grow_child._revealer.get_reveal_child() is True
    # select_project persists expand so the path stays visible after filters clear
    assert forest.groups[parent.id].expanded is True
    assert forest.groups[child.id].expanded is True
    assert grow_child.child_listbox.get_selected_row() is sb._rows['/tmp/pm-nested']
    # One batched emit per host — not one per ancestor (would be 2 here).
    assert got == [('localhost', '', True)]


def test_name_filter_auto_expands_group_without_persisting():
    """Name filter shows group and ephemerally reveals matching descendants."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import (
        empty_forest, add_group, set_membership, set_group_expanded,
    )

    alpha = Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')
    beta = Project(name='beta', path='/tmp/pm-beta', host_id='localhost')

    class FakeStore:
        def load_projects(self):
            return [alpha, beta]

    forest = empty_forest()
    g = add_group(forest, 'Work')
    set_membership(forest, alpha.project_ref, g.id)
    set_group_expanded(forest, g.id, False)

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    grow = sb._group_rows[('localhost', g.id)]
    assert grow._revealer.get_reveal_child() is False
    assert forest.groups[g.id].expanded is False

    got = []
    sb.connect('group-expanded',
               lambda s, hid, gid, exp: got.append((hid, gid, exp)))

    sb.set_filter_text('alp')

    # Group stays addressable and is shown by the host filter.
    filt = sb._filter_func_for('localhost')
    assert filt(grow) is True
    assert filt(sb._rows['/tmp/pm-alpha']) is True
    assert filt(sb._rows['/tmp/pm-beta']) is False
    # Ephemeral reveal so the matching project is visible under the group.
    assert grow._revealer.get_reveal_child() is True
    assert grow._expanded is True
    # Durable forest state and signal must not change on filter keystrokes.
    assert forest.groups[g.id].expanded is False
    assert got == []

    # Clearing the filter restores the durable collapsed state.
    sb.set_filter_text('')
    assert grow._revealer.get_reveal_child() is False
    assert forest.groups[g.id].expanded is False
    assert got == []


# ===========================================================================
# Slice D — group menus, move options, create-in-group signals
# ===========================================================================

def test_group_row_context_menu_has_actions():
    """GroupRow menu exposes New Project / Subgroup / Rename / Delete."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar, GroupRow
    from project_groups import empty_forest, add_group, MAX_GROUP_DEPTH

    class FakeStore:
        def load_projects(self):
            return []

    forest = empty_forest()
    g = add_group(forest, 'Work')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()
    from gi.repository import GLib
    grow = sb._group_rows[('localhost', g.id)]
    assert isinstance(grow, GroupRow)
    assert hasattr(grow, '_menu')
    labels = []
    for i in range(grow._menu.get_n_items()):
        v = grow._menu.get_item_attribute_value(
            i, 'label', GLib.VariantType('s'))
        if v is not None:
            labels.append(v.get_string())
    assert 'New Project\u2026' in labels
    assert 'New Subgroup\u2026' in labels
    assert 'Rename' in labels
    assert 'Delete Group' in labels
    # Depth 1 of 5 → subgroup allowed
    assert grow._new_subgroup_action.get_enabled() is True


def test_group_row_new_subgroup_disabled_at_max_depth():
    """New Subgroup action disabled when group is already at MAX_GROUP_DEPTH."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group, MAX_GROUP_DEPTH

    class FakeStore:
        def load_projects(self):
            return []

    forest = empty_forest()
    parent = None
    for i in range(MAX_GROUP_DEPTH):
        node = add_group(forest, f'L{i+1}', parent_id=parent)
        parent = node.id
    deep_id = parent
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()
    grow = sb._group_rows[('localhost', deep_id)]
    assert grow._new_subgroup_action.get_enabled() is False


def test_project_row_move_to_group_options():
    """set_group_move_options builds breadcrumb submenu; emits project-move-to-group."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group, set_membership

    alpha = Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')

    class FakeStore:
        def load_projects(self):
            return [alpha]

    forest = empty_forest()
    parent = add_group(forest, 'Parent')
    child = add_group(forest, 'Child', parent_id=parent.id)

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    opts = sb._group_move_options('localhost')
    ids = [o[0] for o in opts]
    assert '' in ids  # Ungrouped
    assert parent.id in ids
    assert child.id in ids
    labels = {o[0]: o[1] for o in opts}
    assert labels[child.id] == 'Parent / Child'

    from gi.repository import GLib
    row = sb._rows['/tmp/pm-alpha']
    # Submenu present after options applied
    found_move = False
    for i in range(row._menu.get_n_items()):
        v = row._menu.get_item_attribute_value(
            i, 'label', GLib.VariantType('s'))
        if v and v.get_string() == 'Move to group':
            found_move = True
            break
    assert found_move

    got = []
    sb.connect('project-move-to-group',
               lambda s, hid, path, gid: got.append((hid, path, gid)))
    row.emit('project-move-to-group', child.id)
    assert got == [('localhost', '/tmp/pm-alpha', child.id)]


def test_project_row_no_move_submenu_without_groups():
    """Empty forest → no Move to group submenu on ProjectRow."""
    from model import Project, HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest

    class FakeStore:
        def load_projects(self):
            return [Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')]

    from gi.repository import GLib
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', empty_forest())
    sb.refresh()
    row = sb._rows['/tmp/pm-alpha']
    for i in range(row._menu.get_n_items()):
        v = row._menu.get_item_attribute_value(
            i, 'label', GLib.VariantType('s'))
        if v:
            assert v.get_string() != 'Move to group'


def test_group_create_signal_from_begin_new_subgroup():
    """Committing NameEntryRow under a group emits group-create with parent id."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group

    class FakeStore:
        def load_projects(self):
            return []

    forest = empty_forest()
    g = add_group(forest, 'Work')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    got = []
    sb.connect('group-create',
               lambda s, hid, parent, name: got.append((hid, parent, name)))
    sb._begin_new_subgroup('localhost', g.id)
    assert sb._new_group_entry_row is not None
    # Simulate user typing a name and activating.
    sb._new_group_entry_row._entry.set_text('Sub')
    sb._new_group_entry_row._on_activate(sb._new_group_entry_row._entry)
    assert got == [('localhost', g.id, 'Sub')]
    assert sb._new_group_entry_row is None


def test_project_create_in_group_signal():
    """New project entry inside a group emits project-create-in-group."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from project_groups import empty_forest, add_group

    class FakeStore:
        def load_projects(self):
            return []

    forest = empty_forest()
    g = add_group(forest, 'Work')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()

    got = []
    sb.connect('project-create-in-group',
               lambda s, hid, gid, name: got.append((hid, gid, name)))
    sb.connect('project-create',
               lambda s, hid, name: got.append(('ungrouped', hid, name)))
    sb._begin_new_project_in_group('localhost', g.id)
    assert sb._new_project_row is not None
    assert sb._new_project_group_id == g.id
    sb._new_project_row._entry.set_text('newproj')
    sb._new_project_row._on_activate(sb._new_project_row._entry)
    assert got == [('localhost', g.id, 'newproj')]


def test_host_header_add_menu_has_project_and_group():
    """Host + is a MenuButton with labeled New Project / New Group popover."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar, _TIP_ADD_HOST
    from gi.repository import Gtk

    class FakeStore:
        def load_projects(self):
            return []

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    header = sb._section_headers['localhost']
    assert isinstance(header._add_btn, Gtk.MenuButton)
    tip = header._add_btn.get_tooltip_text() or ''
    assert tip == _TIP_ADD_HOST
    assert 'Project' in tip and 'Group' in tip
    assert header._on_new_group is not None
    assert header._on_add_project is not None
    # Labeled Popover (not Gio.Menu) so AT-SPI exposes real button names.
    pop = header._add_btn.get_popover()
    assert pop is not None
    box = pop.get_child()
    labels = []
    child = box.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            labels.append(child.get_label())
            assert child.get_tooltip_text()  # explanatory tooltips
        child = child.get_next_sibling()
    assert labels == ['New Project', 'New Group']


def test_group_row_add_menu_has_subgroup_and_project():
    """Group + MenuButton offers labeled New Subgroup and New Project buttons."""
    from model import Project, HistoryReader, StatusWatcher
    from project_groups import empty_forest, add_group
    from settings import Settings
    from sidebar import Sidebar
    from gi.repository import Gtk

    class FakeStore:
        def load_projects(self):
            return [Project(name='alpha', path='/tmp/pm-alpha', host_id='localhost')]

    forest = empty_forest()
    g = add_group(forest, 'Work')
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb.set_group_forest('localhost', forest)
    sb.refresh()
    grow = sb._group_rows[('localhost', g.id)]
    assert isinstance(grow._add_btn, Gtk.MenuButton)
    tip = grow._add_btn.get_tooltip_text() or ''
    assert 'Subgroup' in tip or 'Project' in tip
    pop = grow._add_btn.get_popover()
    assert pop is not None
    box = pop.get_child()
    labels = []
    child = box.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            labels.append(child.get_label())
            assert child.get_tooltip_text()
        child = child.get_next_sibling()
    assert 'New Project' in labels
    assert 'New Subgroup' in labels



def test_new_project_empty_name_flashes_required():
    """Empty Enter keeps the row and shows Name required (no silent no-op)."""
    from sidebar import NewProjectEntryRow
    committed = []
    row = NewProjectEntryRow(on_commit=lambda n: committed.append(n), on_cancel=lambda: None)
    row._entry.set_text('   ')
    row._on_activate(row._entry)
    assert committed == []
    assert row._entry.has_css_class('error')
    assert row._entry.get_placeholder_text() == 'Name required'


def test_close_session_button_tooltip_and_a11y_label():
    """Deactivate control is labeled Close session with explanatory tooltip."""
    from sidebar import _TIP_CLOSE_SESSION, ProjectRow
    from model import Project
    from gi.repository import Gtk
    proj = Project(name='x', path='/tmp/pm-x', host_id='localhost')
    row = ProjectRow(proj, None, None, settings=None)
    tip = row._deactivate_btn.get_tooltip_text() or ''
    assert tip == _TIP_CLOSE_SESSION
    assert 'Close session' in tip
    assert 'Open it again to continue the session' in tip
    assert 'sidebar' not in tip.lower()
    # Accessible name is Close session (not Deactivate)
    assert 'Deactivate' not in tip


def test_new_group_dialog_disables_create_until_named():
    """Create stays disabled for empty group name (no silent dismiss)."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from gi.repository import Adw

    class FakeStore:
        def load_projects(self):
            return []

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    # Present dialog without a real root — exercise construction path.
    # We inspect by monkeypatching present and capturing the dialog.
    captured = []
    real_present = Adw.AlertDialog.present

    def capture(self, parent=None):
        captured.append(self)
        # do not present to display

    Adw.AlertDialog.present = capture
    try:
        sb._prompt_new_group_name('localhost', '')
    finally:
        Adw.AlertDialog.present = real_present
    assert captured, 'dialog was not built'
    d = captured[0]
    assert d.get_response_enabled('create') is False
    entry = d.get_extra_child()
    entry.set_text('MyGroup')
    # changed signal should enable Create
    assert d.get_response_enabled('create') is True
    entry.set_text('  ')
    assert d.get_response_enabled('create') is False


def test_new_group_switches_section_filter_to_all():
    """Creating a group forces host section mode to 'all' so it is visible."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar

    class FakeStore:
        def load_projects(self):
            return []

    settings = Settings()
    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=settings)
    sb.set_host_section_mode('localhost', 'active')
    assert sb._section_mode('localhost') == 'active'
    sb._on_section_new_group('localhost')
    assert sb._section_mode('localhost') == 'all'


def test_create_in_group_membership_logic():
    """Pure path: create membership after project create uses project_ref."""
    from hosts import LOCALHOST_ID, encode_project_ref
    from project_groups import empty_forest, add_group, set_membership

    forest = empty_forest()
    g = add_group(forest, 'Work')
    path = '/tmp/pm-created'
    ref = encode_project_ref(LOCALHOST_ID, path)
    assert set_membership(forest, ref, g.id)
    assert forest.membership[ref] == g.id


def test_escape_cancels_new_project_even_when_not_focused():
    """Sidebar CAPTURE Escape dismisses create row (filter-focused case)."""
    from model import HistoryReader, StatusWatcher
    from settings import Settings
    from sidebar import Sidebar
    from gi.repository import Gdk

    class FakeStore:
        def load_projects(self):
            return []

    sb = Sidebar(FakeStore(), HistoryReader(), StatusWatcher(), settings=Settings())
    sb._on_add_project(None, host_id='localhost')
    assert sb._new_project_row is not None
    # Simulate Escape without focusing the create entry (filter has focus).
    handled = sb._on_sidebar_capture_key(None, Gdk.KEY_Escape, 0, 0)
    assert handled is True
    assert sb._new_project_row is None


def test_new_project_rejects_slash_and_shell_meta_with_feedback():
    from sidebar import NewProjectEntryRow
    committed = []
    row = NewProjectEntryRow(on_commit=lambda n: committed.append(n), on_cancel=lambda: None)
    row._entry.set_text('a/b')
    row._on_activate(row._entry)
    assert committed == []
    assert row._entry.has_css_class('error')
    assert '/' in (row._entry.get_placeholder_text() or '') or 'cannot' in (row._entry.get_placeholder_text() or '').lower()
    row._entry.set_text('$(whoami)')
    row._on_activate(row._entry)
    assert committed == []
    assert row._entry.has_css_class('error')
    row._entry.set_text('ok-name')
    row._on_changed(row._entry)
    assert not row._entry.has_css_class('error')
    row._on_activate(row._entry)
    assert committed == ['ok-name']

