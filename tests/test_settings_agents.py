"""Tests for the harness/binary settings + claude_binary migration.

Claude Code is the sole harness. The back-compat requirement that survives the
multi-harness → Claude-Only pivot: a current-format user settings.json (with
``claude_binary`` and no ``agents`` key) must load with behavior unchanged.
After load, ``claude_binary`` is migrated into ``agents['claude']['binary']``
but the old key stays honored when ``agents`` is absent, and
``resolved_claude_binary`` keeps returning the same value either way.
"""
import json

from settings import Settings, TIERS


# ── field defaults ───────────────────────────────────────────────────────────

def test_field_defaults():
    s = Settings()
    # agents defaults to empty; the migration path fills claude on load.
    assert isinstance(s.agents, dict)
    # The old multi-harness fields are gone.
    assert not hasattr(s, 'agent_default')
    assert not hasattr(s, 'agent_overrides')
    assert not hasattr(s, 'ccr_managed')
    # The new model-axis fields exist.
    assert s.providers == {}
    assert s.model_default == ''
    assert s.model_overrides == {}
    assert s.tier_models == {}


# ── effective_agent is a kept back-compat symbol, always claude ──────────────

def test_effective_agent_always_claude():
    assert Settings().effective_agent('/p/a') == 'claude'
    assert Settings(providers={'p': {'models': ['m']}},
                     model_default='p').effective_agent('/p/a') == 'claude'


# ── tier_models + provider round-trip ────────────────────────────────────────

def test_tier_models_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(providers={'ollama': {'name': 'O', 'base_url': 'http://x',
                                         'api_key': 'k', 'models': ['a', 'b']}},
                 model_default='ollama',
                 tier_models={'opus': 'b', 'sonnet': 'a', 'haiku': '', 'subagent': ''})
    s.save(path)
    s2 = Settings.load(path)
    assert s2.tier_models == {'opus': 'b', 'sonnet': 'a', 'haiku': '',
                              'subagent': '', 'fable': ''}


def test_tier_models_only_canonical_keys_persisted(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(tier_models={'opus': 'x', 'sonnet': '', 'haiku': '', 'subagent': ''})
    s.save(path)
    s2 = Settings.load(path)
    assert set(s2.tier_models.keys()) <= set(TIERS)


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


def test_model_axis_defaults_undisturbed_by_binary_migration():
    """Adding agents/binary fields must not disturb the model-axis defaults."""
    s = Settings()
    assert s.providers == {}
    assert s.model_default == ''
    assert s.tier_models == {}