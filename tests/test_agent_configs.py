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
