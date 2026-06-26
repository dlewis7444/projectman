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

Provider editing now lives in a dedicated ``ProviderEditorWindow`` sub-window
(not the in-page ExpanderRow), so the tier-combo / editability tests open that
editor headless (its ``present`` patched out too).
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


def _no_present_factory():
    """Return a no-op present() replacement. Used for both SettingsWindow and
    ProviderEditorWindow — neither realizes under pytest."""
    def _no_present(_self, *a, **k):
        pass
    return _no_present


def _make_sw(settings):
    """Construct a SettingsWindow without presenting it (no display needed).

    present() realizes the window and needs a surface; patching it out lets the
    full page-build path — including the slim provider rows — run headless,
    which is exactly where the ExpandableRow crash lived.
    """
    orig = sw_mod.SettingsWindow.present
    sw_mod.SettingsWindow.present = _no_present_factory()
    try:
        return sw_mod.SettingsWindow(settings, _FAKE_APP, None)
    finally:
        sw_mod.SettingsWindow.present = orig


def _make_editor(settings, pid):
    """Construct a ProviderEditorWindow headless (present patched out)."""
    orig = sw_mod.ProviderEditorWindow.present
    sw_mod.ProviderEditorWindow.present = _no_present_factory()
    try:
        return sw_mod.ProviderEditorWindow(settings, _FAKE_APP, None, pid)
    finally:
        sw_mod.ProviderEditorWindow.present = orig


def _ollama_provider(models=('glm-5.2:cloud[1m]',)):
    return {'ollama': {'name': 'Ollama', 'base_url': 'http://localhost:11434',
                       'api_key': 'k', 'models': list(models)}}


def _tier_combos(editor):
    """The Adw.ComboRow tier combos inside the editor's tier group only."""
    combos = {}
    for w in _walk(editor._tier_group):
        if isinstance(w, Adw.ComboRow):
            combos[w.get_title()] = w
    return combos


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


def test_provider_row_titled_with_name_preserves_walk_assertion():
    """The slim provider row's title is the provider's display name — the gate
    walk asserts ``has('Ollama')``, so this must hold after the ExpanderRow→row
    refactor."""
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    sw = _make_sw(s)
    row = sw._build_provider_row('ollama', s.providers['ollama'])
    assert row.get_title() == 'Ollama'
    assert row.get_subtitle() == 'ollama'
    # The row is activatable (opens the editor on click) and carries a chevron
    # suffix as the visual affordance — no "Models" button (dropped in the
    # Adw.Window→Adw.Dialog refactor per David).
    assert row.get_activatable() is True
    chevrons = [w for w in _walk(row)
                if isinstance(w, Gtk.Image)]
    assert len(chevrons) >= 1


def test_provider_row_activatable_opens_editor_for_pid():
    """Activating a provider row routes to _open_editor(pid) — the wiring that
    lets the parallel cage-gate walk reach the editor. The row is built
    directly (the unrealized PreferencesDialog doesn't expose its pages'
    children through the widget walk). Replaces the old 'Models' button
    (dropped per David: a button inside Settings opening another window was the
    wrong shape; the row itself is the affordance now)."""
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    sw = _make_sw(s)
    opened = []
    sw._open_editor = lambda pid: opened.append(pid)
    row = sw._build_provider_row('ollama', s.providers['ollama'])
    assert row.get_activatable() is True
    row.emit('activated')
    assert opened == ['ollama']


# --- #2: editor tier combos (replaces the old provider-card combo tests) ----

def test_editor_has_five_tier_combos():
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    combos = _tier_combos(editor)
    assert set(combos) == {'Opus', 'Sonnet', 'Haiku', 'Subagent', 'Fable (future?)'}


def test_editor_fable_combo_disabled():
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    combos = _tier_combos(editor)
    assert combos['Fable (future?)'].get_sensitive() is False
    # The real tiers are editable.
    assert combos['Opus'].get_sensitive() is True


def test_editor_tier_combos_sensitive_even_when_default_is_native():
    """The B2 invariant: TA is editable for any defined provider regardless of
    the default. With a native default, the ollama editor's tier combos are
    still sensitive (the old standalone TA group disabled them when
    default='')."""
    s = Settings(providers=_ollama_provider(), model_default='')  # native default
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    combos = _tier_combos(editor)
    assert combos['Opus'].get_sensitive() is True
    assert combos['Sonnet'].get_sensitive() is True
    assert combos['Subagent'].get_sensitive() is True
    assert combos['Fable (future?)'].get_sensitive() is False  # placeholder


# --- #3: UI↔spawn editability invariant --------------------------------------

def test_override_provider_tiers_honored_and_editor_editable():
    """The cross-layer agreement: a provider reachable only via a per-project
    override (native default) has its tiers honored by build_spawn_env AND its
    editor combos are editable. Locks the per-provider design so the UI and the
    spawn path can't silently disagree again (the TA-disabled-when-native gap).
    """
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
    # UI side: the override provider's editor is editable even though it's not
    # the default (and the default is native).
    _make_sw(s)
    editor = _make_editor(s, 'openrouter')
    combos = _tier_combos(editor)
    assert combos['Opus'].get_sensitive() is True


# --- #4: save-on-change writes all four fields (the bug-#2 regression guard) -

def test_editor_save_on_change_writes_all_four_fields(monkeypatch, tmp_path):
    """The data-loss bug: under the ExpanderRow, Name/Base URL saved only on
    apply (Enter) and a full rebuild yanked focus mid-fill, so they were lost.
    The editor saves Name on apply, Base URL on focus-out (the focus
    controller's 'leave'), API key on every keystroke, and a model on Add
    apply — so all four fields persist to settings.json without ever pressing
    Enter on Base URL."""
    import settings as settings_mod
    # Isolate the round-trip: the editor's save-on-change calls save() with no
    # path, which defaults to DEFAULT_SETTINGS_PATH — the user's REAL
    # ~/.ProjectMan/settings.json. Redirect that global to a tmp path so this
    # test can't clobber real settings.
    tmp_settings = tmp_path / 'settings.json'
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH', str(tmp_settings))
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': []}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')

    # Name — explicit apply (Enter / checkmark).
    editor._name_row.set_text('Ollama')
    editor._name_row.emit('apply')

    # Base URL — focus-out only (NO apply). This is the path that used to lose
    # the field. Emit 'leave' on the stashed focus controller.
    editor._url_row.set_text('http://localhost:11434')
    editor._url_row._focus_ctrl.emit('leave')

    # API key — notify::text fires on set_text (covers paste + typing).
    editor._key_entry.set_text('sekret')

    # Add model — apply on the Add-model entry.
    editor._add_model_row.set_text('glm-5.2:cloud[1m]')
    editor._add_model_row.emit('apply')

    prov = s.providers['ollama']
    assert prov['name'] == 'Ollama'
    assert prov['base_url'] == 'http://localhost:11434'
    assert prov['api_key'] == 'sekret'
    assert prov['models'] == ['glm-5.2:cloud[1m]']

    # And it round-trips through save()→load() (the isolated settings.json).
    reloaded = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    r = reloaded.providers['ollama']
    assert r['name'] == 'Ollama'
    assert r['base_url'] == 'http://localhost:11434'
    assert r['api_key'] == 'sekret'
    assert r['models'] == ['glm-5.2:cloud[1m]']


def test_editor_add_model_does_not_destroy_other_fields():
    """The bug-#1 regression guard: adding a model (which rebuilds the model
    list) must NOT clear an in-progress Name edit. Name lives in a separate
    group from the model list, so it survives the rebuild."""
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': []}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    editor._name_row.set_text('Ollama')
    editor._name_row.emit('apply')
    # Add two models back-to-back; the Add entry is rebuilt each time.
    editor._add_model_row.set_text('glm')
    editor._add_model_row.emit('apply')
    editor._add_model_row.set_text('kimi')
    editor._add_model_row.emit('apply')
    assert s.providers['ollama']['name'] == 'Ollama'
    assert s.providers['ollama']['models'] == ['glm', 'kimi']


# --- #5: classifier levers in the editor -------------------------------

def _find_row(widget, title):
    # Adw.Dialog doesn't expose its set_child content via get_first_child (the
    # content lives in a dialog-internal slot), so walk from the content child.
    root = widget.get_child() if isinstance(widget, Adw.Dialog) else widget
    for w in _walk(root or widget):
        if isinstance(w, Adw.EntryRow) and w.get_title() == title:
            return w
        if isinstance(w, Adw.SwitchRow) and w.get_title() == title:
            return w
        if isinstance(w, Adw.ComboRow) and w.get_title() == title:
            return w
    return None


def test_editor_has_classifier_controls():
    s = Settings(providers=_ollama_provider(models=('glm', 'kimi')),
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    assert _find_row(editor, 'Auto-mode model') is not None
    assert _find_row(editor, 'Background classifier') is not None
    assert _find_row(editor, 'Classifier temperature') is not None
    assert _find_row(editor, 'Two-stage classifier') is not None


def test_editor_classifier_controls_save_on_change():
    """All four classifier controls mutate settings on change without a full
    rebuild (regression guard for bug #1)."""
    import settings as settings_mod
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': ['glm', 'kimi']}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')

    # Auto-mode model combo — select 'kimi' (index 2: Default, glm, kimi).
    auto_combo = _find_row(editor, 'Auto-mode model')
    auto_combo.set_selected(2)

    # Background classifier combo — select 'kimi'.
    bg_combo = _find_row(editor, 'Background classifier')
    bg_combo.set_selected(2)

    # Temperature — focus-out only.
    temp_row = _find_row(editor, 'Classifier temperature')
    temp_row.set_text('0.5')
    temp_row._focus_ctrl.emit('leave')

    # Two-stage switch — toggle on.
    ts_row = _find_row(editor, 'Two-stage classifier')
    ts_row.set_active(True)

    assert s.classifier_models['ollama'] == {
        'auto_mode': 'kimi', 'bg_classifier': 'kimi'}
    assert s.classifier_temperature == {'ollama': 0.5}
    assert s.classifier_two_stage == {'ollama': True}

    # Round-trip through save/load.
    reloaded = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    assert reloaded.classifier_models['ollama'] == {
        'auto_mode': 'kimi', 'bg_classifier': 'kimi'}
    assert reloaded.classifier_temperature == {'ollama': 0.5}
    assert reloaded.classifier_two_stage == {'ollama': True}


def test_editor_classifier_model_persists_when_adding_model():
    """Bug-#1 guard for classifier combos: adding a new model must not drop
    an already-chosen classifier model pick."""
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': ['glm']}},
                 model_default='ollama',
                 classifier_models={'ollama': {'auto_mode': 'glm'}})
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    # Add a new model; the classifier group is rebuilt but selection is
    # restored from settings.
    editor._add_model_row.set_text('kimi')
    editor._add_model_row.emit('apply')
    assert s.classifier_models['ollama']['auto_mode'] == 'glm'


# --- #6: close commits pending edits (the close-attempt safety net) ---------

def test_editor_close_attempt_commits_pending_name_edit(monkeypatch, tmp_path):
    """Regression guard for the close data-loss bug: closing the editor (Escape
    / back / close()) used to bypass the commit safety net and lose a pending
    Name edit (typed but not applied). Adw.Dialog fires 'close-attempt' before
    close (entries still alive); the handler runs _teardown → commit."""
    import settings as settings_mod
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH',
                        str(tmp_path / 'settings.json'))
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': []}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    # Type a name but do NOT emit 'apply' — it's a pending edit held only in
    # the entry buffer.
    editor._name_row.set_text('Ollama')
    # Fire close-attempt as Escape / the back button would.
    editor.emit('close-attempt')
    # _teardown ran on close-attempt, so the name was committed before close.
    assert s.providers['ollama']['name'] == 'Ollama'


def test_editor_probe_result_bails_after_teardown():
    """Regression guard for the probe use-after-free: the reachability probe
    runs on a daemon thread and lands via GLib.idle_add. If the editor was
    closed (and its widgets disposed) before the callback fires, it must bail
    on the _closed flag rather than touch disposed widgets."""
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': ['glm']}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    editor._teardown()
    assert editor._closed is True
    # Must not raise and must short-circuit (return False) — no widget access.
    assert editor._apply_probe_result('glm', True, True) is False


# --- #7: editor opens with a PreferencesDialog parent (the real-app path) ----

def test_editor_constructs_with_preferencesdialog_parent():
    """Regression for the 2ea4d2f editor-open bug surfaced by the cg-pmprov-001
    persona + a deterministic cage probe. _open_editor hands the SettingsWindow
    (an Adw.PreferencesDialog, NOT a Gtk.Window) as the editor's parent. An
    unconditional set_transient_for(parent) raised TypeError → __init__ aborted
    before present() → the editor never opened in the real app. The headless
    tests missed it (they pass parent=None, skipping the call). Constructing the
    editor with a real PreferencesDialog parent must not raise."""
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': ['glm']}},
                 model_default='ollama')
    sw = _make_sw(s)  # a real Adw.PreferencesDialog
    orig = sw_mod.ProviderEditorWindow.present
    sw_mod.ProviderEditorWindow.present = _no_present_factory()
    try:
        editor = sw_mod.ProviderEditorWindow(s, _FAKE_APP, sw, 'ollama')
    finally:
        sw_mod.ProviderEditorWindow.present = orig
    assert editor is not None
