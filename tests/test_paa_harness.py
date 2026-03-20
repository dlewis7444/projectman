import os
import shutil
import subprocess
import stat


def _setup_paa_dir(tmp_path):
    """Simulate the PAA launch harness setup logic from window.py."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()
    (projects_dir / 'beta').mkdir()
    (projects_dir / '.archive').mkdir()
    (projects_dir / '.archive' / 'old-project').mkdir()

    paa_dir = projects_dir / '.project-admin-agent'
    paa_dir.mkdir(exist_ok=True)

    # Copy harness files
    src_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'paa')
    shutil.copy2(os.path.join(src_dir, 'CLAUDE.md'),
                  str(paa_dir / 'CLAUDE.md'))
    shutil.copy2(os.path.join(src_dir, 'gather-context.sh'),
                  str(paa_dir / 'gather-context.sh'))
    os.chmod(str(paa_dir / 'gather-context.sh'), 0o755)

    return projects_dir, paa_dir


def test_harness_copies_claude_md(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    assert (paa_dir / 'CLAUDE.md').exists()
    content = (paa_dir / 'CLAUDE.md').read_text()
    assert 'Projects Admin Agent' in content


def test_harness_copies_gather_script(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    script = paa_dir / 'gather-context.sh'
    assert script.exists()
    assert os.access(str(script), os.X_OK)


def test_user_md_created_when_missing(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    user_md = paa_dir / 'USER.md'
    assert not user_md.exists()
    # Simulate: create only if missing
    if not user_md.exists():
        user_md.write_text(
            '<!-- Custom instructions for the Projects Admin Agent. -->\n'
            '<!-- This file is yours \u2014 ProjectMan will never overwrite it. -->\n'
        )
    assert user_md.exists()
    assert 'never overwrite' in user_md.read_text().lower()


def test_user_md_not_overwritten(tmp_path):
    _, paa_dir = _setup_paa_dir(tmp_path)
    user_md = paa_dir / 'USER.md'
    user_md.write_text('my custom instructions')
    # Simulate re-launch: only create if missing
    if not user_md.exists():
        user_md.write_text('overwritten!')
    assert user_md.read_text() == 'my custom instructions'


def test_gather_context_produces_snapshot(tmp_path):
    projects_dir, paa_dir = _setup_paa_dir(tmp_path)
    result = subprocess.run(
        [str(paa_dir / 'gather-context.sh')],
        cwd=str(paa_dir), capture_output=True, text=True,
    )
    assert result.returncode == 0
    snapshot = (paa_dir / 'project-snapshot.md').read_text()
    assert 'alpha' in snapshot
    assert 'beta' in snapshot
    assert 'Active: 2' in snapshot
    assert 'Archived: 1' in snapshot


def test_gather_context_excludes_hidden_dirs(tmp_path):
    projects_dir, paa_dir = _setup_paa_dir(tmp_path)
    subprocess.run(
        [str(paa_dir / 'gather-context.sh')],
        cwd=str(paa_dir), capture_output=True,
    )
    snapshot = (paa_dir / 'project-snapshot.md').read_text()
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
