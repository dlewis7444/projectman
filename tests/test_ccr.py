import os
import json

import ccr
from settings import Settings


def _settings(**kw):
    base = dict(
        providers={
            'ollama': {
                'name': 'Ollama',
                'base_url': 'http://host:11434/v1',
                'api_key': 'k',
                'models': {'qwen': {'name': 'Qwen'}},
            },
        },
        model_default='ollama/qwen',
        ccr_api_key='secret',
    )
    base.update(kw)
    return Settings(**base)


def _redirect_config(monkeypatch, tmp_path):
    cfg_dir = tmp_path / '.ccr'
    monkeypatch.setattr(ccr, 'CCR_CONFIG_DIR', str(cfg_dir))
    monkeypatch.setattr(ccr, 'CCR_CONFIG_PATH', str(cfg_dir / 'config.json'))
    return cfg_dir


def test_render_config_shape():
    cfg = ccr.render_config(_settings())
    assert cfg['HOST'] == '127.0.0.1'
    assert cfg['PORT'] == 3456
    assert cfg['APIKEY'] == 'secret'
    assert cfg['Providers'] == [{
        'name': 'ollama',
        'api_base_url': 'http://host:11434/v1',
        'api_key': 'k',
        'models': ['qwen'],
    }]
    assert cfg['Router']['default'] == 'ollama,qwen'
    assert cfg['Router']['longContext'] == 'ollama,qwen'


def test_render_config_includes_transformer():
    s = _settings()
    s.providers['ollama']['transformer'] = 'openrouter'
    cfg = ccr.render_config(s)
    assert cfg['Providers'][0]['transformer'] == {'use': ['openrouter']}


def test_router_target_prefers_global_default():
    assert ccr._router_target(_settings()) == 'ollama,qwen'


def test_router_target_falls_back_to_override():
    s = _settings(model_default='', model_overrides={'/p/a': 'ollama/qwen'})
    assert ccr._router_target(s) == 'ollama,qwen'


def test_router_target_falls_back_to_first_provider():
    s = _settings(model_default='', model_overrides={})
    assert ccr._router_target(s) == 'ollama,qwen'


def test_ensure_api_key_mints_when_blank():
    s = _settings(ccr_api_key='')
    key = ccr.ensure_api_key(s)
    assert key and s.ccr_api_key == key
    # idempotent — a second call keeps the same key
    assert ccr.ensure_api_key(s) == key


def test_write_config_atomic_and_hardened(monkeypatch, tmp_path):
    cfg_dir = _redirect_config(monkeypatch, tmp_path)
    assert ccr.write_config(_settings()) is True
    cfg_path = cfg_dir / 'config.json'
    assert cfg_path.exists()
    assert (os.stat(cfg_path).st_mode & 0o777) == 0o600
    assert (os.stat(cfg_dir).st_mode & 0o777) == 0o700
    # no temp files left behind
    assert {p.name for p in cfg_dir.iterdir()} == {'config.json'}
    assert json.loads(cfg_path.read_text())['APIKEY'] == 'secret'


def test_config_differs(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    s = _settings()
    # no file yet
    assert ccr.config_differs(s) is True
    ccr.write_config(s)
    assert ccr.config_differs(s) is False
    # a changed selection is detected
    s.model_default = 'ollama/other'
    s.providers['ollama']['models']['other'] = {'name': 'Other'}
    assert ccr.config_differs(s) is True


def test_available_false_for_bogus_binary():
    assert ccr.available(Settings(ccr_binary='/no/such/ccr-binary-xyz')) is False
