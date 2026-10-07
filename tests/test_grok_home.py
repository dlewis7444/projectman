"""Tests for the ProjectMan-managed GROK_HOME per custom provider
(models.grok_home_dir / ensure_grok_home) and its wiring into
build_grok_spawn_env.

The WHY (the maintainer's ruling, 2026-10-05): a logged-in native home makes grok send
the xAI SESSION JWT as the bearer on custom-endpoint inference calls (401s
key-auth providers; leaks the token). The managed home is sessionless and its
config wires every catalog model as BYOK, so the session token is never sent
and grok's first-party API-key probe gate is bypassed (probe-verified).
"""
import json
import os
import pathlib

import pytest

import settings as settings_mod
from settings import Settings
from models import (
    build_grok_spawn_env,
    grok_home_dir,
    ensure_grok_home,
)

_PROV = {
    'name': 'Ollama',
    'base_url': 'http://localhost:11434',
    'api_key': '',
    'models': ['kimi-k2.7-code:cloud', 'qwen3.5[1m]', 'qwen3.5'],
}


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolate expanduser('~') so the hook-JSON script path is tmp-scoped and
    we can assert NOTHING is created under the fake ~/.grok."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return home


def _read(home, *parts):
    return pathlib.Path(home, *parts).read_text()


# --- grok_home_dir -----------------------------------------------------------

def test_grok_home_dir_sanitization():
    assert str(grok_home_dir('provider3')).endswith(
        os.path.join('grok-homes', 'provider3'))
    assert str(grok_home_dir('a/b c:ï')).endswith(
        os.path.join('grok-homes', 'a_b_c__'))
    assert str(grok_home_dir('...')).endswith(os.path.join('grok-homes', '...'))


def test_grok_home_dir_uses_settings_resolution(monkeypatch):
    """Same resolution as settings.DEFAULT_SETTINGS_PATH — monkeypatching the
    constant moves the home (conftest already isolates it per-test)."""
    monkeypatch.setattr(settings_mod, 'DEFAULT_SETTINGS_PATH',
                        '/tmp/fake-pm-dir/settings.json')
    assert str(grok_home_dir('x')) == '/tmp/fake-pm-dir/grok-homes/x'


# --- ensure_grok_home --------------------------------------------------------

def test_ensure_grok_home_creates_managed_tree(fake_home):
    home = ensure_grok_home('ollama', _PROV)
    assert home == str(grok_home_dir('ollama'))
    assert os.path.isdir(home)

    cfg = _read(home, 'config.toml')
    # PM header (informational — ownership is tracked in the sidecar).
    assert '# projectman: managed grok home' in cfg
    # Sidecar fingerprint written (survives grok stripping the config header).
    sidecar = pathlib.Path(home, '.pm-fingerprint').read_text().strip()
    assert len(sidecar) == 16
    # compat.claude mirrors install.sh (no claude hook.js double-fire).
    assert '[compat.claude]' in cfg
    assert 'hooks = false' in cfg
    # Grok's own first-launch default content is present on fresh homes.
    assert '[marketplace]' in cfg
    assert 'default_skills_installs_purged = true' in cfg
    # BYOK model blocks: quoted keys (ids carry ':' etc.), derived base_url,
    # key via env_key (never a literal secret in this file).
    assert '[model."kimi-k2.7-code:cloud"]' in cfg
    assert 'model = "kimi-k2.7-code:cloud"' in cfg
    assert 'base_url = "http://localhost:11434/v1"' in cfg
    assert 'env_key = "XAI_API_KEY"' in cfg
    # [1m] stripped and deduped; the flagged id carries context_window.
    assert cfg.count('[model."qwen3.5"]') == 1
    assert 'context_window = 1048576' in _read(home, 'config.toml')

    hook = json.loads(_read(home, 'hooks', 'projectman.json'))
    cmd = hook['hooks']['SessionStart'][0]['hooks'][0]['command']
    assert cmd == f'python3 {fake_home}/.grok/hooks/projectman-status.py'
    assert set(hook['hooks']) == {
        'SessionStart', 'UserPromptSubmit', 'Stop', 'PreToolUse',
        'PostToolUse', 'PostToolUseFailure', 'PermissionDenied',
        'Notification', 'SessionEnd',
    }


def test_managed_config_is_valid_toml(fake_home):
    import tomllib
    home = ensure_grok_home('ollama', _PROV)
    with open(os.path.join(home, 'config.toml'), 'rb') as f:
        doc = tomllib.load(f)
    assert doc['compat']['claude']['hooks'] is False
    assert doc['marketplace']['default_skills_installs_purged'] is True
    entry = doc['model']['kimi-k2.7-code:cloud']
    assert entry['model'] == 'kimi-k2.7-code:cloud'
    assert entry['base_url'] == 'http://localhost:11434/v1'
    assert entry['env_key'] == 'XAI_API_KEY'
    assert 'context_window' not in entry               # no [1m] flag
    assert doc['model']['qwen3.5']['context_window'] == 1048576   # [1m] flagged


def test_ensure_grok_home_idempotent_and_preserves_grok_files(fake_home):
    home = ensure_grok_home('ollama', _PROV)
    # Grok itself populated these on first launch.
    pathlib.Path(home, 'sessions').mkdir()
    pathlib.Path(home, 'agent_id').write_text('agent-1')
    pathlib.Path(home, 'models_cache.json').write_text('{}')
    cfg_before = pathlib.Path(home, 'config.toml').read_bytes()
    hook_before = pathlib.Path(home, 'hooks', 'projectman.json').read_bytes()

    again = ensure_grok_home('ollama', _PROV)
    assert again == home
    assert pathlib.Path(home, 'config.toml').read_bytes() == cfg_before
    assert pathlib.Path(home, 'hooks', 'projectman.json').read_bytes() == \
        hook_before
    # Grok-created siblings untouched.
    assert pathlib.Path(home, 'agent_id').read_text() == 'agent-1'
    assert pathlib.Path(home, 'sessions').is_dir()


def test_ensure_grok_home_rewrites_on_catalog_change(fake_home):
    home = ensure_grok_home('ollama', _PROV)
    pathlib.Path(home, 'agent_id').write_text('agent-1')
    changed = dict(_PROV, models=['kimi-k2.7-code:cloud', 'brand-new'])
    ensure_grok_home('ollama', changed)
    cfg = _read(home, 'config.toml')
    assert '[model."brand-new"]' in cfg
    assert '[model."qwen3.5"]' not in cfg
    assert pathlib.Path(home, 'agent_id').read_text() == 'agent-1'


def test_ensure_grok_home_recovers_from_grok_rewrite(fake_home):
    """Grok rewrites the managed config when the user changes in-app settings:
    our header/marker is stripped and foreign sections appear. PM must refresh
    its own sections (incl. context_window on the [1m] id) while preserving
    foreign sections verbatim — with no in-file marker to rely on (the
    .pm-fingerprint sidecar owns that)."""
    home = ensure_grok_home('ollama', _PROV)
    cfg = pathlib.Path(home, 'config.toml')
    # Simulated grok rewrite: PM sections gone/stale, foreign sections added.
    cfg.write_text(
        '[models]\ndefault = "glm-5.2:cloud"\n\n[ui]\nmax_thoughts_width = 120\n\n'
        '[model."stale"]\nmodel = "stale"\nbase_url = "http://old"\n')
    ensure_grok_home('ollama', _PROV)
    text = cfg.read_text()
    # Foreign sections preserved verbatim.
    assert '[models]\ndefault = "glm-5.2:cloud"' in text
    assert '[ui]\nmax_thoughts_width = 120' in text
    # PM namespace refreshed: stale model block gone, catalog models present,
    # [1m] id carries context_window.
    assert '[model."stale"]' not in text
    assert '[model."qwen3.5"]' in text
    assert 'context_window = 1048576' in text
    # compat.claude normalized back to PM's value.
    assert '[compat.claude]\nhooks = false' in text
    # Sidecar written; second run byte-identical.
    assert pathlib.Path(home, '.pm-fingerprint').read_text().strip()
    before = cfg.read_bytes()
    ensure_grok_home('ollama', _PROV)
    assert cfg.read_bytes() == before


def test_ensure_grok_home_merges_foreign_config(fake_home):
    """A config PM did not write is merged, not clobbered: PM-owned sections
    are (re)generated, foreign sections keep their content. [model.*] is
    PM's namespace even when user-authored — those blocks are overwritten."""
    home = ensure_grok_home('foreign')
    cfg = pathlib.Path(home, 'config.toml')
    cfg.write_text('[ui]\nyolo = false\n')
    ensure_grok_home('foreign')
    text = cfg.read_text()
    assert '[ui]' in text and 'yolo = false' in text
    assert '[compat.claude]' in text and 'hooks = false' in text

    cfg.write_text('[compat.claude]\nhooks = true\n\n[ui]\nx = 1\n')
    ensure_grok_home('foreign')
    text = cfg.read_text()
    assert '[compat.claude]\nhooks = false' in text   # PM-owned, normalized
    assert '[ui]\nx = 1' in text                     # foreign, preserved


def test_sidecar_fingerprint_tracks_catalog(fake_home):
    home = ensure_grok_home('ollama', _PROV)
    sidecar = pathlib.Path(home, '.pm-fingerprint')
    fp1 = sidecar.read_text()
    assert fp1.endswith('\n') and fp1.strip()
    ensure_grok_home('ollama', dict(_PROV, models=['kimi-k2.7-code:cloud']))
    fp2 = sidecar.read_text()
    assert fp2 != fp1
    ensure_grok_home('ollama', dict(_PROV, models=['kimi-k2.7-code:cloud']))
    assert sidecar.read_text() == fp2               # stable when catalog stable


def test_ensure_grok_home_writes_nothing_under_dotgrok(fake_home):
    ensure_grok_home('ollama', _PROV)
    assert not (fake_home / '.grok').exists()  # referenced by path, never written


def test_ensure_grok_home_leaves_unreadable_config_alone(fake_home):
    """An existing config.toml that cannot be read (EACCES) is treated as
    foreign — never clobbered by the managed rewrite."""
    home = ensure_grok_home('ollama', _PROV)
    cfg = pathlib.Path(home, 'config.toml')
    original = cfg.read_bytes()
    cfg.chmod(0o000)
    try:
        again = ensure_grok_home('ollama', dict(_PROV, models=['changed']))
        assert again == str(home)
        # Even with a changed catalog the unreadable file is untouched.
        # (Skip the read-back assert when running as root, who bypasses 000.)
        if os.geteuid() != 0:
            cfg.chmod(0o600)
            assert cfg.read_bytes() == original
    finally:
        cfg.chmod(0o600)


def test_managed_config_survives_hostile_model_ids(fake_home):
    """Control characters / quotes in a model id are TOML-escaped, and the
    parsed table still carries the exact id."""
    import tomllib
    from models import _toml_str
    assert _toml_str('a\nb\tc\rd"e\\f\x01g') == (
        '"a\\nb\\tc\\rd\\"e\\\\f\\u0001g"')
    hostile = dict(_PROV, models=['evil\nmodel[1m]', 'a"b\\c'])
    home = ensure_grok_home('ollama', hostile)
    with open(os.path.join(home, 'config.toml'), 'rb') as f:
        doc = tomllib.load(f)
    assert 'evil\nmodel' in doc['model']           # [1m] stripped
    assert doc['model']['evil\nmodel'].get('context_window') == 1048576
    assert 'a"b\\c' in doc['model']
    assert doc['model']['a"b\\c']['env_key'] == 'XAI_API_KEY'
    assert 'context_window' not in doc['model']['a"b\\c']


# --- build_grok_spawn_env carries GROK_HOME -----------------------------------

def test_env_carries_grok_home(fake_home):
    s = Settings(providers={'ollama': _PROV}, provider_defaults={'grok': 'ollama'},
                 model_pins={'/p': 'kimi-k2.7-code:cloud'})
    env, reason = build_grok_spawn_env(s, '/p')
    assert reason is None
    assert env['GROK_HOME'] == str(grok_home_dir('ollama'))
    assert os.path.isfile(os.path.join(env['GROK_HOME'], 'config.toml'))
    # Native: no GROK_HOME and no managed home is created for the project.
    env2, _ = build_grok_spawn_env(Settings(), '/p')
    assert env2 is None
    native_home = grok_home_dir('never-ensured')
    env3, _ = build_grok_spawn_env(
        Settings(providers={'other': _PROV}, provider_overrides={'/p': ''}),
        '/p')
    assert env3 is None
    assert not native_home.exists()


# --- Round 7: disable_web_search / session_summary / subagent pin ----------

_TIER_PROV = {
    'name': 'Kimi',
    'base_url': 'https://api.kimi.com/coding/',
    'api_key': 'k',
    'models': ['kimi-for-coding', 'k3[1m]', 'kimi-for-coding-highspeed'],
}


def test_disable_web_search_in_preamble(fake_home):
    home = ensure_grok_home('ollama', _PROV)
    cfg = _read(home, 'config.toml')
    # Top-level key, before any [section].
    assert cfg.startswith('# projectman: managed grok home')
    assert 'disable_web_search = true\n\n[compat.claude]' in cfg
    import tomllib
    with open(os.path.join(home, 'config.toml'), 'rb') as f:
        doc = tomllib.load(f)
    assert doc['disable_web_search'] is True
    # Byte-stable second run.
    before = pathlib.Path(home, 'config.toml').read_bytes()
    ensure_grok_home('ollama', _PROV)
    assert pathlib.Path(home, 'config.toml').read_bytes() == before


def test_disable_web_search_survives_grok_rewrite(fake_home):
    home = ensure_grok_home('ollama', _PROV)
    cfg = pathlib.Path(home, 'config.toml')
    # Grok rewrites the file (drops our header) and adds [ui].
    body = cfg.read_text().split('[compat.claude]', 1)[1]
    cfg.write_text('[compat.claude]' + body + '\n[ui]\nmax_thoughts_width = 120\n')
    ensure_grok_home('ollama', _PROV)
    text = cfg.read_text()
    assert 'disable_web_search = true' in text
    assert '[ui]\nmax_thoughts_width = 120' in text


def test_session_summary_merged_into_foreign_models(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    # Grok owns [models] with its own default.
    cfg.write_text(cfg.read_text() + '\n[models]\ndefault = "k3"\n')
    ensure_grok_home('ollama', _TIER_PROV)
    text = cfg.read_text()
    # grok's key preserved verbatim; our key added; first catalog model used.
    assert '[models]\ndefault = "k3"\nsession_summary = "kimi-for-coding"' in text
    # Byte-stable from here on.
    before = cfg.read_bytes()
    ensure_grok_home('ollama', _TIER_PROV)
    assert cfg.read_bytes() == before


def test_session_summary_created_when_absent(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV)
    text = _read(home, 'config.toml')
    assert '[models]\nsession_summary = "kimi-for-coding"' in text


def test_session_summary_omitted_on_empty_model_list(fake_home):
    prov = dict(_TIER_PROV, models=[])
    home = ensure_grok_home('ollama', prov)
    assert 'session_summary =' not in _read(home, 'config.toml')
    # A foreign [models] the user wrote passes through untouched.
    cfg = pathlib.Path(home, 'config.toml')
    cfg.write_text(cfg.read_text() + '\n[models]\ndefault = "k3"\n')
    ensure_grok_home('ollama', prov)
    text = cfg.read_text()
    assert '[models]\ndefault = "k3"' in text
    assert 'session_summary =' not in text


def test_subagent_pin_emitted_when_tier_in_catalog(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV, subagent_tier='k3[1m]')
    text = _read(home, 'config.toml')
    assert '[subagents.models]\ngeneral-purpose = "k3"' in text
    import tomllib
    with open(os.path.join(home, 'config.toml'), 'rb') as f:
        doc = tomllib.load(f)
    assert doc['subagents']['models']['general-purpose'] == 'k3'


def test_subagent_pin_stale_or_empty_tier_no_section(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV, subagent_tier='gone')
    assert '\n[subagents' not in _read(home, 'config.toml')
    home2 = ensure_grok_home('other', _TIER_PROV, subagent_tier='')
    assert '\n[subagents' not in _read(home2, 'config.toml')


def test_subagent_foreign_sections_preserved(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    # User-authored [subagents] (bare) must survive; ours is only
    # [subagents.models].
    cfg.write_text(cfg.read_text() + '\n[subagents]\nsampling_limit = 4\n')
    ensure_grok_home('ollama', _TIER_PROV, subagent_tier='kimi-for-coding')
    text = cfg.read_text()
    assert '[subagents]\nsampling_limit = 4' in text
    assert '[subagents.models]\ngeneral-purpose = "kimi-for-coding"' in text


def test_tier_or_catalog_change_rewrites_exactly_once(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV, subagent_tier='kimi-for-coding')
    cfg = pathlib.Path(home, 'config.toml')
    v1 = cfg.read_bytes()
    ensure_grok_home('ollama', _TIER_PROV, subagent_tier='kimi-for-coding')
    assert cfg.read_bytes() == v1                      # no rewrite, same inputs
    # Tier change → exactly one refresh.
    ensure_grok_home('ollama', _TIER_PROV, subagent_tier='k3[1m]')
    v2 = cfg.read_bytes()
    assert v2 != v1
    assert 'general-purpose = "k3"' in v2.decode()
    ensure_grok_home('ollama', _TIER_PROV, subagent_tier='k3[1m]')
    assert cfg.read_bytes() == v2                      # stable again
    # Catalog change → refresh again.
    changed = dict(_TIER_PROV, models=['brand-new'])
    ensure_grok_home('ollama', changed, subagent_tier='k3[1m]')
    v3 = cfg.read_bytes()
    assert v3 != v2
    assert 'general-purpose' not in v3.decode()        # k3 no longer in catalog
    ensure_grok_home('ollama', changed, subagent_tier='k3[1m]')
    assert cfg.read_bytes() == v3


def test_build_grok_spawn_env_passes_subagent_tier(fake_home):
    from models import build_grok_spawn_env
    s = Settings(
        providers={'ollama': _TIER_PROV}, provider_defaults={'grok': 'ollama'},
        tier_models={'ollama': {'subagent': 'kimi-for-coding'}})
    env, _ = build_grok_spawn_env(s, '/p')
    text = _read(env['GROK_HOME'], 'config.toml')
    assert '[subagents.models]\ngeneral-purpose = "kimi-for-coding"' in text


# --- Round 8: [models] key-merge precision -----------------------------------


def test_session_summary_sibling_key_survives(fake_home):
    """Precise key match: session_summary_extra must not be eaten by the
    session_summary filter."""
    home = ensure_grok_home('ollama', _TIER_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    cfg.write_text(
        cfg.read_text()
        + '\n[models]\ndefault = "k3"\nsession_summary_extra = 42\n')
    ensure_grok_home('ollama', _TIER_PROV)
    text = cfg.read_text()
    assert 'session_summary_extra = 42' in text
    assert 'session_summary = "kimi-for-coding"' in text
    import tomllib
    with open(cfg, 'rb') as f:
        doc = tomllib.load(f)
    assert doc['models']['session_summary_extra'] == 42
    assert doc['models']['session_summary'] == 'kimi-for-coding'


def test_session_summary_multiline_remnant_swallowed(fake_home):
    """A stale multiline opener leaves no orphaned continuation lines."""
    home = ensure_grok_home('ollama', _TIER_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    cfg.write_text(
        cfg.read_text()
        + '\n[models]\ndefault = "k3"\n'
          'session_summary = """old\nmultiline value"""\n')
    ensure_grok_home('ollama', _TIER_PROV)
    text = cfg.read_text()
    assert 'old' not in text and 'multiline value' not in text
    assert 'session_summary = "kimi-for-coding"' in text
    assert '[models]\ndefault = "k3"\nsession_summary = ' in text
    import tomllib                      # must still be valid TOML
    with open(cfg, 'rb') as f:
        doc = tomllib.load(f)
    assert doc['models']['default'] == 'k3'
    assert doc['models']['session_summary'] == 'kimi-for-coding'


def test_stale_session_summary_stripped_on_empty_catalog(fake_home):
    """Catalog shrink: the PM key from an earlier catalog is removed even
    when there's nothing new to write; grok's own key is preserved."""
    home = ensure_grok_home('ollama', _TIER_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    cfg.write_text(cfg.read_text() + '\n[models]\ndefault = "k3"\n')
    ensure_grok_home('ollama', _TIER_PROV)
    assert 'session_summary = ' in cfg.read_text()
    ensure_grok_home('ollama', dict(_TIER_PROV, models=[]))
    text = cfg.read_text()
    assert 'session_summary =' not in text
    assert '[models]\ndefault = "k3"' in text
    # And it stays gone / byte-stable on subsequent runs.
    before = cfg.read_bytes()
    ensure_grok_home('ollama', dict(_TIER_PROV, models=[]))
    assert cfg.read_bytes() == before


# --- Round 9: [mcp_servers.pm-search] registration ---------------------------

_SEARCH_PROV = dict(_TIER_PROV,
                    search_url='https://api.kimi.com/coding/v1/search')


def test_search_mcp_registered_iff_search_url(fake_home):
    home = ensure_grok_home('ollama', _SEARCH_PROV)
    text = _read(home, 'config.toml')
    assert '[mcp_servers.pm-search]' in text
    assert '--provider' in text and '"ollama"' in text
    assert 'pm_search_mcp.py' in text
    import tomllib
    with open(os.path.join(home, 'config.toml'), 'rb') as f:
        doc = tomllib.load(f)
    block = doc['mcp_servers']['pm-search']
    assert block['command'] == 'python3'
    assert block['args'][-2] == '--provider'
    assert block['args'][-1] == 'ollama'
    # No search_url → no registration.
    home2 = ensure_grok_home('plain', _TIER_PROV)
    assert 'pm-search' not in _read(home2, 'config.toml')


def test_search_mcp_registration_stripped_when_removed(fake_home):
    home = ensure_grok_home('ollama', _SEARCH_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    assert '[mcp_servers.pm-search]' in cfg.read_text()
    ensure_grok_home('ollama', _TIER_PROV)          # search_url gone
    text = cfg.read_text()
    assert 'pm-search' not in text
    # Byte-stable after the strip.
    before = cfg.read_bytes()
    ensure_grok_home('ollama', _TIER_PROV)
    assert cfg.read_bytes() == before


def test_search_mcp_other_servers_preserved(fake_home):
    home = ensure_grok_home('ollama', _SEARCH_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    cfg.write_text(cfg.read_text() +
                   '\n[mcp_servers.github]\ncommand = "npx"\n')
    ensure_grok_home('ollama', _SEARCH_PROV)
    text = cfg.read_text()
    assert '[mcp_servers.github]\ncommand = "npx"' in text
    assert '[mcp_servers.pm-search]' in text
    import tomllib
    with open(cfg, 'rb') as f:
        tomllib.load(f)


def test_search_url_presence_rewrites_exactly_once(fake_home):
    home = ensure_grok_home('ollama', _TIER_PROV)
    cfg = pathlib.Path(home, 'config.toml')
    v1 = cfg.read_bytes()
    ensure_grok_home('ollama', _SEARCH_PROV)         # add search_url
    v2 = cfg.read_bytes()
    assert v2 != v1 and '[mcp_servers.pm-search]' in v2.decode()
    ensure_grok_home('ollama', _SEARCH_PROV)
    assert cfg.read_bytes() == v2                    # stable
    # The URL itself is catalog data read at call time — the registration
    # doesn't embed it, so editing the URL alone causes no rewrite.
    ensure_grok_home('ollama', dict(
        _SEARCH_PROV, search_url='https://api.kimi.com/v2/search'))
    assert cfg.read_bytes() == v2
    # Removing search_url → exactly one strip rewrite, then stable.
    ensure_grok_home('ollama', _TIER_PROV)
    v3 = cfg.read_bytes()
    assert v3 != v2 and 'pm-search' not in v3.decode()
    ensure_grok_home('ollama', _TIER_PROV)
    assert cfg.read_bytes() == v3


def test_search_url_validation_and_persistence(fake_home, tmp_path):
    from models import validate_providers
    import pytest
    validate_providers({'p': {'name': 'P', 'base_url': 'x', 'models': [],
                              'search_url': ''}})             # empty ok
    validate_providers({'p': {'name': 'P', 'base_url': 'x', 'models': [],
                              'search_url': 'https://e.com/s'}})   # https ok
    with pytest.raises(ValueError):
        validate_providers({'p': {'name': 'P', 'base_url': 'x',
                                  'models': [],
                                  'search_url': 'ftp://e.com'}})
    # Persistence round-trip through Settings save/load.
    from settings import Settings
    path = str(tmp_path / 'settings.json')
    s = Settings(providers={'p': {**_SEARCH_PROV}}, provider_defaults={})
    orig = s.save
    s.save = lambda p=None: orig(path)
    s.save()
    s2 = Settings.load(path)
    assert s2.providers['p']['search_url'] == 'https://api.kimi.com/coding/v1/search'
