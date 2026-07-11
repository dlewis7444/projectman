"""Tests for the harness-aware settings fields + claude_binary migration (P1).

Back-compat is the load-bearing requirement: a current-format user
settings.json (with ``claude_binary`` and no ``agents`` key) must load with
behavior unchanged. After load, ``claude_binary`` is migrated into
``agents['claude']['binary']`` but the old key stays honored when ``agents`` is
absent, and ``resolved_claude_binary`` keeps returning the same value either
way.
"""
import json
from pathlib import Path

from settings import Settings


# ── new field defaults ───────────────────────────────────────────────────────

def test_agent_field_defaults():
    s = Settings()
    assert s.harness_default == 'claude'
    assert s.harness_overrides == {}
    # agents defaults to empty; the migration path fills claude on load.
    assert isinstance(s.harnesses, dict)


# ── effective_harness mirrors effective_model ──────────────────────────────────

def test_effective_harness_default_when_no_overrides():
    s = Settings()
    assert s.effective_harness('/p/a') == 'claude'


def test_effective_harness_global_default():
    s = Settings(harness_default='opencode')
    assert s.effective_harness('/p/a') == 'opencode'


def test_effective_harness_per_project_override():
    s = Settings(harness_default='claude', harness_overrides={'/p/a': 'opencode'})
    assert s.effective_harness('/p/a') == 'opencode'
    assert s.effective_harness('/p/b') == 'claude'


def test_effective_harness_empty_override_falls_back_to_default():
    """An override stored as '' is treated as 'use the default', not 'no agent'."""
    s = Settings(harness_default='claude', harness_overrides={'/p/a': ''})
    assert s.effective_harness('/p/a') == 'claude'


# ── claude_binary migration: old key still honored when agents absent ─────────

def test_resolved_claude_binary_from_legacy_key_unchanged():
    """No agents map → resolved binary still comes from claude_binary."""
    s = Settings(claude_binary='/usr/local/bin/claude')
    assert s.resolved_claude_binary == '/usr/local/bin/claude'


def test_resolved_claude_binary_empty_still_defaults():
    assert Settings().resolved_claude_binary == 'claude'


def test_load_legacy_settings_honors_claude_binary(tmp_path):
    """A current-format file (claude_binary, no agents) loads unchanged."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({
        'claude_binary': '/opt/claude',
        'font_size': 13,
    }))
    s = Settings.load(str(path))
    assert s.resolved_claude_binary == '/opt/claude'
    assert s.font_size == 13


def test_load_migrates_claude_binary_into_agents(tmp_path):
    """On load, a legacy claude_binary is mirrored into agents['claude']['binary']."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'claude_binary': '/opt/claude'}))
    s = Settings.load(str(path))
    assert s.harnesses.get('claude', {}).get('binary') == '/opt/claude'
    # And the resolved property keeps returning the same value.
    assert s.resolved_claude_binary == '/opt/claude'


def test_load_empty_claude_binary_does_not_create_bogus_entry(tmp_path):
    """A blank legacy claude_binary should not forge a misleading binary value."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 12}))
    s = Settings.load(str(path))
    # claude agent entry may exist but its binary must be falsy.
    assert not s.harnesses.get('claude', {}).get('binary')
    assert s.resolved_claude_binary == 'claude'


def test_agents_binary_takes_precedence_when_present(tmp_path):
    """When agents['claude']['binary'] is set, it drives resolved_claude_binary."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({
        'claude_binary': '/old/claude',
        'harnesses': {'claude': {'binary': '/new/claude'}},
    }))
    s = Settings.load(str(path))
    assert s.resolved_claude_binary == '/new/claude'


def test_agents_present_legacy_key_absent(tmp_path):
    """agents set, claude_binary absent → resolved comes from harnesses."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'harnesses': {'claude': {'binary': '/a/claude'}}}))
    s = Settings.load(str(path))
    assert s.resolved_claude_binary == '/a/claude'


# ── round-trip of the migrated form ──────────────────────────────────────────

def test_save_then_load_roundtrips_migrated_form(tmp_path):
    path = str(tmp_path / 'settings.json')
    # The migration happens on load; save persists asdict(self).
    s = Settings(claude_binary='/opt/claude')
    s.save(path)
    s2 = Settings.load(path)
    assert s2.resolved_claude_binary == '/opt/claude'
    # migrated form is present after reload
    assert s2.harnesses.get('claude', {}).get('binary') == '/opt/claude'


def test_harness_overrides_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(harness_default='claude', harness_overrides={'/p/a': 'opencode'})
    s.save(path)
    s2 = Settings.load(path)
    assert s2.harness_default == 'claude'
    # Host axis: bare paths normalize to local:<path> on load.
    assert s2.harness_overrides == {'local:/p/a': 'opencode'}
    assert s2.effective_harness('/p/a') == 'opencode'


def test_harness_overrides_roundtrip_grok(tmp_path):
    """T-B4: a per-project grok override + a grok binary entry survive a
    save/load round-trip (the third agent is no different from the others)."""
    path = str(tmp_path / 'settings.json')
    s = Settings(harness_default='grok',
                 harness_overrides={'/p/g': 'grok'},
                 harnesses={'grok': {'binary': '/opt/grok/grok'}})
    s.save(path)
    s2 = Settings.load(path)
    assert s2.harness_default == 'grok'
    assert s2.harness_overrides == {'local:/p/g': 'grok'}
    assert s2.harnesses.get('grok', {}).get('binary') == '/opt/grok/grok'
    assert s2.effective_harness('/p/g') == 'grok'


def test_existing_provider_fields_still_default(tmp_path):
    """Adding agent fields must not disturb the existing model-axis defaults."""
    s = Settings()
    assert s.providers == {}
    assert s.model_default == ''
    assert s.provider_overrides == {}
    assert s.model_pins == {}
    assert s.tier_models == {}
    assert not hasattr(s, 'ccr_host')



# ── dual-read legacy agent_* settings keys (terminology rename) ──────────────

def test_load_legacy_agent_keys_migrates_to_harness(tmp_path):
    """settings.json with agent_default / agent_overrides / agents still loads."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({
        'agent_default': 'grok',
        'agent_overrides': {'/p/a': 'opencode'},
        'agents': {'grok': {'binary': '/opt/grok'}},
    }))
    s = Settings.load(str(path))
    assert s.harness_default == 'grok'
    assert s.harness_overrides == {'local:/p/a': 'opencode'}
    assert s.harnesses.get('grok', {}).get('binary') == '/opt/grok'
    assert s.effective_harness('/p/a') == 'opencode'
    assert s.effective_harness('/p/b') == 'grok'


def test_save_writes_harness_keys_not_agent_keys(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(harness_default='opencode',
                 harness_overrides={'/p': 'grok'},
                 harnesses={'opencode': {'binary': '/bin/oc'}})
    s.save(path)
    data = json.loads(Path(path).read_text())
    assert 'agent_default' not in data
    assert 'agent_overrides' not in data
    assert 'agents' not in data
    assert data['harness_default'] == 'opencode'
    assert data['harness_overrides'] == {'/p': 'grok'}
    assert data['harnesses']['opencode']['binary'] == '/bin/oc'
