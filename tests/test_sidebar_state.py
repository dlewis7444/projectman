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
