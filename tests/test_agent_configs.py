"""M-UX.2 / M-UX.1 — read-only native agent model-config surfacing (C1/C2).

Pure parser tests over the recorded bench fixtures + the M-UX.1 truthful
"Default Model" resolver. No GTK. The Settings → Models sections and the
"Default Model" label are thin glue over these (settings_window).

Binding tests (spec items 1-2):
  * parse grok config.toml → default key + [model.*] entries with source path
  * parse opencode.json → default + provider models
  * defensive: missing / garbage file → 'none found', NEVER raises
  * default_model_label: agent_default=grok + parsed config → contains "grok"
    + the model name, NOT "Anthropic"
"""
import os

import agent_configs as ac
from settings import Settings


FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
GROK_FIX = os.path.join(FIXDIR, 'grok', 'config.toml')
OPENCODE_FIX = os.path.join(FIXDIR, 'opencode', 'opencode.json')


# ── grok parser ───────────────────────────────────────────────────────────────

def test_parse_grok_config_default_and_models():
    with open(GROK_FIX) as f:
        cfg = ac.parse_grok_config(f.read(), source_path=GROK_FIX)
    assert cfg.agent_id == 'grok'
    assert cfg.exists is True
    assert cfg.default_key == 'pool-qwen'
    keys = {m.key for m in cfg.models}
    assert {'pool-qwen', 'grok-4'} <= keys
    entry = cfg.default_entry()
    assert entry is not None
    assert entry.name == 'Qwen3.5 9B (Ollama pool)'
    assert entry.model == 'qwen3.5:9b'
    assert entry.base_url == 'http://localhost:11434/v1'


def test_parse_grok_config_missing_is_empty_not_raise():
    cfg = ac.parse_grok_config('', source_path='/nope/config.toml')
    assert cfg.exists is False
    assert cfg.models == []
    assert cfg.default_key == ''


def test_parse_grok_config_garbage_does_not_raise():
    cfg = ac.parse_grok_config('this is not = valid ][ toml @@@', source_path='/x')
    # garbage but non-empty → file 'existed', no entries, no raise
    assert cfg.exists is True
    assert cfg.models == []
    assert cfg.default_entry() is None


def test_parse_grok_config_default_without_block_shows_bare_key():
    text = '[models]\ndefault = "grok-4-builtin"\n'
    cfg = ac.parse_grok_config(text, source_path='/x')
    assert cfg.default_key == 'grok-4-builtin'
    assert cfg.default_entry() is None  # no [model.*] block for it


def test_load_grok_config_from_home(tmp_path):
    home = tmp_path
    (home / '.grok').mkdir()
    with open(GROK_FIX) as f:
        (home / '.grok' / 'config.toml').write_text(f.read())
    cfg = ac.load_grok_config(home=str(home))
    assert cfg.exists is True
    assert cfg.default_key == 'pool-qwen'
    assert cfg.source_path == str(home / '.grok' / 'config.toml')


def test_load_grok_config_absent_home(tmp_path):
    cfg = ac.load_grok_config(home=str(tmp_path))
    assert cfg.exists is False
    assert cfg.models == []


# ── opencode parser ───────────────────────────────────────────────────────────

def test_parse_opencode_config_default_and_models():
    with open(OPENCODE_FIX) as f:
        cfg = ac.parse_opencode_config(f.read(), source_path=OPENCODE_FIX)
    assert cfg.agent_id == 'opencode'
    assert cfg.exists is True
    assert cfg.default_key == 'ollama/qwen3.5:cloud'
    keys = {m.key for m in cfg.models}
    assert {
        'ollama/qwen3.5:cloud', 'ollama/glm-5.1:cloud',
        'ollama/kimi-k2.6:cloud', 'ollama/nemotron-3-super:cloud',
    } == keys
    entry = cfg.default_entry()
    assert entry is not None
    assert entry.base_url == 'http://localhost:11434/v1'


def test_parse_opencode_config_garbage_does_not_raise():
    cfg = ac.parse_opencode_config('{not json', source_path='/x')
    assert cfg.exists is True
    assert cfg.models == []


def test_parse_opencode_config_empty_is_absent():
    cfg = ac.parse_opencode_config('', source_path='/x')
    assert cfg.exists is False


def test_load_opencode_config_from_home(tmp_path):
    home = tmp_path
    (home / '.config' / 'opencode').mkdir(parents=True)
    with open(OPENCODE_FIX) as f:
        (home / '.config' / 'opencode' / 'opencode.json').write_text(f.read())
    cfg = ac.load_opencode_config(home=str(home))
    assert cfg.exists is True
    assert cfg.default_key == 'ollama/qwen3.5:cloud'


def test_load_opencode_config_absent_home(tmp_path):
    cfg = ac.load_opencode_config(home=str(tmp_path))
    assert cfg.exists is False


def test_load_agent_config_dispatch():
    assert ac.load_agent_config('claude') is None
    assert ac.load_agent_config('unknown-xyz') is None
    g = ac.load_agent_config('grok', home='/definitely/nowhere')
    assert g is not None and g.agent_id == 'grok'


# ── M-UX.1: truthful default-model label ──────────────────────────────────────

def _home_with_grok(tmp_path):
    (tmp_path / '.grok').mkdir()
    with open(GROK_FIX) as f:
        (tmp_path / '.grok' / 'config.toml').write_text(f.read())
    return str(tmp_path)


def test_default_model_label_grok_truthful(tmp_path):
    """BINDING (item 1): agent_default=grok + parsed config → label contains
    'grok' and the model name, NOT 'Anthropic'."""
    home = _home_with_grok(tmp_path)
    s = Settings(agent_default='grok')
    label = ac.default_model_label(s, home=home)
    assert 'Anthropic' not in label
    assert 'Grok Build' in label
    assert 'Qwen3.5 9B (Ollama pool)' in label
    assert '~/.grok/config.toml' in label


def test_default_model_label_claude_is_native(tmp_path):
    s = Settings(agent_default='claude')
    label = ac.default_model_label(s, home=str(tmp_path), native_label='NATIVE')
    assert label == 'NATIVE'


def test_default_model_label_grok_missing_config_still_names_agent(tmp_path):
    """No config on disk: drop the model suffix but still attribute the agent +
    path (never falls back to a Claude/Anthropic story for a grok default)."""
    s = Settings(agent_default='grok')
    label = ac.default_model_label(s, home=str(tmp_path), native_label='NATIVE')
    assert 'Anthropic' not in label
    assert label != 'NATIVE'
    assert 'Grok Build' in label


def test_default_model_label_opencode_truthful(tmp_path):
    home = tmp_path
    (home / '.config' / 'opencode').mkdir(parents=True)
    with open(OPENCODE_FIX) as f:
        (home / '.config' / 'opencode' / 'opencode.json').write_text(f.read())
    s = Settings(agent_default='opencode')
    label = ac.default_model_label(s, home=str(home))
    assert 'Anthropic' not in label
    assert 'opencode' in label
    assert 'qwen3.5:cloud' in label


# ════════════════════════════════════════════════════════════════════════════
# B1 / M-UX.8-residual — grok [compat.claude] hooks: three states → three strings
# ════════════════════════════════════════════════════════════════════════════

def test_grok_compat_state_disabled():
    """BINDING (B1): hooks = false → the 'disabled' state."""
    text = '[compat.claude]\nhooks = false\n'
    assert ac.grok_compat_hooks_state(text) == ac.COMPAT_HOOKS_DISABLED


def test_grok_compat_state_enabled():
    """BINDING (B1): hooks = true → the 'enabled' (risk) state."""
    text = '[compat.claude]\nhooks = true\n'
    assert ac.grok_compat_hooks_state(text) == ac.COMPAT_HOOKS_ENABLED


def test_grok_compat_state_absent_when_key_missing():
    """BINDING (B1): no [compat.claude] key → 'absent' (grok's default = on)."""
    text = '[models]\ndefault = "pool-qwen"\n'
    assert ac.grok_compat_hooks_state(text) == ac.COMPAT_HOOKS_ABSENT


def test_grok_compat_state_absent_on_empty_and_garbage():
    assert ac.grok_compat_hooks_state('') == ac.COMPAT_HOOKS_ABSENT
    assert ac.grok_compat_hooks_state('this ][ is not = toml @@@') == ac.COMPAT_HOOKS_ABSENT


def test_grok_compat_state_nonbool_value_is_absent():
    """A typo'd non-bool value never earns the safe 'disabled' claim."""
    text = '[compat.claude]\nhooks = "false"\n'   # string, not bool
    assert ac.grok_compat_hooks_state(text) == ac.COMPAT_HOOKS_ABSENT


def test_grok_compat_line_disabled_from_home(tmp_path):
    """BINDING (B1): the three states drive three line strings (tmp-home)."""
    (tmp_path / '.grok').mkdir()
    (tmp_path / '.grok' / 'config.toml').write_text('[compat.claude]\nhooks = false\n')
    line = ac.grok_compat_hooks_line(home=str(tmp_path))
    assert line == 'disabled ✓ (status dots fire once)'


def test_grok_compat_line_enabled_from_home(tmp_path):
    (tmp_path / '.grok').mkdir()
    (tmp_path / '.grok' / 'config.toml').write_text('[compat.claude]\nhooks = true\n')
    line = ac.grok_compat_hooks_line(home=str(tmp_path))
    assert line.startswith('⚠ enabled')
    assert 'double-fire' in line
    assert 'Install/Update bridge' in line


def test_grok_compat_line_absent_from_home_reads_as_enabled(tmp_path):
    """No grok config at all → the absent state reads the SAME warning string as
    enabled (grok's default is on)."""
    line = ac.grok_compat_hooks_line(home=str(tmp_path))
    assert line.startswith('⚠ enabled')


def test_grok_compat_line_disabled_from_bench_fixture(tmp_path):
    """The recorded bench fixture has hooks = false → the safe line."""
    (tmp_path / '.grok').mkdir()
    with open(GROK_FIX) as f:
        (tmp_path / '.grok' / 'config.toml').write_text(f.read())
    assert ac.grok_compat_hooks_line(home=str(tmp_path)) == 'disabled ✓ (status dots fire once)'


# ════════════════════════════════════════════════════════════════════════════
# B2 / M-UX.13 — per-agent account status (PRESENCE-based; contents never read)
# ════════════════════════════════════════════════════════════════════════════

def test_claude_account_signed_in_when_credentials_nonempty(tmp_path):
    """BINDING (B2 claude): non-empty ~/.claude/.credentials.json → signed in.
    The file holds only a placeholder byte — no realistic-looking secret."""
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.claude' / '.credentials.json').write_text('x')
    assert ac.claude_account_line(home=str(tmp_path)) == 'Signed in (credentials present)'


def test_claude_account_not_signed_in_when_absent(tmp_path):
    line = ac.claude_account_line(home=str(tmp_path))
    assert line.startswith('Not signed in')
    assert 'claude' in line


def test_claude_account_not_signed_in_when_empty_file(tmp_path):
    """An empty credentials file is NOT signed in (size == 0)."""
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.claude' / '.credentials.json').write_text('')
    assert ac.claude_account_line(home=str(tmp_path)).startswith('Not signed in')


def test_grok_account_signed_in_when_auth_token_present(tmp_path):
    """BINDING (B2 grok): non-empty ~/.grok/auth.json → signed in (token present).
    Placeholder byte only — never a real-looking token."""
    (tmp_path / '.grok').mkdir()
    (tmp_path / '.grok' / 'auth.json').write_text('x')
    assert ac.grok_account_line(home=str(tmp_path)) == 'Signed in (token present)'


def test_grok_account_api_key_configured_when_config_has_key(tmp_path):
    """BINDING (B2 grok): no auth.json but a [model.*] api_key → API key
    configured with the config path (the offline-pool recipe)."""
    (tmp_path / '.grok').mkdir()
    with open(GROK_FIX) as f:
        (tmp_path / '.grok' / 'config.toml').write_text(f.read())  # has api_key
    line = ac.grok_account_line(home=str(tmp_path))
    assert line.startswith('API key configured')
    assert '~/.grok/config.toml' in line


def test_grok_account_not_signed_in_when_nothing(tmp_path):
    """No auth.json, no api_key in config → not signed in, points at grok login."""
    (tmp_path / '.grok').mkdir()
    (tmp_path / '.grok' / 'config.toml').write_text(
        '[models]\ndefault = "grok-4"\n[model.grok-4]\nmodel = "grok-4-latest"\n')
    line = ac.grok_account_line(home=str(tmp_path))
    assert line == 'Not signed in — `grok login`'


def test_grok_account_auth_wins_over_api_key(tmp_path):
    """auth.json presence takes precedence over the api_key fallback."""
    (tmp_path / '.grok').mkdir()
    (tmp_path / '.grok' / 'auth.json').write_text('x')
    with open(GROK_FIX) as f:
        (tmp_path / '.grok' / 'config.toml').write_text(f.read())
    assert ac.grok_account_line(home=str(tmp_path)) == 'Signed in (token present)'


def test_opencode_account_providers_configured(tmp_path):
    """BINDING (B2 opencode): the HONEST provable line — providers found in the
    parsed config (no invented auth path)."""
    (tmp_path / '.config' / 'opencode').mkdir(parents=True)
    with open(OPENCODE_FIX) as f:
        (tmp_path / '.config' / 'opencode' / 'opencode.json').write_text(f.read())
    line = ac.opencode_account_line(home=str(tmp_path))
    assert line.startswith('Providers configured: 1')   # one provider (ollama)
    assert '~/.config/opencode/opencode.json' in line


def test_opencode_account_no_providers_when_absent(tmp_path):
    assert ac.opencode_account_line(home=str(tmp_path)) == 'No providers found'


def test_account_status_dispatch(tmp_path):
    assert ac.account_status_line('claude', home=str(tmp_path)).startswith('Not signed in')
    assert ac.account_status_line('grok', home=str(tmp_path)).startswith('Not signed in')
    assert ac.account_status_line('opencode', home=str(tmp_path)) == 'No providers found'
    assert ac.account_status_line('unknown-xyz', home=str(tmp_path)) is None


def test_account_lines_never_read_contents(tmp_path, monkeypatch):
    """Discipline guard: the presence checks must never OPEN the token files.
    We poison ``open`` for the credential paths and require no raise + the
    not-signed-in answer (existence/size via os.stat only)."""
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.claude' / '.credentials.json').write_text('x')
    real_open = open

    def guard(path, *a, **k):
        p = os.fspath(path)
        if p.endswith('.credentials.json') or p.endswith('auth.json'):
            raise AssertionError(f'must not open credential file: {p}')
        return real_open(path, *a, **k)

    import builtins
    monkeypatch.setattr(builtins, 'open', guard)
    # Still reports signed in — proves existence/size path, no content read.
    assert ac.claude_account_line(home=str(tmp_path)) == 'Signed in (credentials present)'


# ════════════════════════════════════════════════════════════════════════════
# B3 / M-UX.14 — ccr "in use" decision (pure)
# ════════════════════════════════════════════════════════════════════════════

def test_ccr_in_use_false_empty_settings():
    """BINDING (B3): providers == {} and no custom overrides → NOT in use
    (block collapses to one row)."""
    assert ac.ccr_in_use(Settings()) is False


def test_ccr_in_use_true_with_providers():
    s = Settings(providers={'openrouter': {'models': {'x': {}}}})
    assert ac.ccr_in_use(s) is True


def test_ccr_in_use_true_with_custom_default_even_without_provider():
    """A custom 'provider/model' default still means ccr was meant to be used."""
    s = Settings(model_default='openrouter/some-model')
    assert ac.ccr_in_use(s) is True


def test_ccr_in_use_true_with_custom_override():
    s = Settings(model_overrides={'/p': 'openrouter/some-model'})
    assert ac.ccr_in_use(s) is True


def test_ccr_in_use_false_with_native_default_and_overrides():
    """Native default ('') + native overrides ('') → still not in use."""
    s = Settings(model_default='', model_overrides={'/p': ''})
    assert ac.ccr_in_use(s) is False
