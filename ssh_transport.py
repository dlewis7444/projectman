"""SSH transport helpers for remote ProjectMan hosts.

Pure / headless-testable: builds argv lists and parses stdout. No GTK.
Network I/O is confined to ``run_ssh`` (subprocess) so unit tests mock it.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from typing import Mapping, Sequence

from hosts import is_safe_project_name

# Home-relative path for virtual project groups (not under remote_projects_dir).
REMOTE_GROUPS_REL = '.ProjectMan/project_groups.json'
# Parent dir of REMOTE_GROUPS_REL under $HOME (DRY for mkdir / mktemp).
_REMOTE_GROUPS_PARENT = REMOTE_GROUPS_REL.rsplit('/', 1)[0]  # '.ProjectMan'
# Soft cap both directions (push rejects before SSH; fetch bounds with head).
REMOTE_GROUPS_MAX_BYTES = 1_000_000
_REMOTE_GROUPS_MAX_BYTES = REMOTE_GROUPS_MAX_BYTES  # alias for internal use


# ── Health ────────────────────────────────────────────────────────────────────

class HealthState:
    """Sidebar remote-section health colors (string constants)."""
    GREY = 'grey'      # checks disabled / unknown
    GREEN = 'green'    # SSH ok and projects dir ok
    YELLOW = 'yellow'  # SSH ok, projects dir missing or unreadable
    RED = 'red'        # SSH failed


def classify_health(
    ssh_ok: bool,
    projects_ok: bool,
    *,
    checks_enabled: bool,
) -> str:
    """Map health probe results to a HealthState color string.

    * checks disabled → grey
    * SSH fail → red
    * SSH ok, projects fail → yellow
    * both ok → green
    """
    if not checks_enabled:
        return HealthState.GREY
    if not ssh_ok:
        return HealthState.RED
    if not projects_ok:
        return HealthState.YELLOW
    return HealthState.GREEN


# ── SSH argv builders ─────────────────────────────────────────────────────────

def build_ssh_base_argv(
    ssh_target: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Local argv prefix: ``ssh [BatchMode] ConnectTimeout=N target``.

    Does **not** set ``StrictHostKeyChecking=no`` — host keys must already be
    accepted (or fail closed).
    """
    argv = ['ssh']
    if batch:
        argv.extend(['-o', 'BatchMode=yes'])
    argv.extend(['-o', f'ConnectTimeout={int(connect_timeout)}', ssh_target])
    return argv


def _remote_cd_snippet(remote_cwd: str) -> str:
    """Bash ``cd`` to *remote_cwd* with remote tilde expansion.

    Quoted ``'~/…'`` does not expand; rewrite like ``_remote_dir_assign``.
    """
    path = remote_cwd if remote_cwd is not None else ''
    if path == '~':
        return 'cd -- "$HOME"'
    if path.startswith('~/'):
        return f'cd -- "$HOME"{shlex.quote(path[1:])}'
    return f'cd -- {shlex.quote(path)}'


def build_remote_shell_command(
    remote_cwd: str,
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> str:
    """Remote command string for ``ssh target bash -lc <this>``.

    cd to *remote_cwd*, optionally export *env*, then ``exec`` *argv*.
    Values are shell-quoted via shlex. Tilde in *remote_cwd* expands on remote.

    Prepends common user bin dirs to PATH so bare harness names still work when
    ``.bashrc`` early-returns for non-interactive shells (opencode, etc.).
    """
    parts: list[str] = []
    # Interactive terminals load .bashrc (opencode in ~/.opencode/bin); non-
    # interactive login shells often ``return`` before that. Cover the common
    # install locations without requiring a full interactive profile.
    parts.append(
        'export PATH="$HOME/.kimi-code/bin:$HOME/.opencode/bin:$HOME/.grok/bin:'
        '$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/bin:$PATH"'
    )
    if env:
        for key, val in env.items():
            # Keys are expected to be shell identifiers; quote values only.
            parts.append(f'export {key}={shlex.quote(str(val))}')
    parts.append(_remote_cd_snippet(remote_cwd))
    # Grok hooks read GROK_WORKSPACE_ROOT for status cwd. Literal ``--cwd .`` is
    # unreliable (observed writes to $HOME → project dots stuck working/grey).
    # Always pin workspace to the absolute path after cd.
    parts.append('_pm_cwd="$(pwd)"')
    parts.append('export GROK_WORKSPACE_ROOT="$_pm_cwd"')
    argv_list = [str(a) for a in argv]
    bin0 = argv_list[0] if argv_list else ''
    is_grok = (
        bin0 == 'grok'
        or bin0.endswith('/grok')
        or os.path.basename(bin0) == 'grok'
    )
    if is_grok and argv_list:
        # Drop any prior --cwd and inject absolute path (shell-expanded).
        cleaned = [argv_list[0]]
        i = 1
        while i < len(argv_list):
            if argv_list[i] == '--cwd' and i + 1 < len(argv_list):
                i += 2
                continue
            cleaned.append(argv_list[i])
            i += 1
        rest = shlex.join(cleaned[1:]) if len(cleaned) > 1 else ''
        if rest:
            parts.append(
                f'exec {shlex.quote(cleaned[0])} --cwd "$_pm_cwd" {rest}'
            )
        else:
            parts.append(f'exec {shlex.quote(cleaned[0])} --cwd "$_pm_cwd"')
    else:
        parts.append(f'exec {shlex.join(argv_list)}')
    return ' && '.join(parts)


# Env keys safe to export into a remote SSH session. Never forward the local
# process environment wholesale — laptop HOME/USER/PATH break remote cd and
# harnesses (e.g. giskard@clawdbot seeing /home/user/…).
_REMOTE_ENV_EXACT = frozenset({
    'DISABLE_AUTOUPDATER',
    'TERM',
    'COLORTERM',
    'OLLAMA_HOST',
})
_REMOTE_ENV_PREFIXES = (
    'ANTHROPIC_',
    'CLAUDE_CODE_',
)


def filter_remote_export_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """Keep only harness/provider vars for remote ``export``; drop local HOME etc."""
    if not env:
        return None
    out: dict[str, str] = {}
    for key, val in env.items():
        if key in _REMOTE_ENV_EXACT or any(key.startswith(p) for p in _REMOTE_ENV_PREFIXES):
            out[key] = str(val)
    return out or None


def build_ssh_spawn_argv(
    ssh_target: str,
    remote_cwd: str,
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Full local argv to spawn an interactive remote session (VTE).

    ``ssh … -tt target bash -lc <remote_command>`` — ``-tt`` forces a TTY.
    """
    remote = build_remote_shell_command(
        remote_cwd, argv, filter_remote_export_env(env),
    )
    base = build_ssh_base_argv(
        ssh_target, batch=batch, connect_timeout=connect_timeout,
    )
    # base is [ssh, opts…, target]; insert -tt before target.
    # One remote command token (not bash / -lc / script as three argv) so
    # OpenSSH does not re-parse and empty $HOME — see Add Host remote-path bug.
    return base[:-1] + [
        '-tt', base[-1], f'bash -lc {shlex.quote(remote)}',
    ]


def _remote_dir_assign(remote_projects_dir: str) -> str:
    """Bash snippet setting ``dir=`` with remote tilde expansion.

    ``~/…`` becomes ``$HOME/…`` so expansion happens on the remote shell,
    not the local machine. Other paths are shlex-quoted.
    """
    path = remote_projects_dir if remote_projects_dir is not None else ''
    if path == '~':
        return 'dir="$HOME"'
    if path.startswith('~/'):
        # Single double-quoted expansion: dir="$HOME/.ProjectMan/projects"
        # (rest is path[1:], which starts with /). Avoid split
        # dir="$HOME"'/…' forms that multi-arg ssh mangled historically.
        rest = path[1:]
        if any(c in rest for c in '`"$\n\\'):
            # Fall back to quoted concatenation for odd paths.
            return f'dir="$HOME"{shlex.quote(rest)}'
        return f'dir="$HOME{rest}"'
    return f'dir={shlex.quote(path)}'


def _bash_lc_argv(ssh_target: str, remote_script: str, **base_kw) -> list[str]:
    """``ssh … target 'bash -lc <quoted-script>'`` (batch health / fs ops).

    The remote command must be **one** argv token after the target. Passing
    ``bash``, ``-lc``, and the script as three tokens makes OpenSSH join them
    for the user shell in a way that empties ``$HOME`` inside the script
    (reproduced when remote $HOME collapsed: ``mkdir: cannot create directory ''``).
    """
    return build_ssh_base_argv(ssh_target, **base_kw) + [
        f'bash -lc {shlex.quote(remote_script)}',
    ]


def build_list_projects_argv(
    ssh_target: str,
    remote_projects_dir: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """List entries under the remote projects dir (create dir if missing).

    Remote: assign dir (tilde-expand), ``mkdir -p``, ``ls -1A``.
    Callers filter via ``parse_ls_project_names``.
    """
    assign = _remote_dir_assign(remote_projects_dir)
    script = f'{assign}; mkdir -p -- "$dir"; ls -1A -- "$dir"'
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


def build_ensure_projects_dir_argv(
    ssh_target: str,
    remote_projects_dir: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Ensure the remote projects directory exists (``mkdir -p``)."""
    assign = _remote_dir_assign(remote_projects_dir)
    script = f'{assign}; mkdir -p -- "$dir"'
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


def build_mkdir_project_argv(
    ssh_target: str,
    remote_projects_dir: str,
    name: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Create a project directory under the remote projects dir.

    Raises ``ValueError`` if *name* fails ``hosts.is_safe_project_name``.
    """
    if not is_safe_project_name(name):
        raise ValueError(f'unsafe project name: {name!r}')
    assign = _remote_dir_assign(remote_projects_dir)
    # Single safe segment under $dir only — never a free-form path.
    script = (
        f'{assign}; mkdir -p -- "$dir"; '
        f'mkdir -- "$dir"/{shlex.quote(name)}'
    )
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


def build_rename_project_argv(
    ssh_target: str,
    remote_projects_dir: str,
    old_name: str,
    new_name: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Rename a project directory on the remote host.

    Raises ``ValueError`` if either name is unsafe.
    """
    if not is_safe_project_name(old_name):
        raise ValueError(f'unsafe project name: {old_name!r}')
    if not is_safe_project_name(new_name):
        raise ValueError(f'unsafe project name: {new_name!r}')
    assign = _remote_dir_assign(remote_projects_dir)
    script = (
        f'{assign}; '
        f'mv -- "$dir"/{shlex.quote(old_name)} "$dir"/{shlex.quote(new_name)}'
    )
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


def build_rmdir_project_argv(
    ssh_target: str,
    remote_projects_dir: str,
    name: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Remove a project directory on the remote host (``rm -rf``).

    Only *name* is used as a single path segment under the projects dir;
    unsafe names raise ``ValueError``. Does not allow ``..`` / slashes / dots.
    """
    if not is_safe_project_name(name):
        raise ValueError(f'unsafe project name: {name!r}')
    assign = _remote_dir_assign(remote_projects_dir)
    # -rf on a quoted single segment under $dir only — never a free-form path.
    script = (
        f'{assign}; '
        f'rm -rf -- "$dir"/{shlex.quote(name)}'
    )
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


def build_fetch_project_groups_argv(
    ssh_target: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Fetch remote ``~/.ProjectMan/project_groups.json`` (if present).

    Missing file → exit 0 with empty stdout (not an error). Present file is
    emitted via ``head -c (max+1)`` so oversize is detectable after transfer
    without reading unbounded remote content. cat/head failure → non-zero rc
    (no trailing unconditional ``exit 0``). Remote path is home-relative
    (``REMOTE_GROUPS_REL``), not under ``remote_projects_dir``.
    """
    # Expand $HOME on the remote; never rely on local tilde.
    # head -c max+1: if result is max+1 bytes, parse_fetch rejects as too large.
    max_plus = _REMOTE_GROUPS_MAX_BYTES + 1
    script = (
        f'f="$HOME/{REMOTE_GROUPS_REL}"; '
        'if [ ! -f "$f" ]; then exit 0; fi; '
        f'head -c {max_plus} -- "$f"'
    )
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


def build_push_project_groups_argv(
    ssh_target: str,
    json_text: str,
    *,
    batch: bool = True,
    connect_timeout: int = 5,
) -> list[str]:
    """Atomically write *json_text* to remote ``project_groups.json``.

    Payload is base64-encoded in the local builder and decoded on the remote
    so raw JSON never enters the remote shell unquoted. Write order:
    ``mkdir -p`` → ``mktemp`` under parent of ``REMOTE_GROUPS_REL`` →
    decode → ``mv -f`` into place.

    Raises:
        ValueError: if UTF-8 byte length of *json_text* exceeds
            ``REMOTE_GROUPS_MAX_BYTES`` (caller should not open SSH).
    """
    if not isinstance(json_text, str):
        json_text = str(json_text)
    raw = json_text.encode('utf-8')
    if len(raw) > _REMOTE_GROUPS_MAX_BYTES:
        raise ValueError('too large')
    b64 = base64.b64encode(raw).decode('ascii')
    # base64 alphabet is shell-safe; still quote for hygiene.
    b64_q = shlex.quote(b64)
    parent = _REMOTE_GROUPS_PARENT
    # mktemp XXXXXX template; cleanup temp on decode failure; mv is atomic.
    script = (
        f'mkdir -p -- "$HOME/{parent}"; '
        f'tmp=$(mktemp "$HOME/{parent}/project_groups.json.tmp.XXXXXX") || exit 1; '
        f'echo {b64_q} | base64 -d > "$tmp" || {{ rm -f "$tmp"; exit 1; }}; '
        f'mv -f -- "$tmp" "$HOME/{REMOTE_GROUPS_REL}"'
    )
    return _bash_lc_argv(
        ssh_target, script, batch=batch, connect_timeout=connect_timeout,
    )


# ── Parse helpers ─────────────────────────────────────────────────────────────

def parse_fetch_groups_stdout(
    stdout: str,
    *,
    max_bytes: int = _REMOTE_GROUPS_MAX_BYTES,
) -> tuple[dict | None, str | None]:
    """Parse fetch stdout into ``(data_dict_or_None, error_or_None)``.

    - empty / whitespace → ``(None, None)`` (missing file → empty forest)
    - body larger than *max_bytes* → ``(None, 'too large')``
    - invalid JSON → ``(None, 'invalid json: …')``
    - valid JSON non-object → ``(None, 'invalid top-level type')``
    - valid JSON object → ``(dict, None)``
    """
    if stdout is None:
        stdout = ''
    # Size cap on the raw body (UTF-8 byte length).
    try:
        nbytes = len(stdout.encode('utf-8'))
    except Exception:
        nbytes = len(stdout)
    if nbytes > max_bytes:
        return None, 'too large'
    if not stdout.strip():
        return None, None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return None, f'invalid json: {e}'
    if not isinstance(data, dict):
        return None, 'invalid top-level type'
    return data, None


def parse_ls_project_names(stdout: str) -> list[str]:
    """Parse ``ls -1A`` stdout into project names.

    Skips empty lines, ``.``, ``..``, and names starting with ``.``.
    """
    if not stdout:
        return []
    names: list[str] = []
    for line in stdout.splitlines():
        name = line.strip('\r')
        # ls -1A one name per line; keep interior spaces, drop pure empty.
        if not name or name in ('.', '..') or name.startswith('.'):
            continue
        names.append(name)
    return names


# ── Run ───────────────────────────────────────────────────────────────────────

def run_ssh(argv: Sequence[str], timeout: float = 10) -> tuple[int, str, str]:
    """Run an SSH argv via ``subprocess.run``; return ``(rc, stdout, stderr)``.

    On timeout returns rc 124. On OSError (e.g. ssh missing) returns 127.
    Safe to mock in tests — pure builders do not call this.
    """
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or '', proc.stderr or ''
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ''
        err = exc.stderr or ''
        if isinstance(out, bytes):
            out = out.decode('utf-8', errors='replace')
        if isinstance(err, bytes):
            err = err.decode('utf-8', errors='replace')
        if not err:
            err = f'timeout after {timeout}s'
        return 124, out, err
    except OSError as exc:
        return 127, '', str(exc)
