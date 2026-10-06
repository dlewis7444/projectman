"""Provider/model helpers shared by the settings UI, the sidebar, and the
spawn path.

The provider definitions live in ``Settings.providers`` as::

    {provider_id: {"name": str,            # display label
                   "base_url": str,        # Anthropic-compatible API base URL
                   "api_key": str,          # cleartext key (see settings.py)
                   "models": [str, ...],   # free-text model ids; trailing
                                           # ``[1m]`` is a per-model 1M flag
                                           # (CC strips it before the API call)
                   "max_context_tokens": int  # optional; injects
                                           # CLAUDE_CODE_MAX_CONTEXT_TOKENS
                   }}

A provider is identified by its ``provider_id``; the empty string ``''`` is the
sentinel for "Anthropic (native)" — no env injection, CC uses its own creds.
Custom-provider context controls (1M model suffix, max_context_tokens) never
apply to native Anthropic.

Claude Code tiers (Opus/Sonnet/Haiku/Subagent/Fable) are each pinnable to any
model id on the ACTIVE custom provider. CC's ``ANTHROPIC_BASE_URL`` is
process-wide, so one session can mix model NAMES across tiers but never
providers — every tier must be reachable from the active provider's endpoint.
``build_spawn_env`` resolves the tiers and injects the ollama-style env dict
at spawn for custom providers only.
"""

import os
import re
import json
import urllib.request
import urllib.parse

NATIVE_LABEL = 'Anthropic (native)'
GROK_NATIVE_LABEL = 'Grok (native)'
OPENCODE_NATIVE_LABEL = 'OpenCode (native)'
KIMI_NATIVE_LABEL = 'Kimi (native)'

# Claude Code treats a trailing ``[1m]`` on a model id as a 1M-token context
# window (modelMax = 1_000_000) and strips the suffix before the API call.
# The provider editor's per-model 1M toggle encodes that flag in the stored id.
_1M_SUFFIX = '[1m]'

# Sentinel used by the per-project provider menu to mean "follow the global
# default" (i.e. remove any override). Safe because a real provider id is a
# non-empty dict key and never this string.
FOLLOW_DEFAULT = '__default__'

# Harness-native "provider" sentinels for the projects Provider submenu.
# Distinct from real provider ids and from FOLLOW_DEFAULT.
NATIVE_GROK = '__native_grok__'
NATIVE_OPENCODE = '__native_opencode__'
NATIVE_KIMI = '__native_kimi__'

# The label shown for a tier's "use the provider's default model" entry.
TIER_DEFAULT_LABEL = 'Default'


def _provider_models(providers, pid):
    """The active provider's model list (strings), or ``[]`` if unknown."""
    if not isinstance(providers, dict) or not pid:
        return []
    prov = providers.get(pid)
    if not isinstance(prov, dict):
        return []
    models = prov.get('models')
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, str)]


def build_provider_options(providers):
    """Return ``(ids, labels)`` parallel lists for a provider picker.

    Index 0 is always the native-Anthropic sentinel (id ``''``). Remaining
    entries are the provider ids (sorted for stable ordering) with their
    display names. A malformed ``providers`` value degrades to just the native
    entry rather than raising.
    """
    ids = ['']
    labels = [NATIVE_LABEL]
    if not isinstance(providers, dict):
        return ids, labels
    for pid in sorted(providers):
        prov = providers.get(pid)
        if not isinstance(prov, dict):
            continue
        ids.append(pid)
        labels.append(prov.get('name') or pid)
    return ids, labels


def provider_label(providers, pid):
    """Human-readable label for a provider id.

    Returns the native label for ``''`` and falls back to the raw id when the
    provider is unknown (e.g. a stale per-project override naming a deleted
    provider).
    """
    if not pid:
        return NATIVE_LABEL
    if pid == NATIVE_GROK:
        return GROK_NATIVE_LABEL
    if pid == NATIVE_OPENCODE:
        return OPENCODE_NATIVE_LABEL
    if pid == NATIVE_KIMI:
        return KIMI_NATIVE_LABEL
    if not isinstance(providers, dict):
        return pid
    prov = providers.get(pid)
    if not isinstance(prov, dict):
        return pid
    return prov.get('name') or pid


def build_provider_menu_entries(settings, harness_id):
    """Entries for the projects-tab Provider submenu.

    One native option for the effective harness only (Gio.Menu cannot grey
    items, so unselectable choices are omitted rather than shown disabled):

      * Claude  → Anthropic (native) + every custom Settings provider
      * Grok    → Grok (native) + every custom Settings provider (env-only)
      * OpenCode → OpenCode (native) only
      * Kimi    → Kimi (native) only

    Returns ``[(id, label, selectable)]`` — ``selectable`` is always True for
    listed entries. Pure + defensive — never raises on bad settings shapes.
    """
    hid = harness_id or 'claude'
    if hid == 'opencode':
        return [(NATIVE_OPENCODE, OPENCODE_NATIVE_LABEL, True)]
    if hid == 'kimi':
        return [(NATIVE_KIMI, KIMI_NATIVE_LABEL, True)]
    if hid == 'grok':
        entries = [(NATIVE_GROK, GROK_NATIVE_LABEL, True)]
    else:
        # Claude (or unknown): Anthropic native + customs.
        entries = [('', NATIVE_LABEL, True)]
    # Claude and Grok both honor the custom provider catalog (grok rides it
    # env-only via GROK_MODELS_BASE_URL); OpenCode/Kimi stay native-only.
    providers = getattr(settings, 'providers', None)
    if isinstance(providers, dict):
        for pid in sorted(providers):
            prov = providers.get(pid)
            if not isinstance(prov, dict):
                continue
            label = prov.get('name') or pid
            entries.append((pid, label, True))
    return entries


def provider_menu_current(settings, project_path='', harness_id=None):
    """Concrete radio target for the Provider submenu (never FOLLOW_DEFAULT).

    Claude / Grok → ``effective_provider`` under that row's harness (per-harness
    default or per-project pin), native sentinel when native. OpenCode / Kimi
    → their native sentinel (model owned by the harness).
    """
    if harness_id is None:
        harness_id = getattr(settings, 'effective_harness', lambda p: 'claude')(
            project_path)
    if harness_id == 'grok':
        try:
            return settings.effective_provider(project_path, 'grok') or NATIVE_GROK
        except Exception:
            return NATIVE_GROK
    if harness_id == 'opencode':
        return NATIVE_OPENCODE
    if harness_id == 'kimi':
        return NATIVE_KIMI
    try:
        return settings.effective_provider(project_path, 'claude') or ''
    except Exception:
        return ''


def validate_providers(parsed):
    """Validate a parsed providers dict; raise ``ValueError`` on a bad shape.

    Lenient by design — only rejects shapes that would break the picker or the
    spawn env. Partially-filled providers (missing base_url or api_key) are
    allowed so a user can save work in progress.
    """
    if not isinstance(parsed, dict):
        raise ValueError('top level must be a JSON object of providers')
    for pid, prov in parsed.items():
        if not isinstance(prov, dict):
            raise ValueError(f'provider "{pid}" must be an object')
        models = prov.get('models', [])
        if not isinstance(models, list):
            raise ValueError(f'provider "{pid}": "models" must be a list')
        for mid in models:
            if not isinstance(mid, str):
                raise ValueError(
                    f'provider "{pid}": model ids must be strings')
        search_url = prov.get('search_url', '')
        if isinstance(search_url, str) and search_url.strip():
            if not (search_url.startswith('http://')
                    or search_url.startswith('https://')):
                raise ValueError(
                    f'provider "{pid}": search_url must be http(s)')
    return parsed


def build_tier_options(providers, pid):
    """Return ``(ids, labels)`` for a tier assignment combo.

    Index 0 is the "Default" sentinel (id ``''`` = use the provider's first
    model); remaining entries are the active provider's model ids. When the
    provider has no models the combo is just the Default entry.
    """
    ids = ['']
    labels = [TIER_DEFAULT_LABEL]
    for mid in _provider_models(providers, pid):
        ids.append(mid)
        labels.append(mid)
    return ids, labels


def is_1m_model_id(mid):
    """True when ``mid`` carries the Claude Code 1M-context suffix."""
    return isinstance(mid, str) and mid.endswith(_1M_SUFFIX)


def without_1m_suffix(mid):
    """Return ``mid`` with a trailing ``[1m]`` stripped, if present."""
    if is_1m_model_id(mid):
        return mid[:-len(_1M_SUFFIX)]
    return mid


def with_1m_suffix(mid):
    """Return ``mid`` with a trailing ``[1m]`` ensured (no double-append)."""
    if not mid or not isinstance(mid, str):
        return mid
    if mid.endswith(_1M_SUFFIX):
        return mid
    return f'{mid}{_1M_SUFFIX}'


def normalize_model_id(mid):
    """Strip a trailing ``[1m]`` so a probe membership check doesn't
    false-negative on 1M-flagged model ids (CC strips ``[1m]`` itself before
    the API call, so the provider's endpoint never lists the suffixed form)."""
    return without_1m_suffix(mid)


def list_provider_models(provider):
    """Probe a provider's endpoint for the models it offers, or ``None`` on
    failure.

    Advisory-only reachability check used by the provider editor's per-model
    indicator. Tries an Anthropic-compatible ``<base_url>/v1/models`` first
    (``x-api-key`` + ``anthropic-version`` headers, parse ``data[].id``), then
    Ollama's ``<base_url>/api/tags`` (parse ``models[].name``). Returned ids are
    ``normalize_model_id``-stripped. 4s timeout.

    Returns a set of normalized model ids, or ``None`` if the provider shape is
    bad or neither endpoint responds. Callers MUST keep the model regardless —
    false negatives (id mismatch, tags, ``[1m]``) are expected, so this never
    gates an add.
    """
    if not isinstance(provider, dict):
        return None
    base = (provider.get('base_url') or '').rstrip('/')
    if not base:
        return None
    key = provider.get('api_key') or ''
    timeout = 4

    def _get(url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8', 'replace'))

    # Anthropic-compatible /v1/models.
    try:
        payload = _get(f'{base}/v1/models',
                       {'x-api-key': key, 'anthropic-version': '2023-06-01'})
        data = payload.get('data') if isinstance(payload, dict) else None
        if isinstance(data, list):
            ids = {normalize_model_id(item['id']) for item in data
                   if isinstance(item, dict) and isinstance(item.get('id'), str)}
            if ids:
                return ids
    except Exception:
        pass

    # Ollama /api/tags.
    try:
        payload = _get(f'{base}/api/tags')
        models = payload.get('models') if isinstance(payload, dict) else None
        if isinstance(models, list):
            ids = {normalize_model_id(item['name']) for item in models
                   if isinstance(item, dict) and isinstance(item.get('name'), str)}
            if ids:
                return ids
    except Exception:
        pass

    return None


def resolve_tier_model(settings, pid, tier):
    """Resolve the model id to pin for a tier on the active provider.

    ``tier_models[pid][tier]`` wins when it names a model on the active
    provider; otherwise the provider's first model is used; if the provider
    has no models the empty string is returned (the spawn env still sets the
    var, CC will fall back to its own default). Covers a stale tier value left
    over from a per-project override to a provider that lacks the named model.
    ``tier_models`` is per-provider (``{pid: {tier: model_id}}``); a missing
    pid entry is treated as all-default.
    """
    models = _provider_models(settings.providers, pid)
    val = ''
    tm = getattr(settings, 'tier_models', None)
    if isinstance(tm, dict):
        sub = tm.get(pid)
        if isinstance(sub, dict):
            v = sub.get(tier, '')
            if isinstance(v, str):
                val = v
    if val and val in models:
        return val
    return models[0] if models else ''


def _explicit_tier_model(settings, pid, tier):
    """The explicitly-chosen model id for ``tier`` if it is still on the active
    provider's model list; else ``''``.

    Reads ``tier_models[pid][tier]`` (per-provider). Unlike
    :func:`resolve_tier_model`, this does NOT fall back to the provider's first
    model — it returns ``''`` when the tier is unset (or its value is stale).
    Used to decide whether to *force* a tier's env var (the subagent) vs leave
    it unset so a per-call ``model:"sonnet"`` can route image subagents through
    the Sonnet tier and default subagents fall to CC's global default. See the
    no-forced-subagent policy (the maintainer 2026-06-17).
    """
    models = _provider_models(settings.providers, pid)
    tm = getattr(settings, 'tier_models', None)
    if isinstance(tm, dict):
        sub = tm.get(pid)
        if isinstance(sub, dict):
            v = sub.get(tier, '')
            if isinstance(v, str) and v and v in models:
                return v
    return ''


def _format_classifier_temperature(value):
    """Render a finite temperature value as the string CC expects.

    ``CLAUDE_CODE_AUTO_MODE_TEMPERATURE`` is parsed by CC via ``Number()``,
    so a plain JSON-style float string is sufficient. Values like 0.0 or 1
    are emitted verbatim.
    """
    return str(float(value))


def _provider_max_context_tokens(prov):
    """Positive int max_context_tokens from a provider dict, or ``None`` if
    unset/invalid. Custom providers only; native has no provider dict field."""
    if not isinstance(prov, dict):
        return None
    v = prov.get('max_context_tokens')
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, float) and v > 0 and v == int(v):
        return int(v)
    if isinstance(v, str):
        text = v.strip()
        if text.isdigit():
            n = int(text)
            if n > 0:
                return n
    return None


def build_spawn_env(settings, project_path):
    """Build the env override for a spawn, or report a native fallback.

    Returns ``(env_dict, None)`` for a custom provider (the ollama-style env
    dict, incl. the resolved Opus/Sonnet/Haiku/Fable tier models +
    ``DISABLE_AUTOUPDATER=1``); ``(None, None)`` for native (no injection — CC
    uses its own creds); or ``(None, reason)`` when a custom provider was
    requested but is unusable (missing or no base_url), so the spawn falls back
    to native and the UI surfaces ``reason`` via the provider-unavailable toast.

    Tier model ids are emitted **verbatim** (including any trailing ``[1m]``
    the user set via the per-model 1M toggle). No name-based rewrite at spawn.
    Anthropic native never receives these vars.

    ``CLAUDE_CODE_SUBAGENT_MODEL`` is **opt-in**: emitted only when the user
    explicitly assigned a model to the Subagent tier (e.g. a vision-capable
    model); otherwise it is omitted so per-call ``model:"sonnet"`` routes image
    subagents through the Sonnet tier and default subagents fall to CC's global
    default. Never force a vision-less model on subagents (nested subagent loops).

    Optional provider ``max_context_tokens`` injects
    ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``; when unset the var is scrubbed from the
    inherited parent env.
    """
    pid = settings.effective_provider(project_path, 'claude')
    if not pid:
        return (None, None)
    prov = settings.providers.get(pid) if isinstance(settings.providers, dict) else None
    if not isinstance(prov, dict) or not prov.get('base_url'):
        name = prov.get('name') or pid if isinstance(prov, dict) else pid
        if not isinstance(prov, dict):
            reason = f"provider '{pid}' is not configured"
        else:
            reason = f"provider '{name}' has no base_url"
        return (None, reason)
    base_url = prov.get('base_url', '')
    api_key = prov.get('api_key', '') or ''
    env = dict(os.environ)
    env['ANTHROPIC_BASE_URL'] = base_url
    env['ANTHROPIC_AUTH_TOKEN'] = api_key
    env['ANTHROPIC_API_KEY'] = ''   # empty — the anti-3rd-party-block shape
    # Verbatim tier ids (1M flag is stored on the model id by the UI toggle).
    env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = resolve_tier_model(settings, pid, 'opus')
    env['ANTHROPIC_DEFAULT_SONNET_MODEL'] = resolve_tier_model(settings, pid, 'sonnet')
    env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] = resolve_tier_model(settings, pid, 'haiku')
    # Fable tier: wired like the others; CC honors ANTHROPIC_DEFAULT_FABLE_MODEL
    # (Fable re-launched 2026-07).
    env['ANTHROPIC_DEFAULT_FABLE_MODEL'] = resolve_tier_model(settings, pid, 'fable')
    # Subagent is opt-in force: emit only when the user explicitly assigned a
    # model to the Subagent tier (e.g. a vision-capable model). Otherwise omit —
    # no forced subagent — so a per-call model:"sonnet" routes image subagents
    # through the Sonnet tier above and default subagents fall to CC's global
    # default. Never force a vision-less model here (nested subagent loops).
    subagent = _explicit_tier_model(settings, pid, 'subagent')
    if subagent:
        env['CLAUDE_CODE_SUBAGENT_MODEL'] = subagent
    else:
        # No forced subagent: scrub any value inherited from the parent env
        # (e.g. a launcher that set CLAUDE_CODE_SUBAGENT_MODEL) so the spawned
        # session doesn't inherit a stale forced-subagent model.
        env.pop('CLAUDE_CODE_SUBAGENT_MODEL', None)

    # Classifier temperature — per-provider, opt-in. Omit when unset so CC
    # falls back to its own default. The only live classifier lever in CC
    # v2.1.190+; the other classifier env vars are registered but inert.
    ct = getattr(settings, 'classifier_temperature', None)
    if isinstance(ct, dict) and pid in ct:
        env['CLAUDE_CODE_AUTO_MODE_TEMPERATURE'] = _format_classifier_temperature(
            ct[pid])
    else:
        env.pop('CLAUDE_CODE_AUTO_MODE_TEMPERATURE', None)

    # Provider max context tokens — opt-in. Scrub when unset so a parent
    # launcher cannot leak a stale CLAUDE_CODE_MAX_CONTEXT_TOKENS.
    max_ctx = _provider_max_context_tokens(prov)
    if max_ctx is not None:
        env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] = str(max_ctx)
    else:
        env.pop('CLAUDE_CODE_MAX_CONTEXT_TOKENS', None)

    env['CLAUDE_CODE_ATTRIBUTION_HEADER'] = '0'
    env['OLLAMA_HOST'] = base_url
    env['DISABLE_AUTOUPDATER'] = '1'
    return (env, None)


# ---------------------------------------------------------------------------
# Grok Build custom providers (env-only, 2026-10-05).
# ---------------------------------------------------------------------------

# Catalog base_urls are Anthropic-flavored; grok consumes OpenAI-compatible
# endpoints. A path already carrying a version segment (…/v1, …/v2beta — pure
# digits plus an optional alpha suffix, case-insensitive) is left alone;
# anything else gets /v1 appended to the PATH component (never after a query
# string or fragment).
_GROK_VERSIONED_PATH_RE = re.compile(r'/v\d+[a-z]*(?=/|$)', re.IGNORECASE)


def _grok_base_url(base_url):
    """Derive grok's OpenAI-compatible base URL from a catalog base_url.

    Strip trailing ``/``; append ``/v1`` to the path unless the path already
    carries a ``/v<digits>`` segment (``/v1``, ``/v2beta`` — alpha suffix and
    case tolerated). Query and fragment are preserved; the version segment is
    never appended after them. Verified live 2026-10-05: ``https://api.kimi
    .com/coding/`` → ``https://api.kimi.com/coding/v1`` (its /v1/models
    answers 200); ``http://localhost:11434`` → ``http://localhost:11434/v1``
    (ollama pool).
    """
    base = (base_url or '').rstrip('/')
    if not base:
        return base
    parts = urllib.parse.urlsplit(base)
    path = parts.path
    if not _GROK_VERSIONED_PATH_RE.search(path):
        path = f'{path}/v1'
        base = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    return base


# --- ProjectMan-managed GROK_HOME per custom provider -----------------------
#
# WHY (the maintainer's ruling, 2026-10-05): a logged-in native home makes grok send
# the xAI SESSION JWT — not XAI_API_KEY — as the bearer on all inference
# calls to GROK_MODELS_BASE_URL endpoints (capture-server proven). That both
# 401s key-auth providers and leaks the session token to third parties. A
# ProjectMan-managed, sessionless GROK_HOME per provider fixes both: no
# cached token exists there, and each catalog model gets a BYOK
# ``[model.<id>]`` block (grok 1.0.46 custom-models doc: "Provider-backed
# models are BYOK: your xAI session token is never sent to their endpoints").
# Capture-server probes 2026-10-05 also showed the bare env path
# (GROK_MODELS_BASE_URL + XAI_API_KEY) is gated by grok's first-party API-key
# probe at startup ("Not signed in" for any non-xAI key) — model-level
# credentials bypass that gate, so the managed config carries the wiring.
# Never writes under ~/.grok/ (read-only): the status hook JSON references
# the existing bridge script there by ABSOLUTE path.

# Status-bridge hook events (PascalCase definition keys; the wire names are
# snake_case — see bridges/grok/README.md). Same set as the installed
# ~/.grok/hooks/projectman.json.
_GROK_HOOK_EVENTS = (
    'SessionStart', 'UserPromptSubmit', 'Stop', 'PreToolUse', 'PostToolUse',
    'PostToolUseFailure', 'PermissionDenied', 'Notification', 'SessionEnd',
)

# Header comment for the managed config.toml. INFORMATIONAL ONLY — grok
# rewrites this file when the user changes in-app settings and strips
# comments, so ownership/fingerprint tracking lives in the .pm-fingerprint
# SIDECAR, never in this file (see ensure_grok_home).
_GROK_HOME_HEADER = (
    '# projectman: managed grok home — ProjectMan owns the [model.*],\n'
    '# [compat.claude] and [subagents.models] sections of this file, the\n'
    '# session_summary key inside [models], and the preamble; everything\n'
    '# else is preserved.\n'
    '# Sessionless home for custom-provider Grok Build spawns: no cached\n'
    '# xAI session token exists here, so the session JWT is never sent to\n'
    '# third-party endpoints (BYOK model blocks; see build_grok_spawn_env).'
)
# Sidecar in the home holding the fingerprint of PM's last emission — grok
# never touches it, so it survives grok's config rewrites.
_GROK_HOME_SIDECAR = '.pm-fingerprint'

# PM-owned sections. ``model.*`` is PM's namespace: a user-authored
# [model.*] block inside a PM-managed home is overwritten (documented in
# ensure_grok_home). ``subagents.models`` likewise (only that exact table —
# a bare [subagents] section is the user's). ``models`` (plural) is grok's
# at the SECTION level — PM owns only the ``session_summary`` KEY inside it.
_GROK_COMPAT_SECTION = '[compat.claude]\nhooks = false'
_GROK_SECTION_HEADER_RE = re.compile(r'^\[([^\]]+)\]')

# Grok's own first-launch default config.toml content (52 bytes,
# probe-verified on a fresh sessionless home) — emitted as the [marketplace]
# section of a FRESH managed config; preserved verbatim once present.
_GROK_DEFAULT_MARKETPLACE_SECTION = ('[marketplace]\n'
                                     'default_skills_installs_purged = true')

_GROK_HOME_SAFE_RE = re.compile(r'[^A-Za-z0-9._-]')


def _pm_dotdir():
    """The ProjectMan dotdir (~/.ProjectMan) resolved the same way
    settings.py resolves DEFAULT_SETTINGS_PATH — read at call time so tests
    can monkeypatch settings.DEFAULT_SETTINGS_PATH."""
    from settings import DEFAULT_SETTINGS_PATH
    return os.path.dirname(DEFAULT_SETTINGS_PATH)


def grok_home_dir(provider_id):
    """``Path`` of the managed GROK_HOME for a custom provider:
    ``<dotdir>/grok-homes/<sanitized>`` — sanitized to [A-Za-z0-9._-],
    anything else becomes '_'."""
    safe = _GROK_HOME_SAFE_RE.sub('_', provider_id or '') or '_'
    import pathlib
    return pathlib.Path(_pm_dotdir()) / 'grok-homes' / safe


def _toml_str(value):
    """TOML basic-string literal for a config value. Escapes the quote, the
    backslash, the C0 control characters (\\n \\t \\r and anything else below
    0x20, plus DEL) as TOML escapes, so a hostile model id cannot break the
    managed config."""
    out = []
    for ch in str(value):
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\t':
            out.append('\\t')
        elif ch == '\r':
            out.append('\\r')
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def _atomic_write(path, content, mode=None):
    """tempfile.mkstemp-in-same-dir + os.replace (the settings.py norm) so a
    crash mid-write can never leave a half-written managed file."""
    import tempfile
    dir_path = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(tmp_path, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _grok_home_fingerprint(provider_id, base_url, entries,
                           session_summary='', subagent_pin='',
                           search_block=''):
    """Fingerprint of PM's emission inputs: provider id + base_url +
    ``entries`` (``(stripped_model_id, had_1m)`` pairs, sorted) + the
    session_summary pin, the subagent pin, the search MCP registration block
    (covers search_url presence, the pid, and the script path), and the
    web-search disable (constant) — so catalog/tier/option edits refresh
    homes exactly once."""
    import hashlib
    h = hashlib.sha1()
    h.update(str(provider_id).encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update(str(base_url).encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update(b'websearch:1\n')
    h.update(b'summary:' + session_summary.encode('utf-8', 'replace') + b'\n')
    h.update(b'subagent:' + subagent_pin.encode('utf-8', 'replace') + b'\n')
    h.update(b'search:' + search_block.encode('utf-8', 'replace') + b'\n')
    for mid, had_1m in entries:
        h.update(mid.encode('utf-8', 'replace'))
        h.update(b':1' if had_1m else b':0')
        h.update(b'\n')
    return h.hexdigest()[:16]


def _grok_search_mcp_section(provider_id):
    """PM's ``[mcp_servers.pm-search]`` block: the web_search bridge for
    this provider (present iff the catalog carries a search_url). Carries
    ONLY the provider id — the server reads settings.json (0600) at call
    time; no key material anywhere new.

    No trust write is needed: the managed home's config.toml IS the
    GROK_HOME user config, and grok's folder-trust (trusted_folders.toml)
    gates only repo-level ``.grok/config.toml`` scopes (docs 07-mcp-servers)
    — the same reason the native bridge's user-level registration needs
    none."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'pm_search_mcp.py')
    return '\n'.join([
        '[mcp_servers.pm-search]',
        'command = "python3"',
        f'args = [{_toml_str(script)}, "--provider", {_toml_str(provider_id)}]',
        'startup_timeout_sec = 15',
        'tool_timeout_sec = 60',
    ])


def _grok_model_section(stripped_id, had_1m, base):
    """One PM-owned ``[model."<id>"]`` section. ``env_key`` (never a literal
    key) keeps secrets out of this file. A catalog id that carried Claude's
    ``[1m]`` suffix emits ``context_window = 1048576`` — the suffix is the
    user's declaration that the model serves 1M on this endpoint (the Claude
    side works the same way; no network probing at spawn time). Caveat: if
    the account tier caps the model below 1M, the server will
    context-overflow past the cap — tiers cannot be probed from the endpoint
    row."""
    lines = [
        f'[model.{_toml_str(stripped_id)}]',
        f'model = {_toml_str(stripped_id)}',
        f'base_url = {_toml_str(base)}',
        'env_key = "XAI_API_KEY"',
    ]
    if had_1m:
        lines.append('context_window = 1048576')
    return '\n'.join(lines)


def _split_grok_config(text):
    """Split config.toml textually into ``(preamble, [(name, span), ...])``
    on top-level table headers (lines starting with ``[``). Spans are raw
    (header line through the line before the next header). A value line can
    only start with ``[`` when it is a header — TOML assignment lines start
    with the key."""
    preamble = []
    sections = []
    for line in text.splitlines(keepends=True):
        m = _GROK_SECTION_HEADER_RE.match(line)
        if m:
            sections.append([m.group(1).strip(), [line]])
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return ''.join(preamble), [(n, ''.join(ls)) for n, ls in sections]


def _pm_owns_grok_section(name):
    """PM owns the ``model.*`` namespace, ``compat.claude``, the exact
    ``subagents.models`` table, and the exact ``mcp_servers.pm-search``
    table — and ONLY those (``models`` plural is grok's at the section
    level; a bare ``subagents`` section and any other MCP servers are the
    user's)."""
    return (name == 'compat.claude' or name.startswith('model.')
            or name == 'subagents.models' or name == 'mcp_servers.pm-search')


def _canon_section(span):
    """A foreign section's content verbatim with only TRAILING blank lines
    stripped — canonical inter-section whitespace keeps re-emission
    byte-stable."""
    lines = span.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines)


_SESSION_SUMMARY_KEY_RE = re.compile(r'^session_summary\s*=')


def _without_session_summary(lines):
    """``[models]`` lines with PM's ``session_summary`` key removed.

    Precise key match (a sibling ``session_summary_extra`` survives), and
    when the matched opener begins a multiline basic string (a lone ``\"\"\"``
    on the line), the orphaned continuation lines are swallowed too — a stale
    opener must never leave invalid TOML behind."""
    kept = []
    swallowing = False
    for ln in lines:
        s = ln.strip()
        if swallowing:
            if '\"\"\"' in s:
                swallowing = False
            continue
        if _SESSION_SUMMARY_KEY_RE.match(s):
            if s.count('\"\"\"') % 2 == 1:
                swallowing = True        # multiline opener; eat until close
            continue
        kept.append(ln)
    return kept


def _merge_session_summary(span, value):
    """The ``[models]`` table with PM's ``session_summary`` key set: every
    other line (grok's ``default``, sibling keys, comments) preserved
    verbatim; any prior session_summary line (plus a multiline remnant)
    replaced. ``span`` may be None (no [models] yet) — then the table is
    created holding just our key."""
    if span is None:
        return f'[models]\nsession_summary = {_toml_str(value)}'
    kept = _without_session_summary(span.splitlines())
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append(f'session_summary = {_toml_str(value)}')
    return '\n'.join(kept)


def _strip_session_summary(span):
    """``[models]`` with any (stale) PM session_summary key removed and
    nothing appended — used when the catalog is empty and PM has no value to
    write. Sibling keys and grok's own lines survive."""
    kept = _without_session_summary(span.splitlines())
    while kept and not kept[-1].strip():
        kept.pop()
    return '\n'.join(kept)


def _grok_first_catalog_model(provider):
    """The provider's FIRST catalog model id ([1m]-stripped, catalog order)
    for ``[models] session_summary`` — grok's aux session-title calls need a
    concrete provider model (built-in ids fail silently on custom endpoints).
    '' when the catalog is empty (key omitted entirely)."""
    if isinstance(provider, dict):
        raw = provider.get('models')
        if isinstance(raw, list):
            for m in raw:
                if isinstance(m, str):
                    stripped = without_1m_suffix(m)
                    if stripped:
                        return stripped
    return ''


def _grok_subagent_pin(provider, subagent_tier):
    """The ``[subagents.models] general-purpose`` pin from the provider's
    Subagent tier assignment: the tier value [1m]-stripped when it names a
    model on the provider (same membership rule as resolve_grok_model);
    '' when the tier is unset/empty/stale — grok's inherit-parent stands."""
    if not isinstance(subagent_tier, str) or not subagent_tier:
        return ''
    stripped = without_1m_suffix(subagent_tier)
    if not stripped or not isinstance(provider, dict):
        return ''
    raw = provider.get('models')
    if not isinstance(raw, list):
        return ''
    catalog = {without_1m_suffix(m) for m in raw if isinstance(m, str)}
    return stripped if stripped in catalog else ''


def _render_grok_config(sections):
    """Preamble (header comment + PM's top-level keys) + sections joined with
    exactly one blank line. ``disable_web_search = true`` is top-level: the
    hosted web_search tool POSTs {base}/responses with hardcoded sampling
    params and is deterministically broken on ANY custom endpoint (Kimi 400s
    on temperature; ollama has no /responses at all), so PM disables it and
    the agent falls back to curl (the maintainer's tool-sampling report)."""
    return (_GROK_HOME_HEADER + '\n\n'
            + 'disable_web_search = true\n\n'
            + '\n\n'.join(sections) + '\n')


def _grok_model_entries(provider):
    """``(stripped_id, had_1m)`` pairs from a provider dict: ``[1m]`` stripped
    (Claude's flag is meaningless to grok), deduped on the stripped id (any
    variant flagged → context_window), sorted for determinism."""
    entries = {}
    if isinstance(provider, dict):
        raw = provider.get('models')
        if isinstance(raw, list):
            for m in raw:
                if not isinstance(m, str):
                    continue
                stripped = without_1m_suffix(m)
                if not stripped:
                    continue
                had = entries.get(stripped, False)
                entries[stripped] = had or is_1m_model_id(m)
    return sorted(entries.items())


def _managed_hook_json():
    """The managed home's hooks/projectman.json — the PM status-bridge hook
    definition (same event set as the installed bridge) whose commands point
    at the EXISTING script under ~/.grok/hooks/ by absolute path. Never
    copies the script; never writes under ~/.grok/."""
    import shlex
    status_script = os.path.expanduser('~/.grok/hooks/projectman-status.py')
    command = f'python3 {shlex.quote(status_script)}'
    hooks = {
        ev: [{'hooks': [{'type': 'command', 'command': command,
                         'timeout': 10}]}]
        for ev in _GROK_HOOK_EVENTS
    }
    doc = {
        '_comment': ('ProjectMan-managed status bridge for this managed '
                     'GROK_HOME. Commands reference the existing native-home '
                     'script by absolute path; ProjectMan never writes under '
                     '~/.grok/.'),
        'hooks': hooks,
    }
    return json.dumps(doc, indent=2) + '\n'


def ensure_grok_home(provider_id, provider=None, subagent_tier=''):
    """Create/refresh the managed GROK_HOME for *provider_id*; return its
    path as a string.

    Section/key ownership (works no matter how often grok rewrites the file —
    the in-file marker is informational only; the fingerprint lives in the
    ``.pm-fingerprint`` SIDECAR that grok never touches):

      * PM OWNS the ``[model.*]`` namespace, ``[compat.claude]``, the exact
        ``[subagents.models]`` table, and the preamble (header comment +
        ``disable_web_search``). User-authored blocks in those namespaces are
        overwritten (documented edge).
      * PM owns ONE KEY inside grok's ``[models]`` table: ``session_summary``
        (pinned to the provider's first catalog model so aux session-title
        calls hit a real model instead of failing on built-in ids). Every
        other line of ``[models]`` (grok's ``default``, user keys) is
        preserved verbatim, and the table stays at PM's fixed position so
        re-emission is byte-stable.
      * Everything else — ``[marketplace]``, ``[ui]``, a bare ``[subagents]``,
        any other section grok or the user added — is preserved verbatim
        (content lines byte-identical; only trailing blank lines and
        inter-section whitespace are canonicalized).

    *subagent_tier* is the raw ``tier_models[pid]['subagent']`` value; the
    pin is emitted only when it names a model on the provider (see
    :func:`_grok_subagent_pin`).

    Safe to run before every spawn: missing file → grok's default
    ``[marketplace]`` + PM sections; unreadable file (EACCES) → left alone;
    all writes atomic (mkstemp + os.replace, config chmod 0600). Foreign
    files in the home (grok's sessions/, models_cache.json, logs/, ...) are
    never touched, and nothing is ever written under ~/.grok/.
    """
    home = grok_home_dir(provider_id)
    os.makedirs(home, exist_ok=True)

    base_url = provider.get('base_url', '') if isinstance(provider, dict) else ''
    entries = _grok_model_entries(provider)
    base = _grok_base_url(base_url)
    summary = _grok_first_catalog_model(provider)
    subagent = _grok_subagent_pin(provider, subagent_tier)
    search_url = (provider.get('search_url') or '').strip() \
        if isinstance(provider, dict) else ''
    search_block = _grok_search_mcp_section(provider_id) if search_url else ''
    fingerprint = _grok_home_fingerprint(provider_id, base_url, entries,
                                         summary, subagent, search_block)

    def _pm_sections(models_span=None):
        sections = [_GROK_COMPAT_SECTION]
        if summary:
            sections.append(_merge_session_summary(models_span, summary))
        if subagent:
            sections.append(
                '[subagents.models]\ngeneral-purpose = ' + _toml_str(subagent))
        if search_block:
            sections.append(search_block)
        sections.extend(_grok_model_section(mid, had_1m, base)
                        for mid, had_1m in entries)
        return sections

    cfg_path = os.path.join(str(home), 'config.toml')
    if not os.path.exists(cfg_path):
        new_text = _render_grok_config(
            _pm_sections() + [_GROK_DEFAULT_MARKETPLACE_SECTION])
        _atomic_write(cfg_path, new_text, mode=0o600)
    else:
        try:
            with open(cfg_path, 'r') as f:
                existing = f.read()
        except OSError:
            existing = None          # unreadable (EACCES) → leave alone
        if existing is not None:
            _preamble, parsed = _split_grok_config(existing)
            models_span = None
            foreign = []
            for name, span in parsed:
                if _pm_owns_grok_section(name):
                    continue
                if name == 'models':
                    if summary:
                        # PM-positioned key-merge (fixed slot, byte-stable);
                        # the merged table keeps every non-PM line verbatim.
                        models_span = span
                        continue
                    # No summary to write: still strip a stale PM key from
                    # an earlier catalog (sibling keys/grok's lines survive).
                    foreign.append(_canon_section(_strip_session_summary(span)))
                    continue
                foreign.append(_canon_section(span))
            new_text = _render_grok_config(_pm_sections(models_span) + foreign)
            if new_text != existing:
                _atomic_write(cfg_path, new_text, mode=0o600)

    # Sidecar: survives grok's config rewrites; records the last emission.
    _atomic_write(os.path.join(str(home), _GROK_HOME_SIDECAR),
                  fingerprint + '\n')

    hooks_dir = os.path.join(str(home), 'hooks')
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, 'projectman.json')
    hook_doc = _managed_hook_json()
    try:
        with open(hook_path, 'r') as f:
            if f.read() == hook_doc:
                hook_doc = None
    except OSError:
        pass
    if hook_doc is not None:
        _atomic_write(hook_path, hook_doc)
    return str(home)


def build_grok_spawn_env(settings, project_path):
    """Build the env override for a grok spawn, or report a native fallback.

    Mirrors :func:`build_spawn_env`'s contract for Grok Build's custom
    endpoints (probe-verified 2026-10-05, grok 1.0.46): custom provider →
    ``(env_dict, None)`` holding the derived ``GROK_MODELS_BASE_URL``
    (+ ``/v1`` when the path is unversioned), ``XAI_API_KEY`` (a literal
    ``'dummy'`` when the provider has no key — e.g. ollama ignores the
    bearer), and ``GROK_HOME`` = :func:`ensure_grok_home` — a ProjectMan-
    managed, sessionless home whose config wires every catalog model as BYOK
    (``[model.<id>]`` with ``base_url`` + ``env_key = "XAI_API_KEY"``), pins
    aux session titles to the provider's first model
    (``[models] session_summary``), pins subagent spawns when the provider's
    Subagent tier names a catalog model (``[subagents.models]``), and drops
    the hosted web_search tool (``disable_web_search = true`` — broken on
    custom endpoints). The
    managed home is what keeps the xAI session token off third-party
    endpoints (no cached token exists there) and carries the status-bridge
    hook wiring; sessions/config/memory for custom-provider spawns live
    there, never in ``~/.grok``. Native → ``(None, None)`` (grok uses its own
    xAI creds and native home, byte-identical to pre-provider behavior);
    unusable custom provider → ``(None, reason)`` so the UI surfaces the same
    provider-unavailable toast shape as claude.

    The model rides ``-m`` argv (see :func:`resolve_grok_model`) — never env:
    ``GROK_MODEL`` does not exist, and no other ``GROK_*`` var is set here.
    Nothing is ever written under ``~/.grok/``. Native grok spawns pass
    through any ambient ``GROK_*``/``XAI_API_KEY`` shell exports unchanged —
    intentional, the same posture as claude native inheriting ``ANTHROPIC_*``.
    """
    pid = settings.effective_provider(project_path, 'grok')
    if not pid:
        return (None, None)
    prov = settings.providers.get(pid) if isinstance(settings.providers, dict) else None
    if not isinstance(prov, dict) or not prov.get('base_url'):
        name = prov.get('name') or pid if isinstance(prov, dict) else pid
        if not isinstance(prov, dict):
            reason = f"provider '{pid}' is not configured"
        else:
            reason = f"provider '{name}' has no base_url"
        return (None, reason)
    env = dict(os.environ)
    env['GROK_MODELS_BASE_URL'] = _grok_base_url(prov.get('base_url', ''))
    env['XAI_API_KEY'] = prov.get('api_key', '') or 'dummy'
    subagent_tier = ''
    tm = getattr(settings, 'tier_models', None)
    if isinstance(tm, dict):
        sub = tm.get(pid)
        if isinstance(sub, dict) and isinstance(sub.get('subagent'), str):
            subagent_tier = sub['subagent']
    env['GROK_HOME'] = ensure_grok_home(pid, prov, subagent_tier)
    return (env, None)


def resolve_grok_model(settings, project_path):
    """The ``-m`` model id for a grok spawn, or ``None`` for no flag.

    Native → the per-project model pin verbatim (today's behavior) or
    ``None``. Custom provider → always a concrete id: the pin stripped of
    Claude's ``[1m]`` context flag when it names a model on the provider
    (membership compared on stripped ids), else the provider's first model
    stripped. A pin is trusted verbatim when the provider lists no models at
    all — an empty catalog proves nothing against it. An explicit ``-m`` is
    mandatory under a custom provider: without it grok picks the endpoint's
    first listed model, which may not support chat (probe 2026-10-05).
    """
    pid = settings.effective_provider(project_path, 'grok')
    pin = settings.effective_model(project_path)
    if not pid:
        return pin or None
    models = [without_1m_suffix(m)
              for m in _provider_models(getattr(settings, 'providers', None), pid)]
    if pin:
        stripped = without_1m_suffix(pin)
        if not models or stripped in models:
            return stripped
    return models[0] if models else None


# ---------------------------------------------------------------------------
# Toast aggregation — pure helper (no GTK).
# ---------------------------------------------------------------------------

def aggregate_fallback_notices(events):
    """Collapse (project_name, reason, harness) fallback events into display
    string(s).

    WHY: N failing projects → N identical toasts dismissed one-by-one is poor
    UX. This helper groups events by (harness, reason): a single event returns
    the verbatim string; multiple events with the SAME harness+reason collapse
    to one aggregate; events with DIFFERENT reasons or DIFFERENT harnesses
    produce separate strings (a grok fallback is "running native Grok", never
    "running native Claude").

    Return value:
      ``None``/``''``  — empty input; caller shows nothing
      ``str``          — single group (one or more projects, collapsed)
      ``list[str]``    — two or more distinct groups (one string each)

    The single-project format is
    ``'provider unavailable — running native <harness>. <reason>'``.
    A bare 2-tuple ``(name, reason)`` is tolerated and rendered as Claude.
    """
    if not events:
        return None

    # Group by (harness, reason), preserving insertion order.
    from collections import OrderedDict
    groups: dict = OrderedDict()
    for event in events:
        reason = event[1]
        harness = event[2] if len(event) > 2 else 'Claude'
        key = (harness, reason)
        groups.setdefault(key, 0)
        groups[key] += 1

    def _format(harness, reason, count):
        if count == 1:
            return (f'provider unavailable — running native {harness}. '
                    f'{reason}')
        return (
            f'provider unavailable — {count} projects running native '
            f'{harness}. {reason}'
        )

    strings = [_format(h, r, c) for (h, r), c in groups.items()]
    if len(strings) == 1:
        return strings[0]
    return strings