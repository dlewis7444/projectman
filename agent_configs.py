"""Read-only surfacing of native agent model configs (M-UX.2, C1/C2).

The Experience Gate pilot found that grok and opencode each decide which model
they run from a config file ProjectMan never showed: grok from
``~/.grok/config.toml`` (``[models] default`` + ``[model.<key>]`` blocks),
opencode from ``~/.config/opencode/opencode.json`` (its ``provider``/``model``
map). The "Default Model" row and the Models page told a Claude-only story while
Grok Build was running Qwen from one of those files (C2 origin case; C1 origin
case).

This module is PURE (no GTK; like ``zellij.py``/``models.py``) and READ-ONLY: it
PARSES those native files into plain dataclasses for the Settings → Models page
and the per-agent "Default Model" label to display with source-path attribution.
It NEVER writes an agent-owned file (editing is P4, the spec's read-first
ruling). Every parser is DEFENSIVE: a missing, unreadable, or garbage file
yields an empty result — it must never raise, because it runs on whatever the
user happens to have on disk.

Design source: docs/superpowers/plans/2026-06-10-p3.5-ux.md items 1-2;
constitution C1 (VISIBILITY) + C2 (TRUTH IN STRINGS).
"""
import os
from dataclasses import dataclass, field


# Default native config locations (overridable for tests / non-standard homes).
GROK_CONFIG_PATH = '~/.grok/config.toml'
OPENCODE_CONFIG_PATHS = (
    '~/.config/opencode/opencode.json',
    '~/.config/opencode/config.json',  # older builds named it config.json
)


@dataclass
class ModelEntry:
    """One model an agent can run, as named in its OWN config.

    ``key`` is the identifier the agent uses (a grok ``[model.<key>]`` name, an
    opencode ``provider/model`` id); ``name`` is the human label from the config
    (falls back to ``key``); ``model`` is the upstream model id when the config
    states one separately (grok's ``model =`` line); ``base_url`` is the endpoint
    when present. All optional except ``key``.
    """
    key: str
    name: str = ''
    model: str = ''
    base_url: str = ''


@dataclass
class AgentModelConfig:
    """A parsed read-only view of one agent's native model config.

    ``source_path`` is the absolute file the data came from (for the UI's
    "edited in the agent's own config" attribution). ``exists`` is whether that
    file was found and read. ``default_key`` is the config's declared default
    model id, if any. ``models`` is every model entry the config defines. An
    absent/garbage file yields ``exists=False`` and empty lists — never raises.
    """
    agent_id: str
    source_path: str
    exists: bool = False
    default_key: str = ''
    models: list = field(default_factory=list)

    def default_entry(self):
        """The ModelEntry matching ``default_key``, or None.

        Falls back to None (not a raise) when the default key names no defined
        model — a real config can reference a default that isn't in a
        ``[model.*]`` block (grok ships built-in model ids), in which case the
        UI shows the bare key.
        """
        for m in self.models:
            if m.key == self.default_key:
                return m
        return None


def _expand(path, home=None):
    """Expand ``~`` against ``home`` (or the real HOME)."""
    if home is not None:
        if path.startswith('~/'):
            return os.path.join(home, path[2:])
        if path == '~':
            return home
    return os.path.expanduser(path)


# ── grok: ~/.grok/config.toml ─────────────────────────────────────────────────

def parse_grok_config(text, *, source_path=''):
    """Parse grok's ``config.toml`` text into an ``AgentModelConfig``.

    Reads ``[models] default`` (the active default model key) and every
    ``[model.<key>]`` block (``name`` / ``model`` / ``base_url``). Uses the
    stdlib ``tomllib`` (read-only; 3.11+); any parse error → ``exists=False``
    empty result. NEVER raises.

    The two table spellings grok accepts are both honored: the singular
    ``[models]`` table with a ``default`` key, and the per-model
    ``[model.<key>]`` tables. (The bench's config used ``[models] default =
    "pool-qwen"`` + ``[model.pool-qwen]``.)
    """
    cfg = AgentModelConfig(agent_id='grok', source_path=source_path)
    if not text or not text.strip():
        return cfg
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        # Unparseable TOML (or no tomllib) → defensive empty result. The file
        # genuinely existed (we were handed its text), so mark exists True with
        # no parsed entries rather than claiming it's absent.
        cfg.exists = True
        return cfg
    if not isinstance(data, dict):
        cfg.exists = True
        return cfg
    cfg.exists = True
    models_tbl = data.get('models')
    if isinstance(models_tbl, dict):
        default = models_tbl.get('default')
        if isinstance(default, str):
            cfg.default_key = default
    model_tbl = data.get('model')
    if isinstance(model_tbl, dict):
        for key in sorted(model_tbl):
            block = model_tbl.get(key)
            if not isinstance(block, dict):
                continue
            cfg.models.append(ModelEntry(
                key=str(key),
                name=str(block.get('name') or ''),
                model=str(block.get('model') or ''),
                base_url=str(block.get('base_url') or ''),
            ))
    return cfg


def load_grok_config(*, home=None, path=None):
    """Load + parse grok's config from disk (defensive). Returns AgentModelConfig.

    ``path`` overrides the location; otherwise ``GROK_CONFIG_PATH`` under
    ``home``. A missing/unreadable file yields ``exists=False`` — never raises.
    """
    src = path or _expand(GROK_CONFIG_PATH, home)
    try:
        with open(src, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return AgentModelConfig(agent_id='grok', source_path=src, exists=False)
    cfg = parse_grok_config(text, source_path=src)
    return cfg


# ── opencode: ~/.config/opencode/opencode.json ────────────────────────────────

def parse_opencode_config(text, *, source_path=''):
    """Parse opencode's JSON config into an ``AgentModelConfig``.

    Reads the top-level ``model`` (its default ``provider/model`` id) and every
    model under ``provider.<pid>.models`` (an opencode model id is
    ``<provider>/<model>``; the label is the model block's ``name`` or the bare
    model id). Defensive: bad JSON / wrong shape → ``exists=True`` with no
    entries (the file existed) or, for empty input, ``exists=False``. NEVER
    raises.
    """
    cfg = AgentModelConfig(agent_id='opencode', source_path=source_path)
    if not text or not text.strip():
        return cfg
    import json as _json
    try:
        data = _json.loads(text)
    except (ValueError, TypeError):
        cfg.exists = True
        return cfg
    if not isinstance(data, dict):
        cfg.exists = True
        return cfg
    cfg.exists = True
    default = data.get('model')
    if isinstance(default, str):
        cfg.default_key = default
    providers = data.get('provider')
    if isinstance(providers, dict):
        for pid in sorted(providers):
            prov = providers.get(pid)
            if not isinstance(prov, dict):
                continue
            base_url = ''
            opts = prov.get('options')
            if isinstance(opts, dict):
                base_url = str(opts.get('baseURL') or opts.get('base_url') or '')
            models = prov.get('models')
            if not isinstance(models, dict):
                continue
            for mid in sorted(models):
                entry = models.get(mid)
                name = ''
                if isinstance(entry, dict):
                    name = str(entry.get('name') or '')
                cfg.models.append(ModelEntry(
                    key=f'{pid}/{mid}',
                    name=name,
                    model=str(mid),
                    base_url=base_url,
                ))
    return cfg


def load_opencode_config(*, home=None, path=None):
    """Load + parse opencode's config from disk (defensive). Returns AgentModelConfig.

    Tries ``OPENCODE_CONFIG_PATHS`` in order (newer ``opencode.json`` first); the
    first file that exists wins. A total miss yields ``exists=False`` with the
    primary path as ``source_path`` — never raises.
    """
    candidates = [path] if path else [_expand(p, home) for p in OPENCODE_CONFIG_PATHS]
    for src in candidates:
        if not src:
            continue
        try:
            with open(src, 'r', encoding='utf-8') as f:
                text = f.read()
        except OSError:
            continue
        return parse_opencode_config(text, source_path=src)
    primary = candidates[0] if candidates else ''
    return AgentModelConfig(agent_id='opencode', source_path=primary, exists=False)


def load_agent_config(agent_id, *, home=None):
    """Dispatch to the right native-config loader for ``agent_id``.

    Returns an ``AgentModelConfig`` for grok/opencode, or None for agents with
    no native model-config surface (claude routes models through ccr, which has
    its own Models-page surface; an unknown id → None). Never raises.
    """
    if agent_id == 'grok':
        return load_grok_config(home=home)
    if agent_id == 'opencode':
        return load_opencode_config(home=home)
    return None


# ── M-UX.1: the effective default agent's "model story" label ─────────────────

def default_model_label(settings, *, home=None, native_label=None):
    """The truthful "Default Model" label for the EFFECTIVE default agent (C2).

    The pilot's #1 finding: the row said "Default (Anthropic (native Claude))"
    while grok ran Qwen from config.toml. This resolver tells the truth per the
    agent that actually decides the model for new sessions:

      * claude (or any agent with no native model config) → ``native_label``
        (today's ccr/Anthropic text — claude's model story is the providers
        surface, unchanged);
      * grok / opencode → ``"Managed by <Display> (<config path>)"`` plus the
        resolved default model name when the config names one parseably (e.g.
        ``"Managed by Grok Build (~/.grok/config.toml) — Qwen3.5 9B (Ollama
        pool)"``). When the config is absent/garbage the suffix is dropped but
        the agent + path attribution still hold, so the row never lies about
        WHICH agent owns the model.

    ``native_label`` defaults to ``models.NATIVE_LABEL`` (lazy import to keep
    this module GTK-free and import-light). Pure + defensive — never raises.
    """
    if native_label is None:
        from models import NATIVE_LABEL
        native_label = NATIVE_LABEL
    import agents as _agents
    agent_id = getattr(settings, 'agent_default', '') or _agents.DEFAULT_AGENT
    adapter = _agents.ADAPTERS.get(agent_id)
    display = adapter.display_name if adapter is not None else agent_id

    cfg = load_agent_config(agent_id, home=home)
    if cfg is None:
        # claude (or any non-native-config agent): the providers/ccr model story.
        return native_label

    src = _display_path(cfg.source_path, home=home)
    base = f'Managed by {display} ({src})'
    entry = cfg.default_entry()
    if entry is not None:
        name = entry.name or entry.model or entry.key
        if name:
            return f'{base} — {name}'
    if cfg.default_key:
        # Config names a default but no matching [model.*] block (a built-in
        # model id) — show the bare key rather than nothing.
        return f'{base} — {cfg.default_key}'
    return base


def _display_path(path, *, home=None):
    """Render an absolute config path back to a ``~/``-relative display form.

    Tilde-collapsing keeps the attribution readable and home-agnostic in
    screenshots; falls back to the raw path when it isn't under home.
    """
    if not path:
        return path
    base = home if home is not None else os.path.expanduser('~')
    try:
        if path == base:
            return '~'
        if path.startswith(base + os.sep):
            return '~/' + os.path.relpath(path, base)
    except (ValueError, TypeError):
        pass
    return path
