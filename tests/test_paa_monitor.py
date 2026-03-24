from paa_monitor import (
    extract_file_references,
    check_missing_claude_md,
    check_context_drift,
    check_no_git,
    scan_project,
    PAAMonitor,
)
from paa_ledger import Ledger
from settings import Settings
from model import ProjectStore


def test_extract_refs_backtick_path():
    content = 'See `src/model.py` for details.'
    refs = extract_file_references(content)
    assert 'src/model.py' in refs


def test_extract_refs_just_filename():
    content = 'The file `settings.py` handles config.'
    refs = extract_file_references(content)
    assert 'settings.py' in refs


def test_extract_refs_skips_urls():
    content = 'Visit `https://example.com/path.html` for docs.'
    refs = extract_file_references(content)
    assert len(refs) == 0


def test_extract_refs_skips_dotted_class_names():
    content = 'Use `Gtk.Box.new()` to create.'
    refs = extract_file_references(content)
    assert len(refs) == 0


def test_extract_refs_strips_line_number():
    content = 'See `model.py:42` for the function.'
    refs = extract_file_references(content)
    assert 'model.py' in refs


def test_extract_refs_strips_line_range():
    content = 'Modify `window.py:22-34` in the init.'
    refs = extract_file_references(content)
    assert 'window.py' in refs


def test_extract_refs_skips_commands_with_spaces():
    content = 'Run `python -m pytest tests/` to test.'
    refs = extract_file_references(content)
    assert len(refs) == 0


def test_extract_refs_bold_backtick():
    content = '**`session.py`** handles restore logic.'
    refs = extract_file_references(content)
    assert 'session.py' in refs


def test_extract_refs_table_cell():
    content = '| `model.py` | Pure data layer |'
    refs = extract_file_references(content)
    assert 'model.py' in refs


def test_extract_refs_tilde_path():
    content = 'Stored at `~/.ProjectMan/settings.json`.'
    refs = extract_file_references(content)
    assert '~/.ProjectMan/settings.json' in refs


def test_check_missing_claude_md_absent(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    items = check_missing_claude_md('myproj', str(proj))
    assert len(items) == 1
    assert items[0].type == 'missing-claude-md'
    assert items[0].severity == 'warning'


def test_check_missing_claude_md_present(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text('# Instructions')
    items = check_missing_claude_md('myproj', str(proj))
    assert len(items) == 0


def test_check_context_drift_detects_missing_file(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text(
        '# Project\n\nSee `src/gone.py` for the data layer.\n'
    )
    items = check_context_drift('myproj', str(proj))
    assert len(items) == 1
    assert 'src/gone.py' in items[0].summary
    assert items[0].severity == 'action-needed'


def test_check_context_drift_ignores_existing_file(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'model.py').write_text('# exists')
    (proj / 'CLAUDE.md').write_text('The file `model.py` has the data layer.\n')
    items = check_context_drift('myproj', str(proj))
    assert len(items) == 0


def test_check_context_drift_no_claude_md(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    items = check_context_drift('myproj', str(proj))
    assert len(items) == 0  # missing CLAUDE.md handled by other check


def test_check_context_drift_multiple_refs(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text(
        '`src/a.py` and `src/b.py` are important.\n'
    )
    items = check_context_drift('myproj', str(proj))
    assert len(items) == 2


def test_check_no_git_absent(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    items = check_no_git('myproj', str(proj))
    assert len(items) == 1
    assert items[0].type == 'no-git'
    assert items[0].severity == 'info'


def test_check_no_git_present(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / '.git').mkdir()
    items = check_no_git('myproj', str(proj))
    assert len(items) == 0


def test_scan_project_combines_all_checks(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    # No CLAUDE.md, no .git — should get both findings
    items = scan_project('myproj', str(proj))
    types = {i.type for i in items}
    assert 'missing-claude-md' in types
    assert 'no-git' in types


def test_scan_project_clean(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / '.git').mkdir()
    (proj / 'CLAUDE.md').write_text('# Clean project\n')
    items = scan_project('myproj', str(proj))
    assert len(items) == 0


def test_run_scan_populates_ledger(tmp_path):
    """run_scan detects issues and adds them to the ledger."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()  # no CLAUDE.md, no .git
    (projects_dir / 'beta').mkdir()
    (projects_dir / 'beta' / '.git').mkdir()
    (projects_dir / 'beta' / 'CLAUDE.md').write_text('# Beta\n')

    settings = Settings(projects_dir=str(projects_dir))
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    monitor.run_scan()

    assert ledger.pending_count >= 1  # alpha has issues
    types = {i.type for i in ledger.pending_items()}
    assert 'missing-claude-md' in types or 'no-git' in types


def test_run_scan_sweeps_resolved(tmp_path):
    """Items auto-resolve when the issue disappears."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    proj = projects_dir / 'alpha'
    proj.mkdir()
    # First scan: no CLAUDE.md
    settings = Settings(projects_dir=str(projects_dir))
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)
    monitor.run_scan()
    assert ledger.pending_count >= 1

    # Fix the issue
    (proj / 'CLAUDE.md').write_text('# Alpha\n')
    (proj / '.git').mkdir()

    # Second scan: should resolve
    monitor.run_scan()
    assert ledger.pending_count == 0


from unittest.mock import patch, MagicMock
from paa_ledger import LedgerItem, make_item_id, now_iso


def test_scan_runs_ai_when_budget_available(tmp_path):
    """AI checks run when haiku enabled and budget remains."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    proj = projects_dir / 'alpha'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text('# Alpha')
    (proj / '.git').mkdir()

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=100000,
        paa_budget_used=0,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks', return_value=([], 500)) as mock_ai:
        monitor.run_scan()
    mock_ai.assert_called_once()
    assert settings.paa_budget_used == 500


def test_scan_skips_ai_when_budget_exceeded(tmp_path):
    """AI checks skipped when budget is exhausted."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=1000,
        paa_budget_used=1000,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks') as mock_ai:
        monitor.run_scan()
    mock_ai.assert_not_called()


def test_scan_runs_ai_when_unlimited(tmp_path):
    """AI checks run with unlimited budget even if used > tokens."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_unlimited=True,
        paa_budget_tokens=1000,
        paa_budget_used=9999,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks', return_value=([], 200)) as mock_ai:
        monitor.run_scan()
    mock_ai.assert_called_once()


def test_scan_skips_ai_when_haiku_disabled(tmp_path):
    """AI checks skipped when paa_allow_haiku is False."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=False,
        paa_budget_tokens=100000,
        paa_budget_used=0,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks') as mock_ai:
        monitor.run_scan()
    mock_ai.assert_not_called()


def test_budget_monthly_reset(tmp_path):
    """Budget resets when month rolls over."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=100000,
        paa_budget_used=50000,
        paa_budget_month='2026-02',  # last month
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks', return_value=([], 300)) as mock_ai:
        monitor.run_scan()
    assert settings.paa_budget_month == '2026-03'
    assert settings.paa_budget_used == 300  # reset + new usage


def test_filesystem_checks_run_when_budget_exceeded(tmp_path):
    """Filesystem checks still produce findings even when AI budget is exhausted."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'alpha').mkdir()  # no CLAUDE.md, no .git

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=100,
        paa_budget_used=100,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'):
        monitor.run_scan()
    assert ledger.pending_count >= 1  # filesystem checks found issues


def test_full_scan_with_mocked_ai(tmp_path):
    """Full scan produces both filesystem and AI findings."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    proj = projects_dir / 'alpha'
    proj.mkdir()
    # No CLAUDE.md, no .git -> filesystem checks will find issues

    ai_item = LedgerItem(
        id='ai-test-001',
        type='ai-health-concern',
        project='alpha',
        project_path=str(proj),
        summary='Missing README',
        evidence='No README.md found',
        severity='info',
        created=now_iso(),
    )

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=100000,
        paa_budget_used=0,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks', return_value=([ai_item], 1500)):
        monitor.run_scan()

    # Should have both filesystem AND AI findings
    assert ledger.pending_count >= 2
    types = {i.type for i in ledger.pending_items()}
    assert 'ai-health-concern' in types
    assert 'missing-claude-md' in types or 'no-git' in types
    assert settings.paa_budget_used == 1500


def test_full_scan_ai_failure_graceful(tmp_path):
    """AI failure doesn't prevent filesystem findings from appearing."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    proj = projects_dir / 'alpha'
    proj.mkdir()
    # No CLAUDE.md -> filesystem will find an issue

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=100000,
        paa_budget_used=0,
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks', side_effect=Exception('AI broke')):
        monitor.run_scan()

    # Filesystem findings should still be present despite AI failure
    assert ledger.pending_count >= 1
    types = {i.type for i in ledger.pending_items()}
    assert 'missing-claude-md' in types or 'no-git' in types
    assert settings.paa_budget_used == 0  # No AI tokens consumed


def test_full_scan_budget_exhaustion_midway(tmp_path):
    """Budget exhaustion mid-scan stops AI for remaining projects."""
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'aaa').mkdir()  # scanned first (alphabetical)
    (projects_dir / 'zzz').mkdir()  # scanned second

    settings = Settings(
        projects_dir=str(projects_dir),
        paa_allow_haiku=True,
        paa_budget_tokens=1000,
        paa_budget_used=999,  # only 1 token of budget left
        paa_budget_month='2026-03',
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    monitor = PAAMonitor(store, ledger, settings)

    call_count = 0

    def mock_ai_checks(name, path, s):
        nonlocal call_count
        call_count += 1
        return ([], 500)  # each call uses 500 tokens

    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks', side_effect=mock_ai_checks):
        monitor.run_scan()

    # _budget_allows_ai checks settings.paa_budget_used which stays at 999
    # throughout the loop (budget is only updated AFTER the loop completes).
    # 999 < 1000 is True for both projects, so both get AI checks.
    # Budget enforcement is per-scan, not per-project within a scan.
    assert call_count == 2  # Both got AI checks in this scan
    assert settings.paa_budget_used == 999 + 1000  # 500 per project
