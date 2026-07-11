import os
import json
import pytest
from settings import Settings, DEFAULT_SETTINGS_PATH


def test_defaults():
    s = Settings()
    assert s.projects_dir == '~/.ProjectMan/projects'
    assert s.claude_binary == ''
    assert s.resume_projects is True
    assert 'resume_last_project' not in Settings.__dataclass_fields__
    assert s.font_size == 11
    assert s.scrollback_lines == 10000
    assert s.audible_bell is False
    assert s.multiplexer == 'none'


def test_resolved_projects_dir():
    s = Settings()
    assert s.resolved_projects_dir == os.path.expanduser('~/.ProjectMan/projects')


def test_resolved_claude_binary_empty():
    s = Settings()
    assert s.resolved_claude_binary == 'claude'


def test_resolved_claude_binary_whitespace():
    s = Settings(claude_binary='   ')
    assert s.resolved_claude_binary == 'claude'


def test_resolved_claude_binary_set():
    s = Settings(claude_binary='/usr/local/bin/claude')
    assert s.resolved_claude_binary == '/usr/local/bin/claude'


def test_load_missing_file(tmp_path):
    path = str(tmp_path / 'nonexistent.json')
    s = Settings.load(path)
    assert s.font_size == 11  # defaults returned


def test_load_missing_file_persists_defaults(tmp_path):
    """BINDING (FB-7 / power #2): a genuine first run (no settings.json) WRITES
    the defaults so the file exists from launch one. Reverting the first-run
    write (return cls() without save) leaves the file absent and FAILS this."""
    import json
    path = tmp_path / 'settings.json'
    assert not path.exists()
    s = Settings.load(str(path))
    assert path.exists()                 # the file was created
    data = json.loads(path.read_text())
    # The persisted content is the full defaults (round-trips to the same obj).
    assert data['font_size'] == 11
    assert data['model_default'] == ''
    assert Settings.load(str(path)).font_size == s.font_size


def test_load_corrupt_json_does_not_overwrite(tmp_path):
    """A corrupt file is NEVER overwritten on load (recovery preserved) — only a
    GENUINELY ABSENT file triggers the first-run write."""
    path = tmp_path / 'settings.json'
    path.write_text('not json!')
    s = Settings.load(str(path))
    assert s.font_size == 11             # in-memory defaults on parse error
    assert path.read_text() == 'not json!'   # on-disk file left untouched


def test_load_corrupt_json(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('not json!')
    s = Settings.load(str(path))
    assert s.font_size == 11  # defaults on parse error


def test_load_partial_file(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 14}))
    s = Settings.load(str(path))
    assert s.font_size == 14
    assert s.scrollback_lines == 10000  # default for missing field


def test_load_ignores_unknown_keys(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 14, 'unknown_key': 'value'}))
    s = Settings.load(str(path))
    assert s.font_size == 14  # must not raise TypeError


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(font_size=14, multiplexer='tmux')
    s.save(path)
    s2 = Settings.load(path)
    assert s2.font_size == 14
    assert s2.multiplexer == 'tmux'


def test_save_atomic_no_temp_files(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings()
    s.save(path)
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == 'settings.json'


def test_save_creates_directory(tmp_path):
    path = str(tmp_path / 'subdir' / 'settings.json')
    s = Settings()
    s.save(path)
    assert os.path.exists(path)


def test_default_settings_path_constant():
    assert DEFAULT_SETTINGS_PATH == os.path.expanduser('~/.ProjectMan/settings.json')


def test_load_ignores_old_resume_last_project_key(tmp_path):
    """Old settings.json with resume_last_project is silently upgraded."""
    path = tmp_path / 'settings.json'
    path.write_text('{"resume_last_project": false}')
    s = Settings.load(str(path))
    # Old key is ignored; new field uses dataclass default (True)
    assert s.resume_projects is True


def test_paa_defaults():
    s = Settings()
    assert s.paa_enabled is False
    assert s.paa_loop_interval_minutes == 30
    assert s.paa_budget_tokens == 100000
    assert s.paa_budget_used == 0
    assert s.paa_budget_unlimited is False
    assert s.paa_allow_haiku is True
    assert s.paa_autonomy_level == 'suggest'


def test_paa_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(paa_enabled=True, paa_loop_interval_minutes=15)
    s.save(path)
    s2 = Settings.load(path)
    assert s2.paa_enabled is True
    assert s2.paa_loop_interval_minutes == 15


def test_paa_budget_month_default():
    s = Settings()
    assert s.paa_budget_month == ''


def test_paa_budget_month_roundtrip(tmp_path):
    p = str(tmp_path / 'settings.json')
    s = Settings(paa_budget_month='2026-03')
    s.save(p)
    s2 = Settings.load(p)
    assert s2.paa_budget_month == '2026-03'


# --- Model layer (Claude-Only + provider axis) ----------------------------- #

def _sample_providers():
    return {
        'ollama': {
            'name': 'Ollama',
            'base_url': 'http://host:11434/v1',
            'api_key': 'x',
            'models': ['qwen'],
        },
    }


def test_provider_defaults():
    s = Settings()
    assert s.providers == {}
    assert s.model_default == ''
    assert s.provider_overrides == {}
    assert s.model_pins == {}
    assert s.tier_models == {}
    # ccr/harness_default/harness_overrides are no longer fields
    assert not hasattr(s, 'ccr_managed')
    assert hasattr(s, 'harness_default')
    assert hasattr(s, 'harness_overrides')


def test_providers_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(providers=_sample_providers(), model_default='ollama',
                 tier_models={'opus': 'qwen'})
    s.save(path)
    s2 = Settings.load(path)
    assert s2.providers == _sample_providers()
    assert s2.model_default == 'ollama'
    assert s2.tier_models['ollama']['opus'] == 'qwen'


def test_load_old_file_without_provider_keys(tmp_path):
    """An old settings.json predating the model fields loads with defaults."""
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 12}))
    s = Settings.load(str(path))
    assert s.providers == {}
    assert s.model_default == ''


def test_effective_provider_global_default():
    s = Settings(providers=_sample_providers(), model_default='ollama')
    assert s.effective_provider('/p/a') == 'ollama'


def test_effective_provider_per_project_override():
    s = Settings(providers=_sample_providers(), model_default='ollama',
                 provider_overrides={'/p/a': ''})
    assert s.effective_provider('/p/a') == ''          # pinned to native
    assert s.effective_provider('/p/b') == 'ollama'    # follows default


def test_effective_harness_defaults_to_claude():
    assert Settings().effective_harness('/p') == 'claude'
    assert Settings(providers=_sample_providers(),
                    model_default='ollama').effective_harness('/p') == 'claude'


def test_effective_model_reads_model_pins_only():
    s = Settings(providers=_sample_providers(), model_default='ollama',
                 provider_overrides={'/p': 'ollama'},
                 model_pins={'/p': 'ollama/qwen'})
    assert s.effective_provider('/p') == 'ollama'
    assert s.effective_model('/p') == 'ollama/qwen'
    # Provider pin alone does not invent a model pin.
    s2 = Settings(providers=_sample_providers(),
                  provider_overrides={'/p': 'ollama'})
    assert s2.effective_model('/p') == ''


def test_stale_provider_override_falls_back_to_default():
    s = Settings(providers=_sample_providers(), model_default='ollama',
                 provider_overrides={'/p': 'not-a-provider'})
    assert s.effective_provider('/p') == 'ollama'


def test_uses_custom_provider():
    s = Settings(providers=_sample_providers(), model_default='ollama')
    assert s.uses_custom_provider('/p/a') is True
    # native default
    assert Settings().uses_custom_provider('/p/a') is False
    # provider id not defined
    s2 = Settings(model_default='ghost')
    assert s2.uses_custom_provider('/p/a') is False
    # provider defined but no base_url
    s3 = Settings(providers={'p': {'name': 'P', 'base_url': '',
                                     'api_key': '', 'models': []}},
                  model_default='p')
    assert s3.uses_custom_provider('/p/a') is False


def test_any_custom_provider_active():
    assert Settings().any_custom_provider_active() is False
    s = Settings(providers=_sample_providers(), model_default='ollama')
    assert s.any_custom_provider_active() is True
    # custom only via a per-project override
    s2 = Settings(providers=_sample_providers(),
                  provider_overrides={'/p/a': 'ollama'})
    assert s2.any_custom_provider_active() is True


def test_save_hardens_permissions(tmp_path):
    path = str(tmp_path / 'settings.json')
    Settings(providers=_sample_providers()).save(path)
    assert (os.stat(path).st_mode & 0o777) == 0o600
