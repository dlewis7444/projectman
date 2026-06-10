"""Pure helpers behind the Settings → Agents page (B3): bridge install + doctor.

Headless; no GTK. The GUI page (settings_window._build_agents_page) is thin glue
over these.
"""
import os

import agents
from settings import Settings


# ── agent_bridge_source ───────────────────────────────────────────────────────

def test_bridge_source_found(tmp_path):
    app = tmp_path
    (app / 'bridges' / 'opencode').mkdir(parents=True)
    f = app / 'bridges' / 'opencode' / 'projectman.js'
    f.write_text('// bridge')
    assert agents.agent_bridge_source(str(app), 'opencode') == str(f)


def test_bridge_source_missing_file(tmp_path):
    assert agents.agent_bridge_source(str(tmp_path), 'opencode') is None


def test_bridge_source_unknown_agent(tmp_path):
    assert agents.agent_bridge_source(str(tmp_path), 'claude') is None


def test_real_repo_ships_opencode_bridge():
    """The actual repo tree carries the opencode bridge where the GUI expects."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert agents.agent_bridge_source(repo, 'opencode') is not None


def test_real_repo_ships_grok_bridge():
    """T-B4: the repo carries the grok bridge (the JSON hook definition) where
    the GUI 'Install bridge' button + install_agent_bridge expect it. With this
    present, the Settings → Agents page shows an Install-bridge row for grok
    automatically (it checks ``agent_bridge_source(...) is not None``)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = agents.agent_bridge_source(repo, 'grok')
    assert src is not None
    assert src.endswith(os.path.join('bridges', 'grok', 'projectman.json'))


def test_three_agents_registered_with_display_names():
    """T-B4: the registry now carries THREE builtins; the settings page + the
    sidebar submenu iterate ADAPTERS, so all three appear by construction."""
    ids = list(agents.ADAPTERS.keys())
    assert 'claude' in ids and 'opencode' in ids and 'grok' in ids
    assert len(ids) >= 3
    names = {aid: agents.ADAPTERS[aid].display_name for aid in ids}
    assert names['claude'] == 'Claude Code'
    assert names['opencode'] == 'opencode'
    assert names['grok'] == 'Grok Build'


def test_install_grok_bridge_copies_json_definition(tmp_path):
    """install_agent_bridge drops the grok hook JSON into ~/.grok/hooks/."""
    app = tmp_path / 'app'
    (app / 'bridges' / 'grok').mkdir(parents=True)
    (app / 'bridges' / 'grok' / 'projectman.json').write_text('{"hooks":{}}')
    home = tmp_path / 'home'
    home.mkdir()
    result = agents.install_agent_bridge(str(app), 'grok', home=str(home))
    assert result == 'installed'
    dest = home / '.grok' / 'hooks' / 'projectman.json'
    assert dest.exists()
    assert dest.read_text() == '{"hooks":{}}'


# ── install_agent_bridge ──────────────────────────────────────────────────────

def _app_with_bridge(tmp_path, content='// bridge v1'):
    app = tmp_path / 'app'
    (app / 'bridges' / 'opencode').mkdir(parents=True)
    (app / 'bridges' / 'opencode' / 'projectman.js').write_text(content)
    return app


def test_install_bridge_fresh(tmp_path):
    app = _app_with_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    result = agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    assert result == 'installed'
    dest = home / '.config' / 'opencode' / 'plugins' / 'projectman.js'
    assert dest.exists()
    assert dest.read_text() == '// bridge v1'


def test_install_bridge_idempotent(tmp_path):
    app = _app_with_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    # Second run with identical content → 'already'.
    assert agents.install_agent_bridge(str(app), 'opencode', home=str(home)) == 'already'


def test_install_bridge_updates_changed(tmp_path):
    app = _app_with_bridge(tmp_path, content='// v1')
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    # Change the source → next install copies again.
    (app / 'bridges' / 'opencode' / 'projectman.js').write_text('// v2')
    assert agents.install_agent_bridge(str(app), 'opencode', home=str(home)) == 'installed'
    dest = home / '.config' / 'opencode' / 'plugins' / 'projectman.js'
    assert dest.read_text() == '// v2'


def test_install_bridge_no_bridge_agent(tmp_path):
    assert agents.install_agent_bridge(str(tmp_path), 'claude', home=str(tmp_path)) == 'no-bridge'


def test_install_bridge_missing_source(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    # app dir has no bridges/.
    assert agents.install_agent_bridge(str(tmp_path / 'app'), 'opencode',
                                       home=str(home)) == 'missing-source'


# ── agent_doctor ──────────────────────────────────────────────────────────────

def test_doctor_ok_reports_version():
    def fake_run(argv):
        assert argv == ['opencode', '--version']
        return (0, '1.16.2\n')
    ok, detail = agents.agent_doctor(Settings(), 'opencode', run_fn=fake_run)
    assert ok is True
    assert detail == '1.16.2'


def test_doctor_uses_custom_binary():
    seen = {}

    def fake_run(argv):
        seen['argv'] = argv
        return (0, 'x')
    s = Settings(agents={'opencode': {'binary': '/opt/oc/opencode'}})
    agents.agent_doctor(s, 'opencode', run_fn=fake_run)
    assert seen['argv'][0] == '/opt/oc/opencode'


def test_doctor_claude_uses_resolved_binary():
    seen = {}

    def fake_run(argv):
        seen['argv'] = argv
        return (0, 'claude 2.1.170')
    s = Settings(claude_binary='/usr/local/bin/claude')
    ok, detail = agents.agent_doctor(s, 'claude', run_fn=fake_run)
    assert seen['argv'] == ['/usr/local/bin/claude', '--version']
    assert ok and 'claude' in detail


def test_doctor_not_runnable():
    def fake_run(argv):
        return None  # OSError under the hood
    ok, detail = agents.agent_doctor(Settings(), 'opencode', run_fn=fake_run)
    assert ok is False
    assert 'not found' in detail or 'not runnable' in detail


def test_doctor_nonzero_exit():
    def fake_run(argv):
        return (1, '')
    ok, detail = agents.agent_doctor(Settings(), 'opencode', run_fn=fake_run)
    assert ok is False
