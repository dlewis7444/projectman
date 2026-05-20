"""Provider/model helpers shared by the settings UI and the main window.

The provider definitions live in ``Settings.providers`` as::

    {provider_id: {"name": str,            # display label
                   "base_url": str,        # provider API base URL
                   "api_key": str,         # cleartext key (see settings.py)
                   "transformer": str|None,# optional ccr transformer name
                   "models": {model_id: {"name": str}}}}

A model is identified everywhere by the string ``'provider_id/model_id'``.
The empty string ``''`` is the sentinel for "Anthropic (native Claude)".
"""

NATIVE_LABEL = 'Anthropic (native Claude)'

# Sentinel used by the per-project model menu to mean "follow the global
# default" (i.e. remove any override). Safe because a real model id is either
# '' (native) or contains a '/'.
FOLLOW_DEFAULT = '__default__'


def build_model_options(providers):
    """Return ``(ids, labels)`` parallel lists for a model picker.

    Index 0 is always the native-Anthropic sentinel (id ``''``). Remaining
    entries are ``'provider/model'`` ids with ``'Provider — Model'`` labels,
    sorted for stable ordering. A malformed ``providers`` value degrades to
    just the native entry rather than raising.
    """
    ids = ['']
    labels = [NATIVE_LABEL]
    if not isinstance(providers, dict):
        return ids, labels
    for pid in sorted(providers):
        prov = providers.get(pid)
        if not isinstance(prov, dict):
            continue
        pname = prov.get('name') or pid
        models = prov.get('models')
        if not isinstance(models, dict):
            continue
        for mid in sorted(models):
            entry = models.get(mid)
            mname = entry.get('name', mid) if isinstance(entry, dict) else mid
            ids.append(f'{pid}/{mid}')
            labels.append(f'{pname} — {mname}')
    return ids, labels


def model_label(providers, model_id):
    """Human-readable label for a ``'provider/model'`` id.

    Returns the native label for ``''`` and falls back to the raw id when the
    provider/model is unknown (e.g. a stale per-project override).
    """
    if not model_id:
        return NATIVE_LABEL
    ids, labels = build_model_options(providers)
    try:
        return labels[ids.index(model_id)]
    except ValueError:
        return model_id


def validate_providers(parsed):
    """Validate a parsed providers dict; raise ``ValueError`` on a bad shape.

    Lenient by design — only rejects shapes that would break the model picker
    or ccr config generation. Partially-filled providers (missing base_url or
    api_key) are allowed so a user can save work in progress.
    """
    if not isinstance(parsed, dict):
        raise ValueError('top level must be a JSON object of providers')
    for pid, prov in parsed.items():
        if not isinstance(prov, dict):
            raise ValueError(f'provider "{pid}" must be an object')
        models = prov.get('models', {})
        if not isinstance(models, dict):
            raise ValueError(f'provider "{pid}": "models" must be an object')
        for mid, entry in models.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f'provider "{pid}", model "{mid}" must be an object')
    return parsed
