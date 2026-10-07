"""Shared debug logging for ProjectMan.

Settings → Debug Logging (or ``--debug``) gates output. All session launches
should go through :func:`debug_log` so operators can see *what* started without
guessing from silent success paths.

Convention: ``print`` to stdout with a ``[DBG]`` prefix and flush, matching
the historical ``window._debug`` / ``terminal._debug`` helpers.
"""

from __future__ import annotations

import shlex


def debug_enabled(settings) -> bool:
    return bool(settings is not None and getattr(settings, 'debug_logging', False))


def debug_log(settings, msg: str) -> None:
    """Print ``[DBG] …`` when debug logging is on; no-op otherwise."""
    if debug_enabled(settings):
        print(f'[DBG] {msg}', flush=True)


def format_argv_for_debug(argv, *, max_arg: int = 96, max_total: int = 400) -> str:
    """Shell-ish argv string with long args truncated (prompts stay readable)."""
    if not argv:
        return '(empty argv)'
    parts = []
    for raw in argv:
        s = str(raw)
        if len(s) > max_arg:
            s = s[: max_arg - 1] + '…'
        parts.append(shlex.quote(s))
    out = ' '.join(parts)
    if len(out) > max_total:
        return out[: max_total - 1] + '…'
    return out
