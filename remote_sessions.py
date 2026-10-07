"""Remote harness session listing over SSH.

Local adapters list sessions from local disk/CLI. For remote projects the same
CLI/storage lives on the *host*, so we re-run list commands via SSH from the
project's ``remote_cwd`` (or fetch remote history files).

No GTK. Unit tests mock ``run_ssh``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from hosts import LOCALHOST_ID
from harnesses import SessionRef

if TYPE_CHECKING:
    from hosts import HostProfile
    from model import Project


def is_remote_project(project) -> bool:
    hid = getattr(project, 'host_id', None) or LOCALHOST_ID
    return hid != LOCALHOST_ID


def host_profile_for_project(settings, project) -> HostProfile | None:
    if settings is None or project is None:
        return None
    hid = getattr(project, 'host_id', None) or LOCALHOST_ID
    if hid == LOCALHOST_ID:
        return None
    return settings.host_profiles().get(hid)


def match_session_project_path(history_path: str, project) -> bool:
    """Whether a history/workDir path refers to *project* on its host.

    History files store absolute remote paths; Project uses ``remote_cwd`` which
    may still be ``~/…``. Match by basename (project name) and/or suffix.
    """
    if not history_path or project is None:
        return False
    name = (getattr(project, 'name', None) or '').strip()
    if not name:
        return False
    # Normalize trailing slashes
    hp = history_path.rstrip('/')
    if hp.endswith('/' + name) or hp == name:
        return True
    import os
    if os.path.basename(hp) == name:
        return True
    rcwd = getattr(project, 'remote_cwd', None) or ''
    if rcwd:
        rc = rcwd.rstrip('/')
        if rc.startswith('~/'):
            # history usually absolute; compare suffix after ~
            if hp.endswith(rc[1:]) or hp.endswith(rc[2:] if rc.startswith('~/') else rc):
                return True
        if hp == rc or hp.endswith(rc):
            return True
    return False


def parse_claude_history_text(text: str, project, *, cap: int = 7) -> list[SessionRef]:
    """Parse ``~/.claude/history.jsonl`` text into SessionRefs for *project*."""
    import json
    sessions = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = entry.get('sessionId', '')
        if not sid:
            continue
        proj = entry.get('project', '') or ''
        if not match_session_project_path(proj, project):
            continue
        ts = entry.get('timestamp', 0) or 0
        display = entry.get('display', '') or ''
        if sid not in sessions:
            sessions[sid] = {
                'title': display,
                'last_active': ts,
            }
        else:
            sessions[sid]['last_active'] = max(sessions[sid]['last_active'], ts)
            if display and not sessions[sid]['title']:
                sessions[sid]['title'] = display
    refs = [
        SessionRef(id=sid, title=info['title'] or sid[:12], last_active=info['last_active'])
        for sid, info in sessions.items()
    ]
    refs.sort(key=lambda r: r.last_active, reverse=True)
    return refs[:cap]


def list_remote_claude_sessions(profile, project, *, run_ssh_fn=None, timeout: float = 12) -> list[SessionRef]:
    """Fetch remote Claude history.jsonl and return SessionRefs for *project*."""
    from ssh_transport import build_remote_cat_argv, run_ssh
    run = run_ssh_fn or run_ssh
    argv = build_remote_cat_argv(profile.ssh_target, '.claude/history.jsonl')
    rc, out, _err = run(argv, timeout=timeout)
    if rc != 0 or not (out or '').strip():
        return []
    return parse_claude_history_text(out, project)


def list_remote_cli_sessions(
    profile,
    project,
    argv: list,
    parse_fn,
    *,
    run_ssh_fn=None,
    timeout: float = 12,
) -> list[SessionRef]:
    """Run a cwd-scoped session-list CLI on the remote host; parse stdout.

    *parse_fn(stdout) -> list[SessionRef]* (or list the adapter already uses).
    """
    from ssh_transport import build_remote_capture_argv, run_ssh
    run = run_ssh_fn or run_ssh
    remote_cwd = getattr(project, 'spawn_cwd', None) or getattr(project, 'remote_cwd', None)
    if not remote_cwd:
        return []
    full = build_remote_capture_argv(profile.ssh_target, remote_cwd, argv)
    rc, out, _err = run(full, timeout=timeout)
    if rc != 0 or not (out or '').strip():
        return []
    try:
        refs = parse_fn(out)
    except Exception:
        return []
    return list(refs) if refs else []


def parse_opencode_sessions_remote(text: str, project, *, cap: int = 7) -> list[SessionRef]:
    """Parse opencode JSON session list for a remote project (no local realpath)."""
    import json
    try:
        data = json.loads(text or '')
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    refs = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        directory = entry.get('directory') or entry.get('worktree') or ''
        if not match_session_project_path(str(directory), project):
            continue
        sid = entry.get('id') or entry.get('sessionID') or ''
        if not sid:
            continue
        last = entry.get('updated')
        if last is None:
            last = entry.get('created', 0)
        refs.append(SessionRef(
            id=str(sid),
            title=str(entry.get('title') or ''),
            last_active=int(last) if isinstance(last, (int, float)) else 0,
        ))
    refs.sort(key=lambda r: r.last_active, reverse=True)
    return refs[:cap]

def list_remote_kimi_sessions(profile, project, *, run_ssh_fn=None, timeout: float = 12) -> list[SessionRef]:
    """Fetch remote kimi session_index.jsonl + match workDir to project.

    Index lines do not carry titles/timestamps (those live in per-session
    ``state.json``); remote list uses session id as title and last_active=0.
    Enough for resume-by-id in the expander.
    """
    from ssh_transport import build_remote_cat_argv, run_ssh
    from harnesses import parse_kimi_session_index_lines, SessionRef as SR
    run = run_ssh_fn or run_ssh
    argv = build_remote_cat_argv(profile.ssh_target, '.kimi-code/session_index.jsonl')
    rc, out, _err = run(argv, timeout=timeout)
    if rc != 0 or not (out or '').strip():
        return []
    refs = []
    for entry in parse_kimi_session_index_lines(out):
        work = entry.get('workDir') or ''
        if not match_session_project_path(str(work), project):
            continue
        sid = entry.get('sessionId') or ''
        if not sid:
            continue
        refs.append(SR(id=str(sid), title=str(sid)[:16], last_active=0))
    # File order is oldest-first; reverse for newest-first expander default.
    refs.reverse()
    return refs[:7]