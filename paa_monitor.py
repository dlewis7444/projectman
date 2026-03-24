import os
import re

from paa_ledger import LedgerItem, make_item_id, now_iso


_FILE_EXTENSIONS = frozenset({
    '.py', '.js', '.ts', '.tsx', '.jsx', '.md', '.sh', '.css',
    '.json', '.yaml', '.yml', '.toml', '.html', '.rs', '.go',
    '.java', '.rb', '.c', '.h', '.cpp', '.hpp', '.cfg', '.ini',
})


def extract_file_references(content):
    """Extract file/path references from backtick-quoted tokens in markdown."""
    refs = set()
    for match in re.finditer(r'`([^`\n]+)`', content):
        token = match.group(1).strip()
        if '://' in token:
            continue
        if ' ' in token:
            continue
        # Strip :line or :line-line suffix
        clean = re.sub(r':\d+(-\d+)?$', '', token)
        _, ext = os.path.splitext(clean)
        if ext.lower() not in _FILE_EXTENSIONS:
            continue
        # Skip dotted class names like Gtk.Box.new — multi-dot, no slash
        if '/' not in clean and clean.count('.') > 1:
            continue
        refs.add(clean)
    return refs


def check_missing_claude_md(project_name, project_path):
    """Flag projects with no CLAUDE.md."""
    if not os.path.exists(os.path.join(project_path, 'CLAUDE.md')):
        return [LedgerItem(
            id=make_item_id('missing-claude-md', project_name, ''),
            type='missing-claude-md',
            project=project_name,
            project_path=project_path,
            summary=f'{project_name} has no CLAUDE.md',
            evidence='No CLAUDE.md file found in project root',
            severity='warning',
            created=now_iso(),
        )]
    return []


def check_context_drift(project_name, project_path):
    """Flag CLAUDE.md references to files that no longer exist."""
    claude_md = os.path.join(project_path, 'CLAUDE.md')
    try:
        with open(claude_md) as f:
            content = f.read()
    except FileNotFoundError:
        return []
    refs = extract_file_references(content)
    items = []
    for ref in sorted(refs):
        if ref.startswith('~'):
            full = os.path.expanduser(ref)
        else:
            full = os.path.join(project_path, ref.lstrip('./'))
        if not os.path.exists(full):
            items.append(LedgerItem(
                id=make_item_id('context-drift', project_name, ref),
                type='context-drift',
                project=project_name,
                project_path=project_path,
                summary=f'CLAUDE.md references `{ref}` which does not exist',
                evidence=f'File reference `{ref}` in CLAUDE.md — not found on disk',
                severity='action-needed',
                created=now_iso(),
            ))
    return items


def check_no_git(project_name, project_path):
    """Flag projects that are not git repositories."""
    if not os.path.exists(os.path.join(project_path, '.git')):
        return [LedgerItem(
            id=make_item_id('no-git', project_name, ''),
            type='no-git',
            project=project_name,
            project_path=project_path,
            summary=f'{project_name} is not a git repository',
            evidence='No .git directory found in project root',
            severity='info',
            created=now_iso(),
        )]
    return []


def scan_project(project_name, project_path):
    """Run all checks on a single project, return list of LedgerItems."""
    items = []
    items.extend(check_missing_claude_md(project_name, project_path))
    items.extend(check_context_drift(project_name, project_path))
    items.extend(check_no_git(project_name, project_path))
    return items
