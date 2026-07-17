"""Remote project groups store — fetch/push ``project_groups.json`` over SSH.

Pure orchestration over ``ssh_transport`` (no GTK). Last-write-wins; no locking.
Missing remote file is not an error (returns empty forest).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from project_groups import (
    GroupForest,
    empty_forest,
    forest_to_dict,
    parse_forest,
)
from ssh_transport import (
    build_fetch_project_groups_argv,
    build_push_project_groups_argv,
    parse_fetch_groups_stdout,
    run_ssh,
)

if TYPE_CHECKING:
    from hosts import HostProfile


def fetch_project_groups(
    profile: 'HostProfile',
    *,
    timeout: float = 15,
) -> tuple[GroupForest, str | None, str]:
    """Return ``(forest, error_or_None, status)`` for *profile*.

    *status* is always set so the UI can gate writes:

    - SSH / remote non-zero rc → ``(empty, error, 'error')``
    - rc 0 + empty stdout → soft missing → ``(empty, None, 'missing')``
      (writable empty: remote has no file yet)
    - rc 0 + body, invalid JSON / oversized / wrong type →
      ``(empty, error, 'invalid')``
    - ok parse → ``(forest, None, 'ok')``
    """
    argv = build_fetch_project_groups_argv(profile.ssh_target)
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return (
            empty_forest(),
            (err or out or f'ssh failed (rc={rc})').strip(),
            'error',
        )
    data, perr = parse_fetch_groups_stdout(out)
    if perr is not None:
        # Invalid JSON / wrong type / oversized — do not pretend ok.
        return empty_forest(), perr, 'invalid'
    if data is None:
        return empty_forest(), None, 'missing'
    return parse_forest(data), None, 'ok'


def push_project_groups(
    profile: 'HostProfile',
    forest: GroupForest,
    *,
    timeout: float = 15,
) -> tuple[bool, str | None]:
    """Serialize *forest* and atomically write to the remote host.

    Returns ``(ok, error_or_None)``. Oversize payloads are rejected locally
    without opening SSH.
    """
    payload = json.dumps(forest_to_dict(forest), indent=2) + '\n'
    try:
        argv = build_push_project_groups_argv(profile.ssh_target, payload)
    except ValueError as e:
        return False, str(e) or 'too large'
    rc, out, err = run_ssh(argv, timeout=timeout)
    if rc != 0:
        return False, (err or out or f'ssh failed (rc={rc})').strip()
    return True, None
