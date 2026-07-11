"""M-UX.2 / M-UX.1 — read-only native agent model-config surfacing (C1/C2).

Pure parser tests over the recorded bench fixtures + the M-UX.1 truthful
"Default Model" resolver. No GTK. The Settings → Models sections and the
"Default Model" label are thin glue over these (settings_window).

Binding tests (spec items 1-2):
  * parse grok config.toml → default key + [model.*] entries with source path
  * parse opencode.json → default + provider models
  * defensive: missing / garbage file → 'none found', NEVER raises
  * default_model_label: harness_default=grok + parsed config → contains "grok"
    + the model name, NOT "Anthropic"
"""
import os

import harness_configs as ac
from settings import Settings


FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
GROK_FIX = os.path.join(FIXDIR, 'grok', 'config.toml')
OPENCODE_FIX = os.path.join(FIXDIR, 'opencode', 'opencode.json')


# ── grok parser ───────────────────────────────────────────────────────────────

def test_parse_grok_config_default_and_models():
    with open(GROK_FIX) as f:
        cfg = ac.parse_grok_config(f.read(), source_path=GROK_FIX)
    assert cfg.harness_id == 'grok'
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
    assert cfg.harness_id == 'opencode'
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


def test_load_harness_config_dispatch():
    assert ac.load_harness_config('claude') is None
    assert ac.load_harness_config('unknown-xyz') is None
    g = ac.load_harness_config('grok', home='/definitely/nowhere')
    assert g is not None and g.harness_id == 'grok'


# ── M-UX.1: truthful default-model label ──────────────────────────────────────

def _home_with_grok(tmp_path):
    (tmp_path / '.grok').mkdir()
    with open(GROK_FIX) as f:
        (tmp_path / '.grok' / 'config.toml').write_text(f.read())
    return str(tmp_path)


def test_default_model_label_grok_truthful(tmp_path):
    """BINDING (item 1): harness_default=grok + parsed config → label contains
    'grok' and the model name, NOT 'Anthropic'."""
    home = _home_with_grok(tmp_path)
    s = Settings(harness_default='grok')
    label = ac.default_model_label(s, home=home)
    assert 'Anthropic' not in label
    assert 'Grok Build' in label
    assert 'Qwen3.5 9B (Ollama pool)' in label
    assert '~/.grok/config.toml' in label


def test_default_model_label_claude_is_native(tmp_path):
    s = Settings(harness_default='claude')
    label = ac.default_model_label(s, home=str(tmp_path), native_label='NATIVE')
    assert label == 'NATIVE'


def test_default_model_label_grok_missing_config_still_names_agent(tmp_path):
    """No config on disk: drop the model suffix but still attribute the harness +
    path (never falls back to a Claude/Anthropic story for a grok default)."""
    s = Settings(harness_default='grok')
    label = ac.default_model_label(s, home=str(tmp_path), native_label='NATIVE')
    assert 'Anthropic' not in label
    assert label != 'NATIVE'
    assert 'Grok Build' in label


def test_default_model_label_opencode_truthful(tmp_path):
    home = tmp_path
    (home / '.config' / 'opencode').mkdir(parents=True)
    with open(OPENCODE_FIX) as f:
        (home / '.config' / 'opencode' / 'opencode.json').write_text(f.read())
    s = Settings(harness_default='opencode')
    label = ac.default_model_label(s, home=str(home))
    assert 'Anthropic' not in label
    assert 'OpenCode' in label or 'opencode' in label
    assert 'qwen3.5:cloud' in label


# ── P3.5f (C2, the maintainer's second reveal): PER-ROW default label ──────────────────
# default_model_label_for resolves the project's EFFECTIVE agent, so a row's
# "Default (…)" label tells ITS harness's story — not the global default's.

def test_default_model_label_for_claude_override_on_grok_bench(tmp_path):
    """T1 (BINDING, the maintainer's repro): on a grok-default bench, a project that
    OVERRIDES its harness to claude gets claude's native label — NOT the global
    grok default's 'Grok Build' story (the C2 bug)."""
    home = _home_with_grok(tmp_path)
    s = Settings(
        harness_default='grok',
        harness_overrides={'/proj/claude': 'claude'},
    )
    label = ac.default_model_label_for(
        s, '/proj/claude', home=home, native_label='NATIVE')
    assert label == 'NATIVE'
    assert 'Grok Build' not in label


def test_default_model_label_for_grok_project_on_grok_bench(tmp_path):
    """T2 (pin today's behavior): a grok project on a grok bench still gets the
    grok label — the per-row resolver did not regress the grok story."""
    home = _home_with_grok(tmp_path)
    s = Settings(
        harness_default='grok',
        harness_overrides={'/proj/grok': 'grok'},
    )
    label = ac.default_model_label_for(s, '/proj/grok', home=home)
    assert 'Anthropic' not in label
    assert 'Grok Build' in label
    assert 'Qwen3.5 9B (Ollama pool)' in label


def test_default_model_label_for_follow_default_matches_global(tmp_path):
    """T3 (unchanged): a follow-default project (no override) gets the GLOBAL
    default harness's label — byte-identical to default_model_label."""
    home = _home_with_grok(tmp_path)
    s = Settings(harness_default='grok')  # /proj/follow has no override
    per_row = ac.default_model_label_for(s, '/proj/follow', home=home)
    global_ = ac.default_model_label(s, home=home)
    assert per_row == global_
    assert 'Grok Build' in per_row


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
# B2 / M-UX.13 — per-harness account status (PRESENCE-based; contents never read)
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






def _home_with_grok_text(tmp_path, text):
    (tmp_path / '.grok').mkdir()
    (tmp_path / '.grok' / 'config.toml').write_text(text)
    return str(tmp_path)


def test_default_model_label_no_default_key_says_built_in(tmp_path):
    """BINDING (FB-1b): grok config with model blocks but NO [models] default →
    the label says 'built-in', and does NOT promote the sole block to default.
    Reverting to the old `return base` (no 'built-in') FAILS this."""
    # A SOLE model block, no [models] default — the exact F11 trap shape.
    text = ('[model.pool-qwen]\nmodel = "qwen3.5:9b"\n'
            'name = "Qwen3.5 9B (Ollama pool)"\n')
    home = _home_with_grok_text(tmp_path, text)
    s = Settings(harness_default='grok')
    label = ac.default_model_label(s, home=home, native_label='NATIVE')
    assert 'built-in' in label
    assert 'Grok Build' in label
    # The sole block must NOT be inferred as the active/default model.
    assert 'Qwen3.5 9B (Ollama pool)' not in label
    assert 'Anthropic' not in label


def test_default_model_label_explicit_default_names_it(tmp_path):
    """BINDING (FB-1b): an explicit [models] default → the label NAMES the model
    (the no-default 'built-in' string must NOT appear)."""
    text = ('[models]\ndefault = "pool-qwen"\n[model.pool-qwen]\n'
            'model = "qwen3.5:9b"\nname = "Qwen3.5 9B (Ollama pool)"\n')
    home = _home_with_grok_text(tmp_path, text)
    s = Settings(harness_default='grok')
    label = ac.default_model_label(s, home=home, native_label='NATIVE')
    assert 'Qwen3.5 9B (Ollama pool)' in label
    assert 'built-in' not in label


def test_default_model_label_default_key_no_block_shows_bare_key(tmp_path):
    """A [models] default naming a built-in id with no [model.*] block → the
    bare key (NOT the 'built-in default' no-key string — a default WAS declared)."""
    text = '[models]\ndefault = "grok-build"\n'
    home = _home_with_grok_text(tmp_path, text)
    s = Settings(harness_default='grok')
    label = ac.default_model_label(s, home=home, native_label='NATIVE')
    assert 'grok-build' in label
    assert 'built-in default' not in label


# ════════════════════════════════════════════════════════════════════════════
# P3.5e FB-1a / FB-1c — native_model_options: the per-project Model picker lists
# the effective harness's NATIVE models; the config-declared default is marked.
# ════════════════════════════════════════════════════════════════════════════

def test_native_model_options_claude_is_none():
    """claude has no native model-config surface → None (picker stays ccr)."""
    assert ac.native_model_options('claude', home='/nowhere') is None
    assert ac.native_model_options('unknown-xyz', home='/nowhere') is None


def test_native_model_options_grok_lists_keys_with_default_marker(tmp_path):
    """BINDING (FB-1a/1c): grok options carry the [model.*] KEYS as ids (the -m
    value), and the [models] default key gets the '• default' marker."""
    home = _home_with_grok(tmp_path)   # bench fixture: default = pool-qwen
    ids, labels = ac.native_model_options('grok', home=home)
    # The KEY (the -m value) is what's stored, not a display name.
    assert 'pool-qwen' in ids
    assert 'grok-4' in ids
    # FB-1c: exactly the declared default is marked.
    marked = [lbl for lbl in labels if ac.DEFAULT_MARKER in lbl]
    assert len(marked) == 1
    assert 'pool-qwen' in marked[0]
    # The non-default model is NOT marked.
    grok4_label = labels[ids.index('grok-4')]
    assert ac.DEFAULT_MARKER not in grok4_label


def test_native_model_options_grok_no_default_no_marker(tmp_path):
    """BINDING (FB-1c): no [models] default → NO entry is marked (no false
    'active' claim — the cross-corrected truth)."""
    text = ('[model.pool-qwen]\nmodel = "qwen3.5:9b"\nname = "Qwen"\n'
            '[model.grok-4]\nmodel = "grok-4-latest"\nname = "Grok 4"\n')
    home = _home_with_grok_text(tmp_path, text)
    ids, labels = ac.native_model_options('grok', home=home)
    assert set(ids) == {'pool-qwen', 'grok-4'}
    assert not any(ac.DEFAULT_MARKER in lbl for lbl in labels)


def test_native_model_options_opencode_lists_provider_model_ids(tmp_path):
    """BINDING (FB-1a): opencode options carry 'provider/model' ids (the -m
    value) and mark the declared default."""
    home = tmp_path
    (home / '.config' / 'opencode').mkdir(parents=True)
    with open(OPENCODE_FIX) as f:
        (home / '.config' / 'opencode' / 'opencode.json').write_text(f.read())
    ids, labels = ac.native_model_options('opencode', home=str(home))
    assert 'ollama/qwen3.5:cloud' in ids   # the default, marked
    marked = [lbl for lbl in labels if ac.DEFAULT_MARKER in lbl]
    assert len(marked) == 1
    assert 'qwen3.5:cloud' in marked[0]


def test_native_model_options_grok_absent_config_empty_lists(tmp_path):
    """A native agent with no config on disk → ([], []) (the row still shows the
    Default entry), never None and never a raise."""
    result = ac.native_model_options('grok', home=str(tmp_path))
    assert result == ([], [])
