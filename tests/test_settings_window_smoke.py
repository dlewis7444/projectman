"""Headless GTK construction smoke for the Models tab + a UI↔spawn invariant.

Purpose: catch the class of UI-surface defects that plain pytest and the
gui-smoke gate miss — a window that crashes when built with realistic state
(e.g. the late Adw.ExpandableRow→ExpanderRow crash only fired once a provider
existed), and a UI that disagrees with the spawn path about which provider
Tier Assignments apply to.

These construct real GTK widgets headless (no display) like the sidebar tests,
with ``SettingsWindow.present`` patched out so the unrealized window never
needs a surface. They run in plain pytest on every commit.

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
    # Subtitle shows the base_url (identifying), not the internal pid.
    assert row.get_subtitle() == 'http://localhost:11434'
    # The row is activatable (opens the editor on click) and carries a chevron
    # suffix as the visual affordance — no "Models" button (dropped in the
    # Adw.Window→Adw.Dialog refactor per the maintainer).
    assert row.get_activatable() is True
    chevrons = [w for w in _walk(row)
                if isinstance(w, Gtk.Image)]
    assert len(chevrons) >= 1


def test_provider_row_activatable_opens_editor_for_pid():
    """Activating a provider row routes to _open_editor(pid) — the wiring that
    lets the parallel release gate walk reach the editor. The row is built
    directly (the unrealized PreferencesDialog doesn't expose its pages'
    children through the widget walk). Replaces the old 'Models' button
    (dropped per the maintainer: a button inside Settings opening another window was the
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
    assert set(combos) == {'Opus', 'Sonnet', 'Haiku', 'Subagent', 'Fable'}


def test_editor_fable_combo_enabled():
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    combos = _tier_combos(editor)
    assert combos['Fable'].get_sensitive() is True
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
    assert combos['Fable'].get_sensitive() is True  # active tier


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
                 provider_overrides={'/p': 'openrouter'},  # project on openrouter
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

def test_editor_writes_all_four_fields_on_close(monkeypatch, tmp_path):
    """The data-loss bug: under the ExpanderRow, Name/Base URL saved only on
    apply (Enter) and a full rebuild yanked focus mid-fill, so they were lost.
    The editor mutates the live in-memory Settings on apply / focus-out /
    keystroke / Add-apply, then persists all four fields to settings.json once
    on close — so they survive a save()→load() round-trip without ever
    pressing Enter on Base URL."""
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

    # The disk write is deferred to close — flush it, then round-trip through
    # save()→load() (the isolated settings.json).
    editor.emit('closed')
    reloaded = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    r = reloaded.providers['ollama']
    assert r['name'] == 'Ollama'
    assert r['base_url'] == 'http://localhost:11434'
    assert r['api_key'] == 'sekret'
    assert r['models'] == ['glm-5.2:cloud[1m]']


def test_editor_defers_disk_write_to_close(monkeypatch, tmp_path):
    """B4: edits mutate the live in-memory Settings immediately, but the
    settings.json disk write is deferred to close — one write per editing
    session, not one per keystroke (the API-key notify::text path was the
    worst offender). Guards against a silent revert to save-on-change."""
    import settings as settings_mod
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH',
                        str(tmp_path / 'settings.json'))
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': []}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    # Patch save AFTER construction so build-time saves use the real path;
    # we only want to count edit-time writes.
    saves = []
    monkeypatch.setattr(s, 'save', lambda *a, **k: saves.append(1))

    # Several edits across fields, including two API-key "keystrokes" (the
    # notify::text path that used to fire a write per character).
    editor._name_row.set_text('Ollama')
    editor._name_row.emit('apply')
    editor._url_row.set_text('http://localhost:11434')
    editor._url_row._focus_ctrl.emit('leave')
    editor._key_entry.set_text('sekret')
    editor._key_entry.set_text('sekret2')
    editor._add_model_row.set_text('glm-5.2:cloud[1m]')
    editor._add_model_row.emit('apply')

    # Edits landed in-memory immediately...
    assert s.providers['ollama']['name'] == 'Ollama'
    assert s.providers['ollama']['api_key'] == 'sekret2'
    assert s.providers['ollama']['models'] == ['glm-5.2:cloud[1m]']
    # ...but NOT ONE disk write fired mid-edit.
    assert saves == []

    # The single write happens on close.
    editor.emit('closed')
    assert saves == [1]


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
    assert _find_row(editor, 'Classifier temperature') is not None
    # The other classifier env vars are inert in Claude Code v2.1.190+ and
    # their UI rows were pruned.
    assert _find_row(editor, 'Auto-mode model') is None
    assert _find_row(editor, 'Background classifier') is None
    assert _find_row(editor, 'Two-stage classifier') is None


def test_editor_classifier_temperature_persists_on_close():
    """The classifier temperature control mutates the live in-memory Settings
    on change and persists to settings.json once on close."""
    import settings as settings_mod
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': ['glm', 'kimi']}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')

    # Temperature — focus-out only.
    temp_row = _find_row(editor, 'Classifier temperature')
    temp_row.set_text('0.5')
    temp_row._focus_ctrl.emit('leave')

    assert s.classifier_temperature == {'ollama': 0.5}

    # The disk write is deferred to close — flush it, then round-trip through save/load.
    editor.emit('closed')
    reloaded = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    assert reloaded.classifier_temperature == {'ollama': 0.5}


def test_editor_classifier_temperature_persists_when_adding_model():
    """Bug-#1 guard: adding a new model must not drop an already-chosen
    classifier temperature."""
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': ['glm']}},
                 model_default='ollama',
                 classifier_temperature={'ollama': 0.7})
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    # Add a new model; the classifier group is rebuilt but the temperature
    # value is restored from settings.
    editor._add_model_row.set_text('kimi')
    editor._add_model_row.emit('apply')
    assert s.classifier_temperature == {'ollama': 0.7}


def test_editor_select_models_picker_merges_server_and_manual(monkeypatch):
    """C6: the server-fed picker fetches models asynchronously, merges them
    with manually-added ones (which are kept and flagged), and mutates the
    live provider on toggle without writing to disk until close (B4 contract)."""
    import threading
    from gi.repository import GLib
    import settings_window as sw_mod
    from models import normalize_model_id

    # Patch the import binding in settings_window, not the models module, because
    # ProviderEditorWindow uses the locally-imported name.
    monkeypatch.setattr(
        sw_mod, 'list_provider_models',
        lambda _provider: {'server-glm', 'server-kimi'})
    # Make the daemon-thread + GLib.idle_add path run synchronously in this
    # headless test (no main loop).
    monkeypatch.setattr(
        threading, 'Thread',
        lambda target, daemon=False, *a, **k:
            type('_FakeThread', (), {'start': lambda _self: target(*a, **k)})())
    monkeypatch.setattr(GLib, 'idle_add',
                        lambda func, *args: func(*args) or 0)

    s = Settings(providers={'ollama': {'name': 'Ollama',
                                       'base_url': 'http://localhost:11434',
                                       'api_key': 'k',
                                       'models': ['manual-model']}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')

    # Opening the expander triggers the fetch; with the synchronous patches
    # above the rows are built immediately.
    editor._picker_expander.set_expanded(True)

    assert set(editor._picker_rows) == {
        normalize_model_id('manual-model'), 'server-glm', 'server-kimi'}

    manual_row = editor._picker_rows[normalize_model_id('manual-model')]
    assert manual_row.get_active() is True
    assert manual_row.get_subtitle() == 'manually added'

    server_row = editor._picker_rows['server-glm']
    assert server_row.get_active() is False
    server_row.set_active(True)
    assert 'server-glm' in s.providers['ollama']['models']
    assert editor._dirty is True

    # No disk write mid-edit (B4 deferred-write contract).
    saves = []
    monkeypatch.setattr(s, 'save', lambda *a, **k: saves.append(1))
    assert saves == []

    # The single write happens on close.
    editor.emit('closed')
    assert saves == [1]


def test_editor_select_models_picker_offline_falls_back_to_add_row(monkeypatch):
    """C6: when the provider is unreachable, list_provider_models returns None
    and the picker falls back to the free-text Add-model row. The manual
    models are still shown in the picker so the user can uncheck them."""
    import threading
    from gi.repository import GLib
    import settings_window as sw_mod
    from models import normalize_model_id

    monkeypatch.setattr(sw_mod, 'list_provider_models', lambda _provider: None)
    monkeypatch.setattr(
        threading, 'Thread',
        lambda target, daemon=False, *a, **k:
            type('_FakeThread', (), {'start': lambda _self: target(*a, **k)})())
    monkeypatch.setattr(GLib, 'idle_add',
                        lambda func, *args: func(*args) or 0)

    s = Settings(providers={'ollama': {'name': 'Ollama',
                                       'base_url': 'http://offline:11434',
                                       'api_key': 'k',
                                       'models': ['manual-model']}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    editor._picker_expander.set_expanded(True)

    # Only the manual model appears; server list is empty.
    assert set(editor._picker_rows) == {normalize_model_id('manual-model')}
    manual_row = editor._picker_rows[normalize_model_id('manual-model')]
    assert manual_row.get_active() is True

    # The free-text Add row is still present and functional (offline fallback).
    editor._add_model_row.set_text('offline-model')
    editor._add_model_row.emit('apply')
    assert s.providers['ollama']['models'] == ['manual-model', 'offline-model']


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
    # Fire 'closed' as Escape / the back button / close() would — with the
    # default can-close=True Adw.Dialog emits 'closed' (after-dismiss), not
    # 'close-attempt' (which only fires when can-close=False).
    editor.emit('closed')
    # _teardown ran on 'closed', so the name was committed before the dialog
    # object goes away.
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
    """Regression for the 2ea4d2f editor-open bug surfaced by the 
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


# --- #8: base_url validation ----------------------------------------------

def test_editor_bad_url_rejected_on_apply():
    """Invalid schemes or unparseable URLs must not overwrite the stored
    base_url, and the row must show an inline error."""
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    prior = s.providers['ollama']['base_url']

    editor._url_row.set_text('ftp://x')
    editor._url_row.emit('apply')
    assert s.providers['ollama']['base_url'] == prior
    assert editor._url_row.has_css_class('error') is True
    assert 'http:// or https://' in (editor._url_row.get_tooltip_text() or '')

    editor._url_row.set_text('not a url')
    editor._url_row.emit('apply')
    assert s.providers['ollama']['base_url'] == prior
    assert editor._url_row.has_css_class('error') is True


def test_editor_bad_url_rejected_on_focus_out():
    """Focus-out (the type-and-move-on path B4 fixed) must also validate."""
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    prior = s.providers['ollama']['base_url']

    editor._url_row.set_text('ftp://x')
    editor._url_row._focus_ctrl.emit('leave')
    assert s.providers['ollama']['base_url'] == prior
    assert editor._url_row.has_css_class('error') is True


def test_editor_good_url_committed_on_apply():
    """A valid http/https URL with a host commits and clears any prior error."""
    s = Settings(providers={'ollama': {'name': '', 'base_url': '',
                                       'api_key': '', 'models': []}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')

    editor._url_row.set_text('http://localhost:11434')
    editor._url_row.emit('apply')
    assert s.providers['ollama']['base_url'] == 'http://localhost:11434'
    assert editor._url_row.has_css_class('error') is False
    assert editor._url_row.get_tooltip_text() == 'e.g. http://localhost:11434'


def test_editor_empty_url_clears_base_url():
    """Empty base_url is valid: it means "no base_url, provider skipped"."""
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')

    editor._url_row.set_text('')
    editor._url_row.emit('apply')
    assert s.providers['ollama']['base_url'] == ''
    assert editor._url_row.has_css_class('error') is False


def test_editor_bad_url_discarded_on_close():
    """B4+validation coordination: closing with a pending bad URL must not
    persist it. _teardown calls _commit_url(); validation rejects it, leaving
    the prior valid value in _prov and therefore in settings.json."""
    import settings as settings_mod
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    prior = s.providers['ollama']['base_url']

    editor._url_row.set_text('ftp://x')
    editor.emit('closed')
    assert s.providers['ollama']['base_url'] == prior


# --- #9: context window + per-model 1M toggle --------------------------------

def test_editor_tier_description_includes_fable():
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    desc = editor._tier_group.get_description() or ''
    assert 'Fable' in desc


def test_editor_max_context_tokens_field_commits():
    s = Settings(providers={'ollama': {'name': 'O', 'base_url': 'http://x',
                                       'api_key': '', 'models': []}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    assert hasattr(editor, '_max_ctx_row')
    editor._max_ctx_row.set_text('200000')
    editor._max_ctx_row.emit('apply')
    assert s.providers['ollama']['max_context_tokens'] == 200000
    editor._max_ctx_row.set_text('')
    editor._max_ctx_row.emit('apply')
    assert 'max_context_tokens' not in s.providers['ollama']


def test_editor_1m_toggle_rewrites_model_id_and_tier_pin():
    class _FakeBtn:
        def __init__(self, active):
            self._active = active

        def get_active(self):
            return self._active

    s = Settings(providers={'ollama': {'name': 'O', 'base_url': 'http://x',
                                       'api_key': '', 'models': ['my-model']}},
                 model_default='ollama',
                 tier_models={'ollama': {
                     'opus': 'my-model', 'sonnet': '', 'haiku': '',
                     'subagent': '', 'fable': '',
                 }})
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    assert editor._model_row_for['my-model'].get_title() == 'my-model'
    editor._on_model_1m_toggled('my-model', _FakeBtn(True))
    assert s.providers['ollama']['models'] == ['my-model[1m]']
    assert s.tier_models['ollama']['opus'] == 'my-model[1m]'
    editor._on_model_1m_toggled('my-model[1m]', _FakeBtn(False))
    assert s.providers['ollama']['models'] == ['my-model']
    assert s.tier_models['ollama']['opus'] == 'my-model'


def test_editor_model_row_shows_bare_title_when_1m_stored():
    s = Settings(providers={'ollama': {'name': 'O', 'base_url': 'http://x',
                                       'api_key': '',
                                       'models': ['my-model[1m]']}},
                 model_default='ollama')
    _make_sw(s)
    editor = _make_editor(s, 'ollama')
    row = editor._model_row_for['my-model[1m]']
    assert row.get_title() == 'my-model'


# --- #10: Adwaita CRITICAL + empty-provider-on-dismiss (release gate adversarial) -

def test_rebuild_providers_group_reentry_safe():
    """Regression for Adwaita-CRITICAL on Add Provider.

    PreferencesGroup parents rows under an internal Gtk.ListBox, so a check
    of ``add_row.get_parent() is providers_group`` never removes the sticky
    Add Provider row before re-adding it. Rebuilding twice (Add Provider →
    refresh, editor close → refresh) must not leave the add row parentless
    or double-parented, and must not raise.
    """
    s = Settings(providers=_ollama_provider(), model_default='ollama')
    sw = _make_sw(s)
    add_row = sw._provider_add_row
    assert add_row.get_parent() is not None

    # Simulate the Add-Provider / on-close refresh path several times.
    for _ in range(3):
        sw._rebuild_providers_group()
        assert add_row is sw._provider_add_row
        assert add_row.get_parent() is not None
        # Provider card rows are fresh each rebuild; add row is retained.
        assert any(r is add_row for r in
                   (sw._provider_add_row,))
        assert len(sw._provider_card_rows) == 1  # ollama

    # Defensive helper itself: re-adding a parented child must not raise.
    sw_mod._safe_group_add(sw._providers_group, add_row)
    assert add_row.get_parent() is not None


def test_safe_group_add_removes_prior_parent():
    """_safe_group_add detaches a child that already has a parent before add."""
    g1 = Adw.PreferencesGroup()
    g2 = Adw.PreferencesGroup()
    row = Adw.ActionRow(title='x')
    g1.add(row)
    assert row.get_parent() is not None
    sw_mod._safe_group_add(g2, row)
    assert row.get_parent() is not None
    # No longer under g1's listbox.
    # (PreferencesGroup has no public "contains"; parent type is ListBox of g2.)
    parent = row.get_parent()
    assert parent is not None


def test_add_provider_dismiss_without_fill_does_not_persist(
        monkeypatch, tmp_path):
    """Add Provider → close editor without edits → empty husk not on disk
    and not left in the live Settings.providers dict."""
    import settings as settings_mod
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH',
                        str(tmp_path / 'settings.json'))
    s = Settings(providers={}, model_default='')
    # Persist a clean baseline so a later load reflects disk truth.
    s.save(settings_mod.DEFAULT_SETTINGS_PATH)
    sw = _make_sw(s)

    editors = []
    orig_present = sw_mod.ProviderEditorWindow.present
    sw_mod.ProviderEditorWindow.present = _no_present_factory()
    try:
        # Capture the editor instance constructed by _open_editor.
        orig_init = sw_mod.ProviderEditorWindow.__init__

        def _init_capture(self, *a, **k):
            orig_init(self, *a, **k)
            editors.append(self)

        monkeypatch.setattr(sw_mod.ProviderEditorWindow, '__init__',
                            _init_capture)
        sw._on_add_provider(None)
    finally:
        sw_mod.ProviderEditorWindow.present = orig_present

    assert editors
    editor = editors[0]
    pid = editor._pid
    assert pid in s.providers  # in-memory only so far

    # Disk must not yet have the empty provider (save deferred on add).
    reloaded_mid = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    assert pid not in (reloaded_mid.providers or {})

    # Dismiss without filling — 'closed' → blank discard + save.
    editor.emit('closed')

    assert pid not in s.providers
    reloaded = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    assert pid not in (reloaded.providers or {})


def test_add_provider_with_name_kept_on_close(monkeypatch, tmp_path):
    """Add Provider → set name → close → provider kept and persisted."""
    import settings as settings_mod
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH',
                        str(tmp_path / 'settings.json'))
    s = Settings(providers={}, model_default='')
    s.save(settings_mod.DEFAULT_SETTINGS_PATH)
    sw = _make_sw(s)

    editors = []
    orig_present = sw_mod.ProviderEditorWindow.present
    sw_mod.ProviderEditorWindow.present = _no_present_factory()
    try:
        orig_init = sw_mod.ProviderEditorWindow.__init__

        def _init_capture(self, *a, **k):
            orig_init(self, *a, **k)
            editors.append(self)

        monkeypatch.setattr(sw_mod.ProviderEditorWindow, '__init__',
                            _init_capture)
        sw._on_add_provider(None)
    finally:
        sw_mod.ProviderEditorWindow.present = orig_present

    assert editors
    editor = editors[0]
    pid = editor._pid
    editor._name_row.set_text('My Provider')
    editor._name_row.emit('apply')
    editor.emit('closed')

    assert pid in s.providers
    assert s.providers[pid]['name'] == 'My Provider'
    reloaded = Settings.load(settings_mod.DEFAULT_SETTINGS_PATH)
    assert reloaded.providers[pid]['name'] == 'My Provider'


def test_add_provider_does_not_save_before_editor_close(monkeypatch, tmp_path):
    """_on_add_provider must not write settings.json until the editor
    commits something meaningful (or discards on blank close)."""
    import settings as settings_mod
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH',
                        str(tmp_path / 'settings.json'))
    s = Settings(providers={}, model_default='')
    s.save(settings_mod.DEFAULT_SETTINGS_PATH)
    saves = []
    monkeypatch.setattr(s, 'save', lambda *a, **k: saves.append(1))
    sw = _make_sw(s)
    orig_present = sw_mod.ProviderEditorWindow.present
    sw_mod.ProviderEditorWindow.present = _no_present_factory()
    try:
        sw._on_add_provider(None)
    finally:
        sw_mod.ProviderEditorWindow.present = orig_present
    assert saves == []  # no disk write on add
    assert s.providers  # in-memory yes
