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
import json
import urllib.request

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
      * Grok    → Grok (native) only
      * OpenCode → OpenCode (native) only
      * Kimi    → Kimi (native) only

    Returns ``[(id, label, selectable)]`` — ``selectable`` is always True for
    listed entries. Pure + defensive — never raises on bad settings shapes.
    """
    hid = harness_id or 'claude'
    if hid == 'grok':
        return [(NATIVE_GROK, GROK_NATIVE_LABEL, True)]
    if hid == 'opencode':
        return [(NATIVE_OPENCODE, OPENCODE_NATIVE_LABEL, True)]
    if hid == 'kimi':
        return [(NATIVE_KIMI, KIMI_NATIVE_LABEL, True)]
    # Claude (or unknown): Anthropic native + customs.
    entries = [('', NATIVE_LABEL, True)]
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

    Claude → ``effective_provider`` (Settings default or per-project pin).
    Grok / OpenCode → their native sentinel (model owned by the harness).
    """
    if harness_id is None:
        harness_id = getattr(settings, 'effective_harness', lambda p: 'claude')(
            project_path)
    if harness_id == 'grok':
        return NATIVE_GROK
    if harness_id == 'opencode':
        return NATIVE_OPENCODE
    if harness_id == 'kimi':
        return NATIVE_KIMI
    try:
        return settings.effective_provider(project_path) or ''
    except Exception:
        return getattr(settings, 'model_default', '') or ''


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
    pid = settings.effective_provider(project_path)
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
# Toast aggregation — pure helper (no GTK).
# ---------------------------------------------------------------------------

def aggregate_fallback_notices(events):
    """Collapse (project_name, reason) fallback events into display string(s).

    WHY: N failing projects → N identical toasts dismissed one-by-one is poor
    UX. This helper groups events by reason: a single event returns the verbatim
    string; multiple events with the SAME reason collapse to one aggregate;
    events with DIFFERENT reasons produce separate strings (one per reason).

    Return value:
      ``None``/``''``  — empty input; caller shows nothing
      ``str``          — single reason (one or more projects, collapsed)
      ``list[str]``    — two or more distinct reasons (one string each)

    The single-project format is
    ``'provider unavailable — running native Claude. <reason>'``
    (provider-agnostic; renamed from the old ccr-specific prefix).
    """
    if not events:
        return None

    # Group by reason text, preserving insertion order.
    from collections import OrderedDict
    groups: dict = OrderedDict()
    for _name, reason in events:
        groups.setdefault(reason, 0)
        groups[reason] += 1

    def _format(reason, count):
        if count == 1:
            return f'provider unavailable — running native Claude. {reason}'
        return (
            f'provider unavailable — {count} projects running native Claude. {reason}'
        )

    strings = [_format(r, c) for r, c in groups.items()]
    if len(strings) == 1:
        return strings[0]
    return strings