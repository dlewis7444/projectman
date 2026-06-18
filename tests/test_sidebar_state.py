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
#
# Claude Code is the sole harness (effective_agent always returns 'claude'),
# so these tests swap ADAPTERS['claude'] for a fake adapter to exercise the
# caps-gating branches that a single-harness fleet would otherwise never
# reach.
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
    """Configurable fake adapter for caps/sessions sliver tests.

    Swapped in for ADAPTERS['claude'] so the row's _adapter() (which resolves
    through effective_agent → get_adapter('claude')) returns this fake, letting
    us exercise low/no caps branches the real single-harness fleet never hits.
    """
    id = 'claude'
    display_name = 'Claude Code'

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
    fake = _FakeCapsAdapter(caps, refs)
    # The row resolves its adapter via effective_agent (always 'claude') →
    # get_adapter('claude') → ADAPTERS['claude']; swap claude for the fake so
    # the row's caps gating and session enumeration use it (during init too).
    monkeypatch.setitem(agents.ADAPTERS, 'claude', fake)
    s = Settings()
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
# P2 Part B — B3 UI: provider subtitle (Claude-Only + first-class model axis).
# The harness is always Claude Code; the subtitle surfaces a pinned PROVIDER
# (or a live-harness mismatch defensive guard) — a plain native row stays clean.
# ===========================================================================

def _make_row_with_settings(settings, path='/tmp/test'):
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    proj = Project(name='test', path=path)
    return ProjectRow(proj, HistoryReader(), StatusWatcher(), settings=settings)


def _providers(**names):
    """Build a providers dict where each provider name maps to a minimal entry
    with an empty model list (the new shape: models is a LIST)."""
    return {pid: {'name': name, 'base_url': '', 'api_key': '', 'models': []}
            for pid, name in names.items()}


def test_subtitle_hidden_for_plain_default():
    """Native provider (no pin) + no running session → no subtitle clutter."""
    from settings import Settings
    row = _make_row_with_settings(Settings(), path='/tmp/p')
    assert row._subtitle_label.get_visible() is False


def test_subtitle_shows_harness_and_provider_when_pinned():
    """A pinned provider → '<HarnessDisplay> · <ProviderLabel>'."""
    from settings import Settings
    s = Settings(model_default='ollama',
                 providers=_providers(ollama='Ollama'))
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'Claude Code · Ollama'


def test_subtitle_per_project_override_uses_that_provider_label():
    """A per-project provider override shows THAT provider's label, not the
    global default's (effective_provider resolves per-row)."""
    from settings import Settings
    s = Settings(model_default='ollama',
                 model_overrides={'/tmp/p': 'openrouter'},
                 providers=_providers(ollama='Ollama', openrouter='OpenRouter'))
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_text() == 'Claude Code · OpenRouter'


# ===========================================================================
# P3.5d Item 1 (FINDING 3 / C5): the subtitle tells the truth about NOW.
# With a single harness the mismatch branch is unreachable in practice, but the
# defensive guard is pinned: a live child running a different harness than
# configured leads with what is ACTUALLY running. set_running_agent(None)
# restores the configured-only subtitle.
# ===========================================================================

def test_subtitle_running_agent_mismatch_leads_with_running():
    """T1: live child runs a different harness than configured (always claude)
    → '<Running> (next: <Configured>)' + the provider suffix. _harness_display
    falls back to the raw id for an unregistered harness."""
    from settings import Settings
    s = Settings(model_default='ollama',
                 providers=_providers(ollama='Ollama'))
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_agent('opencode')
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'opencode (next: Claude Code) · Ollama'


def test_subtitle_mismatch_overrides_clean_default_hide():
    """C5 corollary: a native (normally hidden) row still shows the truth when a
    live child runs a non-default harness (no provider → no suffix)."""
    from settings import Settings
    s = Settings()                       # native default → normally hidden
    row = _make_row_with_settings(s, path='/tmp/p')
    assert row._subtitle_label.get_visible() is False
    row.set_running_agent('opencode')    # live child runs a different harness
    assert row._subtitle_label.get_visible() is True
    assert row._subtitle_label.get_text() == 'opencode (next: Claude Code)'


def test_set_running_agent_none_restores_clean_subtitle():
    """Clearing the running agent (session ended) restores the configured-only
    subtitle — the mismatch form is gone."""
    from settings import Settings
    s = Settings(model_default='ollama',
                 providers=_providers(ollama='Ollama'))
    row = _make_row_with_settings(s, path='/tmp/p')
    row.set_running_agent('opencode')
    assert 'opencode' in row._subtitle_label.get_text()
    row.set_running_agent(None)
    assert row._subtitle_label.get_text() == 'Claude Code · Ollama'


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
    sb.set_running_agent('/no/such/path', 'opencode')   # must be a silent no-op


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
    id = 'claude'
    display_name = 'Claude Code'

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
    # Swap claude for the exploding adapter so the row resolves to it.
    monkeypatch.setitem(agents.ADAPTERS, 'claude', adapter)
    row = ProjectRow(Project(name='test', path='/tmp/test'),
                     HistoryReader(), StatusWatcher(),
                     settings=Settings())
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
# P3.5f (C2, David's second reveal): the "Default (…)" label is PER-ROW —
# computed from THIS row's effective provider, not the single global label
# the window pushes (which a per-project-override row would wear as the global
# default's story).
# ===========================================================================

def test_override_row_default_label_is_per_row_not_global():
    """BINDING (P3.5f / C2): on an ollama-default bench, a project that OVERRIDES
    its provider to openrouter shows a 'Default (…)' label that tells
    OpenRouter's story — NOT the global ollama default's 'Ollama' story, even
    though the window pushes that ollama label as global_label. The row derives
    its OWN label from its effective provider."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    providers = _providers(ollama='Ollama', openrouter='OpenRouter')
    s = Settings(model_default='ollama',
                 model_overrides={'/tmp/p': 'openrouter'},
                 providers=providers)
    row = _make_row_with_settings(s, path='/tmp/p')
    # The window pushes the OLLAMA global label (what _refresh_sidebar_models
    # computes from model_default=ollama) — the row must NOT wear it.
    options = list(zip(*build_provider_options(providers)))
    row.set_model_options(options, FOLLOW_DEFAULT, 'Ollama')
    labels = _model_submenu_labels(row)
    default_lbls = [l for l in labels if l.startswith('Default (')]
    assert len(default_lbls) == 1
    default_lbl = default_lbls[0]
    # Tells OpenRouter's story (THIS row's effective provider), NOT the global
    # ollama default's.
    assert 'OpenRouter' in default_lbl
    assert 'Ollama' not in default_lbl


# ===========================================================================
# G1 (reveal-3 item 1, C2/C4): the Model submenu must not offer the same
# choice twice. A provider option whose LABEL restates the Default story is a
# redundant pin the user hasn't taken — suppress it UNLESS it is the live
# selection (a pin the user DID take stays visible and checked).
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


def test_g1a_no_providers_default_native_dedups():
    """T-G1a (the verbatim repro): no providers, global default native,
    current=FOLLOW_DEFAULT → the submenu contains EXACTLY one item,
    'Default (Anthropic (native))'. The bare native sentinel that duplicated the
    Default story is gone. Reverting the suppression FAILS here (the bare
    'Anthropic (native)' entry reappears)."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    s = Settings(providers={})
    ids, labels = build_provider_options(s.providers)
    options = list(zip(ids, labels))           # [('', 'Anthropic (native)')]
    assert options == [('', NATIVE_LABEL)]
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    row.set_model_options(options, FOLLOW_DEFAULT, NATIVE_LABEL)
    menu = _model_submenu_labels(row)
    assert menu == [f'Default ({NATIVE_LABEL})']
    # No bare native entry survives.
    assert NATIVE_LABEL not in menu
    assert _model_submenu_targets(row) == [FOLLOW_DEFAULT]


def test_g1b_providers_native_suppressed_provider_entries_intact():
    """T-G1b: providers configured, global default native → menu = Default +
    provider entries; the native sentinel (whose label == the Default story) is
    suppressed; the provider entries survive intact."""
    from settings import Settings
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    providers = {
        'openrouter': {'name': 'OpenRouter', 'base_url': '', 'api_key': '',
                       'models': []},
    }
    s = Settings(providers=providers)
    options = [(i, l) for i, l in zip(*build_provider_options(s.providers))]
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    row.set_model_options(options, FOLLOW_DEFAULT, NATIVE_LABEL)
    targets = _model_submenu_targets(row)
    labels = _model_submenu_labels(row)
    # native sentinel ('') suppressed; provider entry intact.
    assert '' not in targets
    assert 'openrouter' in targets
    assert NATIVE_LABEL not in labels        # only inside the 'Default (…)' label
    assert labels[0] == f'Default ({NATIVE_LABEL})'


def test_g1c_default_is_provider_label_native_sentinel_present():
    """T-G1c: the Default story resolves to a PROVIDER's label → the native
    sentinel no longer duplicates and IS present; the provider entry whose label
    equals the Default story is the one suppressed.

    Uses a settings-less row so the row's Default label is exactly the pushed
    ``global_label`` (a settings row resolves per-row to its effective provider,
    which would re-suppress a different entry — not the case under test).
    build_provider_options gives the native sentinel + the provider entry."""
    from models import FOLLOW_DEFAULT, NATIVE_LABEL, build_provider_options
    providers = {
        'openrouter': {'name': 'OpenRouter', 'base_url': '', 'api_key': '',
                       'models': []},
    }
    options = [(i, l) for i, l in zip(*build_provider_options(providers))]
    story = 'OpenRouter'                     # the global default's resolved label
    row = _make_row()                        # no settings → Default label == global_label
    row.set_model_options(options, FOLLOW_DEFAULT, story)
    targets = _model_submenu_targets_all(row)
    labels = _model_submenu_labels(row)
    # The native sentinel is present (its label != the provider Default story).
    assert '' in targets
    assert NATIVE_LABEL in labels
    # The provider entry whose label == the Default story is suppressed.
    assert 'openrouter' not in targets
    assert labels[0] == f'Default ({story})'


def test_g1d_live_pin_to_suppressed_id_stays_present_and_checked():
    """T-G1d: when current is PINNED to the would-be-suppressed id, the entry is
    PRESENT and the action state points at it — a pin the user took must stay
    visible and checked, never silently dropped."""
    from settings import Settings
    from models import NATIVE_LABEL, build_provider_options
    s = Settings(providers={})
    options = list(zip(*build_provider_options(s.providers)))  # [('', NATIVE_LABEL)]
    row = _make_row_with_settings(s, path='/tmp/claudeproj')
    # current pinned to '' (the native sentinel that equals the Default story).
    row.set_model_options(options, '', NATIVE_LABEL)
    targets = _model_submenu_targets_all(row)
    assert '' in targets                      # the pinned entry survives
    assert row._model_action.get_state().get_string() == ''  # and is the active state


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