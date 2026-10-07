"""Per-harness provider/model pin memory (harness_axis_memory).

the maintainer's sequence: pin project to custom provider under Claude Code, switch to
Kimi, switch back to Claude → provider must still be the custom pin, not the
global Ollama default. Flat provider_overrides / model_pins stay runtime SoT;
harness_axis_memory is a lazy side map written on harness leave.
"""
import json
import types

from hosts import override_key
from settings import (
    Settings,
    apply_project_axes,
    remember_and_switch_axes,
    scrub_provider_from_axis_memory,
    snapshot_project_axes,
)


def _providers():
    return {
        'ollama': {
            'name': 'Ollama',
            'base_url': 'http://host:11434/v1',
            'api_key': 'x',
            'models': ['qwen'],
        },
        'kimi-code': {
            'name': 'Kimi Code',
            'base_url': 'https://api.moonshot.cn/v1',
            'api_key': 'y',
            'models': ['kimi-k2'],
        },
    }


PATH = '/home/user/proj'
HOST = 'localhost'
REF = override_key(PATH, HOST)


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_snapshot_omits_absent_provider_and_model():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'})
    assert snapshot_project_axes(s, PATH, HOST, harness_id='claude') == {}
    assert snapshot_project_axes(s, PATH, HOST, harness_id='grok') == {}


def test_snapshot_encodes_native_and_custom_provider():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: ''})
    assert snapshot_project_axes(s, PATH, HOST, harness_id='claude') == {
        'provider': ''}

    s2 = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                  provider_overrides={REF: 'kimi-code'})
    assert snapshot_project_axes(s2, PATH, HOST, harness_id='claude') == {
        'provider': 'kimi-code'}


def test_snapshot_encodes_model_pin():
    s = Settings(model_pins={REF: 'grok-model-x'})
    assert snapshot_project_axes(s, PATH, HOST, harness_id='grok') == {
        'model': 'grok-model-x'}


def test_snapshot_ownership_scoped_omits_unowned_axis():
    """Ownership: claude stashes provider only; grok stashes BOTH axes;
    opencode/kimi stash model only."""
    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        provider_overrides={REF: 'kimi-code'},
        model_pins={REF: 'grok-model-x'},
    )
    assert snapshot_project_axes(s, PATH, HOST, harness_id='claude') == {
        'provider': 'kimi-code'}
    assert 'model' not in snapshot_project_axes(
        s, PATH, HOST, harness_id='claude')
    assert snapshot_project_axes(s, PATH, HOST, harness_id='grok') == {
        'provider': 'kimi-code', 'model': 'grok-model-x'}
    assert snapshot_project_axes(s, PATH, HOST, harness_id='opencode') == {
        'model': 'grok-model-x'}
    assert 'provider' not in snapshot_project_axes(
        s, PATH, HOST, harness_id='opencode')
    # Unknown harness → empty (no junk under a stranger id).
    assert snapshot_project_axes(s, PATH, HOST, harness_id='future-bot') == {}


def test_apply_claude_restores_provider_clears_model():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 model_pins={REF: 'stale-model'})
    apply_project_axes(s, PATH, HOST, 'claude', {'provider': 'kimi-code'})
    assert s.provider_overrides.get(REF) == 'kimi-code'
    assert REF not in s.model_pins
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'


def test_apply_claude_native_pin():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'})
    apply_project_axes(s, PATH, HOST, 'claude', {'provider': ''})
    assert s.provider_overrides.get(REF) == ''
    assert s.effective_provider(PATH, 'claude') == ''


def test_apply_grok_restores_model_clears_provider():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'})
    apply_project_axes(s, PATH, HOST, 'grok', {'model': 'm1'})
    assert s.model_pins.get(REF) == 'm1'
    assert REF not in s.provider_overrides
    assert s.effective_model(PATH) == 'm1'


def test_apply_grok_restores_provider_and_model():
    """Grok is dual-axis since 2026-10-05: a full memory entry restores both
    the provider override and the model pin; an entry missing one axis
    restores only that axis (the other stays cleared)."""
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'})
    apply_project_axes(s, PATH, HOST, 'grok',
                       {'provider': 'kimi-code', 'model': 'm1'})
    assert s.provider_overrides.get(REF) == 'kimi-code'
    assert s.model_pins.get(REF) == 'm1'
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'
    assert s.effective_model(PATH) == 'm1'

    s2 = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                  provider_overrides={REF: 'stale'})
    apply_project_axes(s2, PATH, HOST, 'grok', {'model': 'm1'})
    assert s2.model_pins.get(REF) == 'm1'
    assert REF not in s2.provider_overrides  # no provider key → cleared


def test_apply_empty_memory_clears_both():
    s = Settings(providers=_providers(),
                 provider_overrides={REF: 'kimi-code'},
                 model_pins={REF: 'm1'})
    apply_project_axes(s, PATH, HOST, 'claude', {})
    assert REF not in s.provider_overrides
    assert REF not in s.model_pins


def test_apply_unknown_harness_clears_both_no_restore():
    s = Settings(providers=_providers(),
                 provider_overrides={REF: 'kimi-code'},
                 model_pins={REF: 'm1'})
    # Memory entry may hold junk; unknown id must not restore either axis.
    apply_project_axes(
        s, PATH, HOST, 'future-bot',
        {'provider': 'kimi-code', 'model': 'm1'})
    assert REF not in s.provider_overrides
    assert REF not in s.model_pins


def test_same_harness_is_noop():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'})
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'claude')
    assert s.provider_overrides.get(REF) == 'kimi-code'
    assert s.harness_axis_memory == {}


# ── T1–T4 binding cases ───────────────────────────────────────────────────────

def test_t1_claude_custom_provider_survives_kimi_roundtrip():
    """model_default=ollama, pin kimi-code, claude→kimi→claude → still custom."""
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'},
                 harness_default='claude')
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'kimi')
    # First visit to kimi: both pins cleared (model-pin ownership).
    assert s.effective_provider(PATH, 'claude') == 'ollama'
    assert s.effective_model(PATH) == ''
    # Memory holds claude's pin.
    assert s.harness_axis_memory[REF]['claude'] == {'provider': 'kimi-code'}

    remember_and_switch_axes(s, PATH, HOST, 'kimi', 'claude')
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'


def test_t2_no_pin_follows_default_after_roundtrip():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 harness_default='claude')
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'kimi')
    remember_and_switch_axes(s, PATH, HOST, 'kimi', 'claude')
    assert s.effective_provider(PATH, 'claude') == 'ollama'
    # Follow-default encoded as omitted provider key.
    assert 'provider' not in s.harness_axis_memory[REF].get('claude', {})
    assert REF not in s.provider_overrides


def test_t3_explicit_native_survives_roundtrip():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: ''},
                 harness_default='claude')
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'kimi')
    remember_and_switch_axes(s, PATH, HOST, 'kimi', 'claude')
    assert REF in s.provider_overrides
    assert s.provider_overrides[REF] == ''
    assert s.effective_provider(PATH, 'claude') == ''


def test_t4_grok_model_pin_survives_claude_roundtrip():
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 model_pins={REF: 'grok-fast'},
                 harness_default='grok',
                 harness_overrides={REF: 'grok'})
    remember_and_switch_axes(s, PATH, HOST, 'grok', 'claude')
    assert s.effective_model(PATH) == ''
    assert s.harness_axis_memory[REF]['grok'] == {'model': 'grok-fast'}

    remember_and_switch_axes(s, PATH, HOST, 'claude', 'grok')
    assert s.effective_model(PATH) == 'grok-fast'


def test_t4b_grok_provider_and_model_survive_claude_roundtrip():
    """Grok is dual-axis: provider override AND model pin both survive a
    claude detour (claude snapshots/restores provider only, so its own pin
    never clobbers grok's side of the memory)."""
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'},
                 model_pins={REF: 'grok-fast'},
                 harness_default='grok',
                 harness_overrides={REF: 'grok'})
    remember_and_switch_axes(s, PATH, HOST, 'grok', 'claude')
    assert s.harness_axis_memory[REF]['grok'] == {
        'provider': 'kimi-code', 'model': 'grok-fast'}
    # Entering claude with no claude memory: both flat pins cleared.
    assert s.effective_provider(PATH, 'claude') == 'ollama'
    assert s.effective_model(PATH) == ''

    remember_and_switch_axes(s, PATH, HOST, 'claude', 'grok')
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'
    assert s.effective_model(PATH) == 'grok-fast'


def test_grok_native_provider_pin_survives_roundtrip():
    """Explicit native ('') is a real grok pin now — it must round-trip."""
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: ''},
                 harness_default='grok')
    remember_and_switch_axes(s, PATH, HOST, 'grok', 'claude')
    assert s.harness_axis_memory[REF]['grok'] == {'provider': ''}
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'grok')
    assert REF in s.provider_overrides
    assert s.provider_overrides[REF] == ''
    assert s.effective_provider(PATH, 'claude') == ''


def test_grok_provider_pin_survives_opencode_detour_and_never_leaks():
    """grok→opencode→grok: opencode owns model only, so the grok provider pin
    must NOT follow the project onto opencode (flat provider_overrides is
    cleared while opencode is active) — and must be fully restored on return."""
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'},
                 model_pins={REF: 'grok-fast'},
                 harness_default='grok',
                 harness_overrides={REF: 'grok'})
    remember_and_switch_axes(s, PATH, HOST, 'grok', 'opencode')
    # On opencode: provider axis cleared (model-only ownership) — the grok
    # pin neither applies nor leaks into opencode's flat state.
    assert REF not in s.provider_overrides
    assert s.effective_provider(PATH, 'claude') == 'ollama'
    assert s.effective_model(PATH) == ''
    assert s.harness_axis_memory[REF]['grok'] == {
        'provider': 'kimi-code', 'model': 'grok-fast'}

    remember_and_switch_axes(s, PATH, HOST, 'opencode', 'grok')
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'
    assert s.effective_model(PATH) == 'grok-fast'


def test_model_axis_signature_grok_combines_provider_and_model():
    """Restart-staleness: grok's signature changes when EITHER the effective
    provider or the model pin changes (like claude's provider axis)."""
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 harness_default='grok')
    base = s.model_axis_signature(PATH)
    s.provider_overrides = {REF: 'kimi-code'}
    assert s.model_axis_signature(PATH) != base
    s.provider_overrides = {}
    s.model_pins = {REF: 'grok-fast'}
    assert s.model_axis_signature(PATH) != base
    # opencode/kimi stay model-pin-shaped.
    s2 = Settings(model_pins={REF: 'oc-m'}, harness_default='opencode')
    assert s2.model_axis_signature(PATH) == 'oc-m'


def test_opencode_and_kimi_model_ownership():
    for hid in ('opencode', 'kimi'):
        s = Settings(model_pins={REF: f'{hid}-m'})
        remember_and_switch_axes(s, PATH, HOST, hid, 'claude')
        remember_and_switch_axes(s, PATH, HOST, 'claude', hid)
        assert s.effective_model(PATH) == f'{hid}-m'


# ── T6 save/load ──────────────────────────────────────────────────────────────

def test_t6_save_load_preserves_harness_axis_memory(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(providers=_providers(), provider_defaults={'claude': 'ollama'},
                 provider_overrides={REF: 'kimi-code'})
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'kimi')
    s.save(path)
    s2 = Settings.load(path)
    assert s2.harness_axis_memory[REF]['claude'] == {'provider': 'kimi-code'}
    remember_and_switch_axes(s2, PATH, HOST, 'kimi', 'claude')
    assert s2.effective_provider(PATH, 'claude') == 'kimi-code'


def test_load_normalizes_bare_path_keys(tmp_path):
    path = str(tmp_path / 'settings.json')
    with open(path, 'w') as f:
        json.dump({
            'harness_axis_memory': {
                PATH: {
                    'claude': {'provider': 'kimi-code', 'model': ''},
                    'grok': {'model': 'm1', 'junk': 1},
                    12: {'provider': 'x'},  # non-str harness id dropped
                },
            },
            'providers': _providers(),
        }, f)
    s = Settings.load(path)
    assert REF in s.harness_axis_memory
    assert s.harness_axis_memory[REF]['claude'] == {'provider': 'kimi-code'}
    assert s.harness_axis_memory[REF]['grok'] == {'model': 'm1'}


def test_default_field_empty():
    assert Settings().harness_axis_memory == {}


# ── scrub deleted provider ────────────────────────────────────────────────────

def test_scrub_provider_from_axis_memory():
    s = Settings(harness_axis_memory={
        REF: {
            'claude': {'provider': 'kimi-code'},
            'grok': {'model': 'm1'},
        },
        'local:/other': {'claude': {'provider': 'ollama'}},
    })
    scrub_provider_from_axis_memory(s, 'kimi-code')
    assert 'provider' not in s.harness_axis_memory[REF]['claude']
    assert s.harness_axis_memory[REF]['grok'] == {'model': 'm1'}
    assert s.harness_axis_memory['local:/other']['claude'] == {'provider': 'ollama'}


def test_t5_scrub_then_switch_back_does_not_resurrect():
    """Scrub deleted provider from memory → switch back to claude → not restored."""
    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        provider_overrides={REF: 'kimi-code'},
        harness_default='claude',
        harness_axis_memory={
            REF: {'claude': {'provider': 'kimi-code'}},
        },
    )
    # Simulate delete of custom provider: drop from providers map + scrub memory
    # + clear flat override (settings_window does the flat clear).
    del s.providers['kimi-code']
    s.providers = dict(s.providers)
    scrub_provider_from_axis_memory(s, 'kimi-code')
    s.provider_overrides = {}

    # Leave claude (empty provider snap) for kimi, then return to claude.
    remember_and_switch_axes(s, PATH, HOST, 'claude', 'kimi')
    remember_and_switch_axes(s, PATH, HOST, 'kimi', 'claude')

    assert s.effective_provider(PATH, 'claude') != 'kimi-code'
    assert s.effective_provider(PATH, 'claude') == 'ollama'  # model_default
    assert REF not in s.provider_overrides or s.provider_overrides.get(REF) != 'kimi-code'
    mem_claude = s.harness_axis_memory.get(REF, {}).get('claude', {})
    assert mem_claude.get('provider') != 'kimi-code'


# ── remote project_ref keys ───────────────────────────────────────────────────

def test_remote_ref_provider_roundtrip():
    """ssh:host:proj keys: pin provider, leave and return, restore works."""
    rpath = 'ssh:remotehost:myproj'
    rhost = 'remotehost'
    rref = override_key(rpath, rhost)
    assert rref.startswith('ssh:')

    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        provider_overrides={rref: 'kimi-code'},
        harness_default='claude',
    )
    remember_and_switch_axes(s, rpath, rhost, 'claude', 'kimi')
    assert s.harness_axis_memory[rref]['claude'] == {'provider': 'kimi-code'}
    assert rref not in s.provider_overrides

    remember_and_switch_axes(s, rpath, rhost, 'kimi', 'claude')
    assert s.provider_overrides.get(rref) == 'kimi-code'
    assert s.effective_provider(rpath, 'claude', host_id=rhost) == 'kimi-code'


# ── T7 unbound window handler ─────────────────────────────────────────────────

def test_t7_on_project_harness_change_restores_provider(tmp_path):
    """BINDING: full handler sequence claude(kimi-code)→kimi→claude restores pin."""
    from window import AppWindow

    settings_path = str(tmp_path / 'settings.json')
    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        provider_overrides={REF: 'kimi-code'},
        harness_default='claude',
    )
    s.save(settings_path)
    # Point Settings.save used by handler at tmp file.
    toasts = []
    restarts = []
    apply_calls = []
    clear_calls = []

    fake_tv = types.SimpleNamespace(
        clear_explicit_harness=lambda: clear_calls.append(True),
    )
    fake = types.SimpleNamespace(
        _settings=s,
        _terminals={PATH: fake_tv},
        _find_project=lambda path: types.SimpleNamespace(
            name='proj', host_id=HOST, path=path),
        apply_settings=lambda settings: apply_calls.append(settings),
        _show_toast=lambda text, timeout=5: toasts.append(text),
        _maybe_prompt_restart=lambda path: restarts.append(path),
    )

    # Monkeypatch save so we don't write home settings.
    orig_save = s.save
    s.save = lambda path=None: orig_save(settings_path)

    AppWindow._on_project_harness_change(fake, object(), PATH, 'kimi')
    assert s.effective_harness(PATH) == 'kimi'
    assert s.effective_provider(PATH, 'claude') == 'ollama'
    assert any('Kimi' in t or 'kimi' in t.lower() for t in toasts)
    assert clear_calls  # clear_explicit_harness on harness switch

    AppWindow._on_project_harness_change(fake, object(), PATH, 'claude')
    assert s.effective_harness(PATH) == 'claude'
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'
    assert apply_calls  # apply_settings invoked
    assert restarts == [PATH, PATH]
    assert len(clear_calls) == 2  # once per switch


def test_t7_follow_default_clears_harness_override_keeps_memory(tmp_path):
    from models import FOLLOW_DEFAULT
    from window import AppWindow

    settings_path = str(tmp_path / 'settings.json')
    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        provider_overrides={REF: 'kimi-code'},
        harness_default='claude',
        harness_overrides={REF: 'kimi'},
    )
    # Currently on kimi (override); switch "follow default" → claude, restore pin.
    fake = types.SimpleNamespace(
        _settings=s,
        _terminals={},
        _find_project=lambda path: types.SimpleNamespace(
            name='proj', host_id=HOST, path=path),
        apply_settings=lambda settings: None,
        _show_toast=lambda text, timeout=5: None,
        _maybe_prompt_restart=lambda path: None,
    )
    s.save = lambda path=None: None

    # Seed memory as if we had visited claude with the pin earlier, then left for kimi.
    s.harness_axis_memory = {REF: {'claude': {'provider': 'kimi-code'}}}
    # Flat axes currently reflect kimi (no provider pin).
    s.provider_overrides = {}

    AppWindow._on_project_harness_change(fake, object(), PATH, FOLLOW_DEFAULT)
    assert REF not in s.harness_overrides
    assert s.effective_harness(PATH) == 'claude'
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'


def test_grok_custom_provider_pick_writes_override(tmp_path):
    """BINDING: picking a custom provider for a GROK project writes
    provider_overrides + saves + offers restart (same as claude)."""
    from window import AppWindow

    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        harness_default='grok',
        harness_overrides={REF: 'grok'},
    )
    saves = []
    restarts = []
    refreshes = []
    fake = types.SimpleNamespace(
        _settings=s,
        _find_project=lambda path: types.SimpleNamespace(
            name='proj', host_id=HOST, path=path),
        _refresh_sidebar_models=lambda: refreshes.append(True),
        _maybe_prompt_restart=lambda path: restarts.append(path),
    )
    s.save = lambda path=None: saves.append(True)

    AppWindow._on_project_model_change(fake, object(), PATH, 'kimi-code')
    assert s.provider_overrides.get(REF) == 'kimi-code'
    assert s.effective_provider(PATH, 'claude') == 'kimi-code'
    assert saves and refreshes and restarts == [PATH]


def test_opencode_custom_provider_pick_still_ignored(tmp_path):
    """OpenCode/Kimi remain native-only: a custom pick is silently ignored
    (no override written, no save, no restart prompt)."""
    from window import AppWindow

    s = Settings(
        providers=_providers(),
        provider_defaults={'claude': 'ollama'},
        harness_default='opencode',
        harness_overrides={REF: 'opencode'},
    )
    saves = []
    restarts = []
    fake = types.SimpleNamespace(
        _settings=s,
        _find_project=lambda path: types.SimpleNamespace(
            name='proj', host_id=HOST, path=path),
        _refresh_sidebar_models=lambda: None,
        _maybe_prompt_restart=lambda path: restarts.append(path),
    )
    s.save = lambda path=None: saves.append(True)

    AppWindow._on_project_model_change(fake, object(), PATH, 'kimi-code')
    assert REF not in s.provider_overrides
    assert not saves
    assert not restarts
