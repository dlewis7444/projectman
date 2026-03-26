import os
import re
from datetime import datetime, timezone

import gi
from gi.repository import GObject, GLib
import threading

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


def _current_month():
    return datetime.now(timezone.utc).strftime('%Y-%m')


def _maybe_reset_budget(settings):
    """Reset budget counter if the month has rolled over."""
    month = _current_month()
    if settings.paa_budget_month != month:
        settings.paa_budget_used = 0
        settings.paa_budget_month = month
        return True
    return False


def _budget_allows_ai(settings):
    """Check if the token budget allows AI checks."""
    if not settings.paa_allow_haiku:
        return False
    if settings.paa_budget_unlimited:
        return True
    return settings.paa_budget_used < settings.paa_budget_tokens


def scan_project(project_name, project_path):
    """Run all checks on a single project, return list of LedgerItems."""
    items = []
    items.extend(check_missing_claude_md(project_name, project_path))
    items.extend(check_context_drift(project_name, project_path))
    items.extend(check_no_git(project_name, project_path))
    return items


class PAAMonitor(GObject.GObject):
    """Background monitor that periodically scans projects for issues."""
    __gsignals__ = {
        'findings-changed': (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, store, ledger, settings):
        super().__init__()
        self._store = store
        self._ledger = ledger
        self._settings = settings
        self._timer_id = None
        self._initial_id = None
        self._scanning = False

    def start(self):
        if self._timer_id is not None or self._initial_id is not None:
            return
        self._initial_id = GLib.timeout_add(2000, self._initial_scan)

    def _initial_scan(self):
        self._initial_id = None
        self.schedule_scan()
        interval_ms = max(5, self._settings.paa_loop_interval_minutes) * 60 * 1000
        self._timer_id = GLib.timeout_add(interval_ms, self._on_timer)
        return GLib.SOURCE_REMOVE

    def _on_timer(self):
        if not self._settings.paa_enabled:
            self._timer_id = None
            return GLib.SOURCE_REMOVE
        self.schedule_scan()
        return GLib.SOURCE_CONTINUE

    def schedule_scan(self):
        """Run scan in background thread to avoid blocking the UI."""
        if self._scanning:
            return
        self._scanning = True
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        try:
            self.run_scan()
        finally:
            self._scanning = False

    def stop(self):
        if self._initial_id is not None:
            GLib.source_remove(self._initial_id)
            self._initial_id = None
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def restart(self):
        self.stop()
        if self._settings.paa_enabled:
            self.start()

    def run_scan(self):
        """Execute all checks across all active projects. Update ledger."""
        _maybe_reset_budget(self._settings)
        projects = self._store.load_projects()
        all_findings = []
        total_ai_tokens = 0

        for project in projects:
            # Phase 1 filesystem checks (always run)
            items = scan_project(project.name, project.path)
            all_findings.extend(items)

            # Phase 2 AI checks (budget-gated)
            if _budget_allows_ai(self._settings):
                try:
                    from paa_haiku import run_ai_checks
                    ai_items, tokens = run_ai_checks(
                        project.name, project.path, self._settings
                    )
                    all_findings.extend(ai_items)
                    total_ai_tokens += tokens
                except Exception:
                    pass  # AI failure must not block filesystem findings

        # Update budget
        if total_ai_tokens > 0:
            self._settings.paa_budget_used += total_ai_tokens
            self._settings.save()

        active_ids = {item.id for item in all_findings}
        self._ledger.sweep(active_ids)

        for item in all_findings:
            self._ledger.add_if_new(item)

        self._ledger.save()
        count = self._ledger.pending_count
        GLib.idle_add(lambda: self.emit('findings-changed', count) or False)
