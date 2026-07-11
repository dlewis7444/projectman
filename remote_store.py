"""Remote project store — list/create/rename/delete over SSH.

Pure orchestration over ``ssh_transport`` (no GTK). Callers run on the UI
thread should offload ``run_ssh`` to a worker if latency matters; for v1
synchronous calls on host add / refresh are acceptable.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from hosts import LOCALHOST_ID, HostProfile, is_safe_project_name
from model import Project
from ssh_transport import (
    build_ensure_projects_dir_argv,
    build_list_projects_argv,
    build_mkdir_project_argv,
    build_rename_project_argv,
    build_rmdir_project_argv,
    parse_ls_project_names,
    run_ssh,
    classify_health,
    HealthState,
)

if TYPE_CHECKING:
    pass


def remote_project_path(projects_dir: str, name: str) -> str:
    """Logical remote path for a project (may use ``~/…``)."""
    base = (projects_dir or '~/.ProjectMan/projects').rstrip('/')
    return f'{base}/{name}'


def list_remote_projects(
    profile: HostProfile,
    *,
    timeout: float = 15,
) -> tuple[list[Project], str | None]:
    """Return ``(projects, error_or_None)`` for *profile*.

    Ensures the projects dir exists, lists names, builds Project rows with
    ``host_id`` and logical remote paths.
    """
    argv = build_list_projects_argv(
        profile.ssh_target, profile.remote_projects_dir,
    )
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return [], (err or out or f'ssh failed (rc={rc})').strip()
    from hosts import encode_project_ref
    names = parse_ls_project_names(out)
    projects = []
    for name in sorted(names, key=str.lower):
        projects.append(Project(
            name=name,
            path=encode_project_ref(profile.id, name),
            host_id=profile.id,
            remote_cwd=remote_project_path(profile.remote_projects_dir, name),
        ))
    return projects, None


def ensure_remote_projects_dir(
    profile: HostProfile, *, timeout: float = 15,
) -> tuple[bool, str | None]:
    argv = build_ensure_projects_dir_argv(
        profile.ssh_target, profile.remote_projects_dir,
    )
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return False, (err or out or f'ssh failed (rc={rc})').strip()
    return True, None


def create_remote_project(
    profile: HostProfile, name: str, *, timeout: float = 15,
) -> tuple[Project | None, str | None]:
    if not is_safe_project_name(name):
        return None, f'Invalid project name: {name!r}'
    try:
        argv = build_mkdir_project_argv(
            profile.ssh_target, profile.remote_projects_dir, name,
        )
    except ValueError as e:
        return None, str(e)
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return None, (err or out or f'ssh failed (rc={rc})').strip()
    from hosts import encode_project_ref
    return Project(
        name=name,
        path=encode_project_ref(profile.id, name),
        host_id=profile.id,
        remote_cwd=remote_project_path(profile.remote_projects_dir, name),
    ), None


def rename_remote_project(
    profile: HostProfile, old_name: str, new_name: str, *, timeout: float = 15,
) -> tuple[bool, str | None]:
    try:
        argv = build_rename_project_argv(
            profile.ssh_target, profile.remote_projects_dir, old_name, new_name,
        )
    except ValueError as e:
        return False, str(e)
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return False, (err or out or f'ssh failed (rc={rc})').strip()
    return True, None


def delete_remote_project(
    profile: HostProfile, name: str, *, timeout: float = 15,
) -> tuple[bool, str | None]:
    try:
        argv = build_rmdir_project_argv(
            profile.ssh_target, profile.remote_projects_dir, name,
        )
    except ValueError as e:
        return False, str(e)
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return False, (err or out or f'ssh failed (rc={rc})').strip()
    return True, None


def probe_host_health(
    profile: HostProfile,
    *,
    checks_enabled: bool,
    timeout: float = 10,
) -> tuple[str, list[Project], str | None]:
    """Return ``(health_state, projects_or_stale_empty, detail_tooltip)``.

    When checks disabled → grey. On SSH failure → red. On list failure after
    SSH-ish success → yellow. On success → green + project list.
    """
    if not checks_enabled:
        return HealthState.GREY, [], 'Health checks disabled'
    projects, err = list_remote_projects(profile, timeout=timeout)
    if err is not None:
        # Distinguish total SSH fail vs partial: list already failed.
        # Treat any list failure as red if stderr looks like ssh, else yellow.
        low = (err or '').lower()
        ssh_fail = any(s in low for s in (
            'permission denied', 'connection refused', 'could not resolve',
            'no route', 'timed out', 'connection timed out', 'ssh failed',
            'host key', 'not known',
        ))
        if ssh_fail or 'ssh' in low:
            state = classify_health(False, False, checks_enabled=True)
        else:
            state = classify_health(True, False, checks_enabled=True)
        return state, [], err
    return HealthState.GREEN, projects, None


def fetch_remote_status_snapshots(
    profile: HostProfile, *, timeout: float = 15,
) -> tuple[list, str | None]:
    """Fetch remote ``~/.ProjectMan/status/*.json`` as StatusSnapshot-like objects.

    Returns ``(list[StatusSnapshot], error_or_None)``. Best-effort: missing
    status dir yields empty list, not error.
    """
    from model import StatusSnapshot
    script = r'''
dir="$HOME/.ProjectMan/status"
if [ ! -d "$dir" ]; then exit 0; fi
for f in "$dir"/*.json; do
  [ -f "$f" ] || continue
  cat "$f" 2>/dev/null || true
  printf '\n---PM---\n'
done
'''
    from ssh_transport import build_ssh_base_argv
    import shlex
    import json
    argv = build_ssh_base_argv(profile.ssh_target) + [
        f'bash -lc {shlex.quote(script.strip())}',
    ]
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return [], (err or out or f'ssh failed (rc={rc})').strip()
    snaps = []
    for chunk in (out or '').split('---PM---'):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        cwd = data.get('cwd') or ''
        if not cwd:
            continue
        phase_ts = data.get('phase_ts', 0)
        snaps.append(StatusSnapshot(
            event=data.get('event', ''),
            cwd=cwd,
            ts=data.get('ts', 0) or 0,
            session=data.get('session', '') or '',
            tool=data.get('tool'),
            state=data.get('state', 'done') or 'done',
            phase=data.get('phase'),
            phase_ts=(phase_ts if isinstance(phase_ts, (int, float)) else 0),
        ))
    return snaps, None


def discover_remote_binaries(
    profile: HostProfile, *, timeout: float = 10,
) -> dict[str, str]:
    """Return ``{harness_id: absolute_path}`` for claude/opencode/grok on the host.

    Non-interactive login shells often early-return from ``.bashrc`` before
    user PATH tweaks (e.g. ``~/.opencode/bin``), so ``command -v`` alone
    misses binaries that work in an interactive terminal. We also probe
    well-known install locations.
    """
    script = r'''
for c in claude opencode grok; do
  p=$(command -v "$c" 2>/dev/null || true)
  if [ -z "$p" ]; then
    for cand in \
      "$HOME/.opencode/bin/$c" \
      "$HOME/.local/bin/$c" \
      "$HOME/.npm-global/bin/$c" \
      "$HOME/bin/$c" \
      "/usr/local/bin/$c"; do
      if [ -x "$cand" ]; then p=$cand; break; fi
    done
  fi
  if [ -n "$p" ]; then echo "$c=$p"; fi
done
'''
    from ssh_transport import build_ssh_base_argv
    import shlex
    argv = build_ssh_base_argv(profile.ssh_target) + [
        f'bash -lc {shlex.quote(script.strip())}',
    ]
    rc, out, _err = run_ssh(argv, timeout=timeout)
    found = {}
    if rc != 0:
        return found
    for line in out.splitlines():
        line = line.strip()
        if '=' in line:
            k, v = line.split('=', 1)
            if k in ('claude', 'opencode', 'grok') and v:
                found[k] = v
    return found
