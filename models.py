"""Provider/model helpers shared by the settings UI, the sidebar, and the
spawn path.

The provider definitions live in ``Settings.providers`` as::

    {provider_id: {"name": str,            # display label
                   "base_url": str,        # Anthropic-compatible API base URL
                   "api_key": str,          # cleartext key (see settings.py)
                   "models": [str, ...]}}   # free-text model ids (CC strips a
                                           # trailing ``[1m]`` itself)

A provider is identified by its ``provider_id``; the empty string ``''`` is the
sentinel for "Anthropic (native)" — no env injection, CC uses its own creds.

The four Claude Code tiers (Opus/Sonnet/Haiku/Subagent) are each pinnable to
any model id on the ACTIVE provider. CC's ``ANTHROPIC_BASE_URL`` is
process-wide, so one session can mix model NAMES across tiers but never
providers — every tier must be reachable from the active provider's endpoint.
``build_spawn_env`` resolves the four tiers and injects the ollama-style env
dict at spawn.
"""

import os

NATIVE_LABEL = 'Anthropic (native)'

# Sentinel used by the per-project provider menu to mean "follow the global
# default" (i.e. remove any override). Safe because a real provider id is a
# non-empty dict key and never this string.
FOLLOW_DEFAULT = '__default__'

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
    if not isinstance(providers, dict):
        return pid
    prov = providers.get(pid)
    if not isinstance(prov, dict):
        return pid
    return prov.get('name') or pid


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


def resolve_tier_model(settings, pid, tier):
    """Resolve the model id to pin for a tier on the active provider.

    ``tier_models[tier]`` wins when it names a model on the active provider;
    otherwise the provider's first model is used; if the provider has no
    models the empty string is returned (the spawn env still sets the var, CC
    will fall back to its own default). Covers a stale tier value left over
    from a per-project override to a provider that lacks the named model.
    """
    models = _provider_models(settings.providers, pid)
    val = ''
    if isinstance(getattr(settings, 'tier_models', None), dict):
        v = settings.tier_models.get(tier, '')
        if isinstance(v, str):
            val = v
    if val and val in models:
        return val
    return models[0] if models else ''


def build_spawn_env(settings, project_path):
    """Build the env override for a spawn, or report a native fallback.

    Returns ``(env_dict, None)`` for a custom provider (the ollama-style env
    dict, incl. the four resolved tier models + ``DISABLE_AUTOUPDATER=1``);
    ``(None, None)`` for native (no injection — CC uses its own creds); or
    ``(None, reason)`` when a custom provider was requested but is unusable
    (missing or no base_url), so the spawn falls back to native and the UI
    surfaces ``reason`` via the provider-unavailable toast.
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
    env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = resolve_tier_model(settings, pid, 'opus')
    env['ANTHROPIC_DEFAULT_SONNET_MODEL'] = resolve_tier_model(settings, pid, 'sonnet')
    env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] = resolve_tier_model(settings, pid, 'haiku')
    env['CLAUDE_CODE_SUBAGENT_MODEL'] = resolve_tier_model(settings, pid, 'subagent')
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