"""claude-code-router (ccr) management.

ProjectMan makes Claude Code model-agnostic by pointing the spawned ``claude``
at a local ccr service (an Anthropic Messages API <-> arbitrary-provider
gateway) via environment variables. This module owns ccr's config file and
service lifecycle.

Pure module — no GTK. Every function degrades gracefully: if ccr is not
installed, all of them are no-ops and the caller falls back to native Claude.
Nothing here raises into the GTK main loop.
"""

import os
import json
import socket
import secrets
import tempfile
import subprocess

CCR_CONFIG_DIR = os.path.expanduser('~/.claude-code-router')
CCR_CONFIG_PATH = os.path.join(CCR_CONFIG_DIR, 'config.json')

_CMD_TIMEOUT = 10  # seconds for ccr start/stop/restart/status


def _log(settings, msg):
    if getattr(settings, 'debug_logging', False):
        print(f'ProjectMan[ccr]: {msg}')


def available(settings):
    """True if the ccr binary is on PATH (or the configured override exists)."""
    import shutil
    return shutil.which(settings.resolved_ccr_binary) is not None


def ensure_api_key(settings):
    """Mint a ccr API key into ``settings.ccr_api_key`` if blank; return it.

    The caller is responsible for persisting via ``settings.save()``. The same
    value is used as ccr's ``APIKEY`` and as the auth token injected into the
    ``claude`` child, so they must match.
    """
    if not settings.ccr_api_key:
        settings.ccr_api_key = secrets.token_urlsafe(32)
    return settings.ccr_api_key


def _router_target(settings):
    """Pick a 'provider,model' string for ccr's Router (its required default).

    Prefers the global default model; falls back to any custom per-project
    override; finally the first model in any provider.
    """
    candidates = [settings.model_default]
    candidates.extend(settings.model_overrides.values())
    for model in candidates:
        if model and '/' in model and model.split('/', 1)[0] in settings.providers:
            return model.replace('/', ',', 1)
    for pid, prov in settings.providers.items():
        models = prov.get('models') if isinstance(prov, dict) else None
        if isinstance(models, dict) and models:
            return f'{pid},{next(iter(models))}'
    return ''


def render_config(settings):
    """Translate ``settings.providers`` + selection into a ccr config dict."""
    providers = []
    for pid, prov in settings.providers.items():
        if not isinstance(prov, dict):
            continue
        models = prov.get('models')
        entry = {
            'name': pid,
            'api_base_url': prov.get('base_url', ''),
            'api_key': prov.get('api_key', ''),
            'models': list(models.keys()) if isinstance(models, dict) else [],
        }
        transformer = prov.get('transformer')
        if transformer:
            entry['transformer'] = {'use': [transformer]}
        providers.append(entry)

    target = _router_target(settings)
    router = {k: target for k in ('default', 'background', 'think', 'longContext')}
    return {
        'HOST': settings.ccr_host,
        'PORT': settings.ccr_port,
        'APIKEY': settings.ccr_api_key,
        'Providers': providers,
        'Router': router,
    }


def write_config(settings):
    """Atomically write ccr's config.json from settings. Return True on success.

    The file holds provider API keys in cleartext (ccr has no keyring), so it
    is written 0600 inside a 0700 directory.
    """
    try:
        os.makedirs(CCR_CONFIG_DIR, exist_ok=True)
        try:
            os.chmod(CCR_CONFIG_DIR, 0o700)
        except OSError:
            pass
        fd, tmp_path = tempfile.mkstemp(dir=CCR_CONFIG_DIR, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(render_config(settings), f, indent=2)
            os.replace(tmp_path, CCR_CONFIG_PATH)
            try:
                os.chmod(CCR_CONFIG_PATH, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        _log(settings, f'write_config failed: {e}')
        return False


def config_differs(settings):
    """True if the on-disk ccr config differs from what settings would render.

    Only the keys ProjectMan manages are compared, so runtime keys ccr may add
    on its own do not force needless restarts.
    """
    rendered = render_config(settings)
    try:
        with open(CCR_CONFIG_PATH, 'r') as f:
            on_disk = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True
    return any(on_disk.get(k) != v for k, v in rendered.items())


def _run(settings, *args):
    """Run a ccr subcommand; return True on exit code 0. Never raises."""
    try:
        result = subprocess.run(
            [settings.resolved_ccr_binary, *args],
            capture_output=True, text=True, timeout=_CMD_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        _log(settings, f'ccr {" ".join(args)} -> {result.returncode}')
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(settings, f'ccr {" ".join(args)} failed: {e}')
        return False


def start(settings):
    return _run(settings, 'start')


def stop(settings):
    return _run(settings, 'stop')


def restart(settings):
    return _run(settings, 'restart')


def is_running(settings):
    """True if the ccr service is accepting connections on host:port."""
    try:
        with socket.create_connection(
                (settings.ccr_host, settings.ccr_port), timeout=0.5):
            return True
    except OSError:
        return False


def sync(settings):
    """Reconcile the ccr service with current settings.

    No-op if ccr is not installed. Stops the service when no custom model is
    selected; otherwise writes a fresh config (if changed) and (re)starts.
    """
    if not available(settings):
        _log(settings, 'ccr not installed — skipping sync')
        return
    if not settings.any_custom_model_active():
        if is_running(settings):
            stop(settings)
        return
    ensure_api_key(settings)
    if config_differs(settings):
        write_config(settings)
        if is_running(settings):
            restart(settings)
            return
    if not is_running(settings):
        start(settings)
