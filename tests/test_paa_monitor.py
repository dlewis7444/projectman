from paa_monitor import (
    extract_file_references,
    check_missing_claude_md,
    check_context_drift,
    check_no_git,
    scan_project,
)


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
