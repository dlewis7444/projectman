import os
import shutil
import subprocess
from pathlib import Path

import pytest

from paa_deploy import PAAHarnessError, deploy_paa_harness, resolve_paa_package_files


_PKG = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'paa')


def _setup_paa_dir(tmp_path):
    """Deploy the real PAA harness into a temp projects dir."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()
    (projects_dir / 'beta').mkdir()
    (projects_dir / '.archive').mkdir()
    (projects_dir / '.archive' / 'old-project').mkdir()
    paa_dir = Path(deploy_paa_harness(_PKG, str(projects_dir)))
    return projects_dir, paa_dir


def test_harness_copies_agents_md(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    assert (paa_dir / 'AGENTS.md').exists()
    content = (paa_dir / 'AGENTS.md').read_text()
    assert 'Projects Admin Agent' in content
    # AGENTS-only: do not dual-write CLAUDE.md.
    assert not (paa_dir / 'CLAUDE.md').exists()


def test_package_ships_agents_md_not_claude_md():
    """PAA package SoT is AGENTS.md. Shipping CLAUDE.md fights the lab
    AGENTS policy and the 7059ea4 migration."""
    assert os.path.isfile(os.path.join(_PKG, 'AGENTS.md'))
    assert os.path.isfile(os.path.join(_PKG, 'AGENTS-SUPPLEMENT.md'))
    assert not os.path.isfile(os.path.join(_PKG, 'CLAUDE.md'))
    assert not os.path.isfile(os.path.join(_PKG, 'CLAUDE-SUPPLEMENT.md'))


def test_deploy_from_agents_only_src(tmp_path):
    src = tmp_path / 'pkg'
    src.mkdir()
    shutil.copy2(os.path.join(_PKG, 'AGENTS.md'), src / 'AGENTS.md')
    shutil.copy2(os.path.join(_PKG, 'AGENTS-SUPPLEMENT.md'),
                 src / 'AGENTS-SUPPLEMENT.md')
    shutil.copy2(os.path.join(_PKG, 'gather-context.sh'), src / 'gather-context.sh')
    shutil.copy2(os.path.join(_PKG, 'settings.json'), src / 'settings.json')
    projects = tmp_path / 'projects'
    projects.mkdir()
    paa_dir = Path(deploy_paa_harness(str(src), str(projects)))
    assert (paa_dir / 'AGENTS.md').is_file()
    assert not (paa_dir / 'CLAUDE.md').exists()
    assert (paa_dir / '.system' / 'AGENTS-SUPPLEMENT.md').is_file()
    assert not (paa_dir / '.system' / 'CLAUDE-SUPPLEMENT.md').exists()


def test_deploy_from_legacy_claude_only_src(tmp_path):
    """Old install trees that still only have CLAUDE.md can be *read*;
    deploy still writes AGENTS.md (never CLAUDE.md)."""
    src = tmp_path / 'pkg'
    src.mkdir()
    (src / 'CLAUDE.md').write_text('# Projects Admin Agent\n')
    (src / 'CLAUDE-SUPPLEMENT.md').write_text('supplement\n')
    shutil.copy2(os.path.join(_PKG, 'gather-context.sh'), src / 'gather-context.sh')
    shutil.copy2(os.path.join(_PKG, 'settings.json'), src / 'settings.json')
    projects = tmp_path / 'projects'
    projects.mkdir()
    paa_dir = Path(deploy_paa_harness(str(src), str(projects)))
    assert (paa_dir / 'AGENTS.md').is_file()
    assert (paa_dir / 'AGENTS.md').read_text().startswith('# Projects Admin Agent')
    assert not (paa_dir / 'CLAUDE.md').exists()
    rules = resolve_paa_package_files(str(src))
    assert os.path.basename(rules['rules']) == 'CLAUDE.md'


def test_deploy_missing_rules_raises(tmp_path):
    src = tmp_path / 'pkg'
    src.mkdir()
    shutil.copy2(os.path.join(_PKG, 'gather-context.sh'), src / 'gather-context.sh')
    shutil.copy2(os.path.join(_PKG, 'settings.json'), src / 'settings.json')
    projects = tmp_path / 'projects'
    projects.mkdir()
    with pytest.raises(PAAHarnessError, match='AGENTS.md'):
        deploy_paa_harness(str(src), str(projects))


def test_card_window_surfaces_chat_deploy_errors():
    """Chat click must not fail silently (GTK logs FileNotFoundError and
    the user sees nothing). Error lands on the cards-side banner."""
    src = Path(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'paa_card_window.py')).read_text()
    assert 'deploy_paa_harness' in src
    assert 'paa_interactive_plan' in src
    assert '_spawn_chat' in src
    assert '_show_chat_error' in src
    assert '_error_bar' in src
    assert 'PAA chat failed to start' in src
    assert 'resolved_claude_binary' not in src
    assert "vte_captures_for(self._chat_harness_id)" in src


def test_harness_copies_gather_script(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    script = paa_dir / '.system' / 'gather-context.sh'
    assert script.exists()
    assert os.access(str(script), os.X_OK)


def test_user_md_created_when_missing(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    user_md = paa_dir / 'USER.md'
    assert user_md.exists()
    assert 'never overwrite' in user_md.read_text().lower()


def test_user_md_not_overwritten(tmp_path):
    projects_dir, paa_dir = _setup_paa_dir(tmp_path)
    user_md = paa_dir / 'USER.md'
    user_md.write_text('my custom instructions')
    deploy_paa_harness(_PKG, str(projects_dir))
    assert user_md.read_text() == 'my custom instructions'


def test_journal_created_when_missing(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    journal = paa_dir / 'paa-journal.md'
    assert not journal.exists()
    if not journal.exists():
        journal.write_text(
            '<!-- PAA session journal. Written by the harness, never overwritten by PM. -->\n'
        )
    assert journal.exists()


def test_journal_not_overwritten(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    journal = paa_dir / 'paa-journal.md'
    journal.write_text('## Session 2026-03-24\n- did stuff\n')
    if not journal.exists():
        journal.write_text('overwritten!')
    assert 'did stuff' in journal.read_text()


def test_gather_context_produces_snapshot(tmp_path):
    projects_dir, paa_dir = _setup_paa_dir(tmp_path)
    system_dir = paa_dir / '.system'
    result = subprocess.run(
        [str(system_dir / 'gather-context.sh')],
        cwd=str(system_dir), capture_output=True, text=True,
    )
    assert result.returncode == 0
    snapshot = (system_dir / 'project-snapshot.md').read_text()
    assert 'alpha' in snapshot
    assert 'beta' in snapshot
    assert 'Active: 2' in snapshot
    assert 'Archived: 1' in snapshot


def test_gather_context_excludes_hidden_dirs(tmp_path):
    projects_dir, paa_dir = _setup_paa_dir(tmp_path)
    system_dir = paa_dir / '.system'
    subprocess.run(
        [str(system_dir / 'gather-context.sh')],
        cwd=str(system_dir), capture_output=True,
    )
    snapshot = (system_dir / 'project-snapshot.md').read_text()
    assert '.archive' not in snapshot
    assert '.project-admin-agent' not in snapshot


def test_hidden_dir_skipped_by_project_store(tmp_path):
    """Verify .project-admin-agent is invisible to ProjectStore."""
    from settings import Settings
    from model import ProjectStore
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'real-project').mkdir()
    (projects_dir / '.project-admin-agent').mkdir()
    settings = Settings(projects_dir=str(projects_dir))
    store = ProjectStore(settings)
    names = [p.name for p in store.load_projects()]
    assert 'real-project' in names
    assert '.project-admin-agent' not in names
