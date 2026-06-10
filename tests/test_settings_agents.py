"""Tests for the agent-aware settings fields + claude_binary migration (P1).

Back-compat is the load-bearing requirement: a current-format user
settings.json (with ``claude_binary`` and no ``agents`` key) must load with
behavior unchanged. After load, ``claude_binary`` is migrated into
``agents['claude']['binary']`` but the old key stays honored when ``agents`` is
absent, and ``resolved_claude_binary`` keeps returning the same value either
way.
"""
import json

from settings import Settings


# ── new field defaults ───────────────────────────────────────────────────────

def test_agent_field_defaults():
    s = Settings()
    assert s.agent_default == 'claude'
    assert s.agent_overrides == {}
    # agents defaults to empty; the migration path fills claude on load.
    assert isinstance(s.agents, dict)


# ── effective_agent mirrors effective_model ──────────────────────────────────

def test_effective_agent_default_when_no_overrides():
    s = Settings()
    assert s.effective_agent('/p/a') == 'claude'


def test_effective_agent_global_default():
    s = Settings(agent_default='opencode')
    assert s.effective_agent('/p/a') == 'opencode'


def test_effective_agent_per_project_override():
    s = Settings(agent_default='claude', agent_overrides={'/p/a': 'opencode'})
    assert s.effective_agent('/p/a') == 'opencode'
    assert s.effective_agent('/p/b') == 'claude'


def test_effective_agent_empty_override_falls_back_to_default():
    """An override stored as '' is treated as 'use the default', not 'no agent'."""
    s = Settings(agent_default='claude', agent_overrides={'/p/a': ''})
    assert s.effective_agent('/p/a') == 'claude'


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
    assert s.agents.get('claude', {}).get('binary') == '/opt/claude'
    # And the resolved property keeps returning the same value.
    assert s.resolved_claude_binary == '/opt/claude'


def test_load_empty_claude_binary_does_not_create_bogus_entry(tmp_path):
    """A blank legacy claude_binary should not forge a misleading binary value."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 12}))
    s = Settings.load(str(path))
    # claude agent entry may exist but its binary must be falsy.
    assert not s.agents.get('claude', {}).get('binary')
    assert s.resolved_claude_binary == 'claude'


def test_agents_binary_takes_precedence_when_present(tmp_path):
    """When agents['claude']['binary'] is set, it drives resolved_claude_binary."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({
        'claude_binary': '/old/claude',
        'agents': {'claude': {'binary': '/new/claude'}},
    }))
    s = Settings.load(str(path))
    assert s.resolved_claude_binary == '/new/claude'


def test_agents_present_legacy_key_absent(tmp_path):
    """agents set, claude_binary absent → resolved comes from agents."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'agents': {'claude': {'binary': '/a/claude'}}}))
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
    assert s2.agents.get('claude', {}).get('binary') == '/opt/claude'


def test_agent_overrides_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(agent_default='claude', agent_overrides={'/p/a': 'opencode'})
    s.save(path)
    s2 = Settings.load(path)
    assert s2.agent_default == 'claude'
    assert s2.agent_overrides == {'/p/a': 'opencode'}


def test_agent_overrides_roundtrip_grok(tmp_path):
    """T-B4: a per-project grok override + a grok binary entry survive a
    save/load round-trip (the third agent is no different from the others)."""
    path = str(tmp_path / 'settings.json')
    s = Settings(agent_default='grok',
                 agent_overrides={'/p/g': 'grok'},
                 agents={'grok': {'binary': '/opt/grok/grok'}})
    s.save(path)
    s2 = Settings.load(path)
    assert s2.agent_default == 'grok'
    assert s2.agent_overrides == {'/p/g': 'grok'}
    assert s2.agents.get('grok', {}).get('binary') == '/opt/grok/grok'
    assert s2.effective_agent('/p/g') == 'grok'


def test_existing_provider_and_ccr_fields_still_default(tmp_path):
    """Adding agent fields must not disturb the existing model/ccr defaults."""
    s = Settings()
    assert s.providers == {}
    assert s.model_default == ''
    assert s.ccr_host == '127.0.0.1'
    assert s.ccr_port == 3456
