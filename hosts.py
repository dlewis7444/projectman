"""Host axis — ProjectMan multi-host identity and profiles.

Projects live on a host (localhost or a configured SSH remote). This module is
pure (no GTK, no network) so it is unit-testable headless. SSH I/O lives in
``ssh_transport.py``; UI in sidebar/settings_window.

Project identity for settings maps and session restore is a *project ref*
string (see ``encode_project_ref`` / ``decode_project_ref``). Legacy bare
filesystem paths are treated as localhost.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


LOCALHOST_ID = 'localhost'

# project_ref forms:
#   local:<abspath>           — localhost project (path is absolute)
#   ssh:<host_id>:<name>      — remote project (name is the projects/ dir entry)
#   bare path (legacy)        — dual-read as local:<path>
_REF_LOCAL_PREFIX = 'local:'
_REF_SSH_PREFIX = 'ssh:'


@dataclass
class BinarySpec:
    """How to find a harness binary on a host."""
    use_path: bool = True
    override: str = ''

    def resolved(self, path_fallback: str = '') -> str:
        """Return the binary to exec: override if not use_path, else PATH name."""
        if not self.use_path:
            o = (self.override or '').strip()
            if o:
                return o
        return (path_fallback or '').strip() or 'claude'


@dataclass
class HostProfile:
    """A configured execution host (remotes only; localhost is synthetic)."""
    id: str
    ssh_target: str
    display_name: str = ''
    remote_projects_dir: str = '~/.ProjectMan/projects'
    binaries: dict = field(default_factory=dict)
    # Opt-in: install/poll status hooks on this host (default off).
    rich_status_opt_in: bool = False

    def title(self) -> str:
        """Sidebar / settings display string."""
        name = (self.display_name or '').strip()
        return name if name else (self.ssh_target or self.id)

    def binary_spec(self, harness_id: str) -> BinarySpec:
        raw = self.binaries.get(harness_id) if isinstance(self.binaries, dict) else None
        if not isinstance(raw, dict):
            return BinarySpec()
        return BinarySpec(
            use_path=bool(raw.get('use_path', True)),
            override=str(raw.get('override') or ''),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Any) -> HostProfile | None:
        if not isinstance(data, dict):
            return None
        hid = data.get('id')
        target = data.get('ssh_target')
        if not isinstance(hid, str) or not hid or hid == LOCALHOST_ID:
            return None
        if not isinstance(target, str) or not target.strip():
            return None
        binaries = data.get('binaries')
        if not isinstance(binaries, dict):
            binaries = {}
        return cls(
            id=hid,
            ssh_target=target.strip(),
            display_name=str(data.get('display_name') or ''),
            remote_projects_dir=str(
                data.get('remote_projects_dir') or '~/.ProjectMan/projects'
            ),
            binaries=binaries,
            rich_status_opt_in=bool(data.get('rich_status_opt_in', False)),
        )


def new_host_id() -> str:
    return uuid.uuid4().hex[:12]


def encode_project_ref(host_id: str, path_or_name: str) -> str:
    """Encode a stable settings/session key for a project on a host.

    localhost: ``local:<absolute-or-given-path>``
    remote:    ``ssh:<host_id>:<project_name>``
    """
    if not host_id or host_id == LOCALHOST_ID:
        return f'{_REF_LOCAL_PREFIX}{path_or_name}'
    return f'{_REF_SSH_PREFIX}{host_id}:{path_or_name}'


def decode_project_ref(ref: str) -> tuple[str, str]:
    """Return ``(host_id, path_or_name)``.

    Bare paths (no prefix) → localhost. Malformed ssh refs fall back to
    treating the whole string as a localhost path (legacy safety).
    """
    if not isinstance(ref, str) or not ref:
        return LOCALHOST_ID, ref or ''
    if ref.startswith(_REF_LOCAL_PREFIX):
        return LOCALHOST_ID, ref[len(_REF_LOCAL_PREFIX):]
    if ref.startswith(_REF_SSH_PREFIX):
        rest = ref[len(_REF_SSH_PREFIX):]
        # host_id is hex-ish / slug without colons ideally; name may not contain
        # the first colon separator only — split once.
        if ':' in rest:
            hid, name = rest.split(':', 1)
            if hid and name:
                return hid, name
        return LOCALHOST_ID, ref
    # Legacy bare path
    return LOCALHOST_ID, ref


def normalize_override_key(key: str) -> str:
    """Map a settings map key to canonical project_ref form.

    Bare paths become ``local:<path>``. Already-encoded refs pass through.
    """
    if not isinstance(key, str) or not key:
        return key
    if key.startswith(_REF_LOCAL_PREFIX) or key.startswith(_REF_SSH_PREFIX):
        return key
    return encode_project_ref(LOCALHOST_ID, key)


def migrate_override_map(raw: Any) -> dict:
    """Normalize a ``{project_key: value}`` map to project_ref keys.

    Last write wins on collision after normalize (shouldn't happen for legacy
    bare paths). Non-dict → empty dict.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        out[normalize_override_key(k)] = v
    return out


def resolve_override_identity(project_path: str, host_id: str = LOCALHOST_ID):
    """Normalize a project identity for override maps.

    Accepts a bare local path, ``local:…``, or ``ssh:<host>:<name>``.
    Returns ``(host_id, path_or_name)`` where path_or_name is the local
    absolute path or the remote project *name* (never a double-encoded ref).
    """
    if not isinstance(project_path, str) or not project_path:
        return (host_id or LOCALHOST_ID), project_path or ''
    if project_path.startswith(_REF_LOCAL_PREFIX) or project_path.startswith(
            _REF_SSH_PREFIX):
        return decode_project_ref(project_path)
    return (host_id or LOCALHOST_ID), project_path


def override_key(project_path: str, host_id: str = LOCALHOST_ID) -> str:
    """Canonical settings-map key for a project."""
    hid, key = resolve_override_identity(project_path, host_id)
    return encode_project_ref(hid, key)


def lookup_override(overrides: dict | None, host_id: str, path_or_name: str,
                    legacy_path: str | None = None):
    """Look up an override trying namespaced key then legacy bare path.

    ``path_or_name`` may be a local absolute path, a remote project name, or a
    full project_ref (``ssh:…`` / ``local:…``). Double-encoding is avoided via
    ``resolve_override_identity``.
    """
    if not isinstance(overrides, dict):
        return None, False
    # Exact key (window often stores project.path which is already a ref)
    if path_or_name in overrides:
        return overrides[path_or_name], True
    hid, key = resolve_override_identity(path_or_name, host_id)
    ref = encode_project_ref(hid, key)
    if ref in overrides:
        return overrides[ref], True
    # Legacy bare path (localhost only)
    if hid == LOCALHOST_ID or not hid:
        if key in overrides:
            return overrides[key], True
        if legacy_path and legacy_path in overrides:
            return overrides[legacy_path], True
    return None, False


def parse_hosts_map(raw: Any) -> dict[str, HostProfile]:
    """Parse settings.hosts into ``{id: HostProfile}``; skip invalid entries."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, val in raw.items():
        # Accept either id-keyed map of dicts, or embed id in value.
        if isinstance(val, dict):
            data = dict(val)
            if 'id' not in data and isinstance(key, str):
                data['id'] = key
            prof = HostProfile.from_dict(data)
            if prof is not None:
                out[prof.id] = prof
    return out


def hosts_to_settings_dict(hosts: dict[str, HostProfile]) -> dict:
    """Serialize host profiles for settings.json."""
    return {hid: p.to_dict() for hid, p in hosts.items() if hid != LOCALHOST_ID}


_SAFE_NAME_RE = re.compile(r'^[^/\0]+$')


def is_safe_project_name(name: str) -> bool:
    """True if name is usable as a single path segment (no slash/null)."""
    return bool(name and isinstance(name, str) and _SAFE_NAME_RE.match(name)
                and name not in ('.', '..') and not name.startswith('.'))
