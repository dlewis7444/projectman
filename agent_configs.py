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

# Auth / account presence files (B2). These are checked for EXISTENCE + SIZE
# only — the credential CONTENTS are never read (a token file's mere presence is
# the signal). Each path is PROVABLE from the repo's own docs/fixtures:
#   * claude:  ~/.claude/.credentials.json  (the standard `claude` login store)
#   * grok:    ~/.grok/auth.json            (README.md: grok's OAuth flow writes
#              this file on sign-in; "no ~/.grok/auth.json is ever created" when
#              running offline with a per-model api_key).
# opencode has NO verifiable auth-file location in this repo's fixtures or docs,
# so B2 reports opencode's account state from its PARSED config (providers
# found) instead of inventing an auth path (flagged-not-guessed applies to code).
CLAUDE_CREDENTIALS_PATH = '~/.claude/.credentials.json'
GROK_AUTH_PATH = '~/.grok/auth.json'


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


# ── B1 / M-UX.8-residual: grok's [compat.claude] hooks compat surface ─────────
#
# F10 (sweep) + C1/C5: grok reads Claude-style hooks by default, so Claude's
# ProjectMan hook would DOUBLE-FIRE on grok events and fight the grok bridge for
# the status dot. install.sh sets ``[compat.claude] hooks = false`` to make the
# grok bridge the sole status writer — but nothing in the UI SHOWED that key's
# state (a behavior driven only by a TOML file = a C1 defect). This pure check
# surfaces it on the Agents page. Three states → three strings.

# compat.claude.hooks states (kept as constants so the UI strings are pinnable).
COMPAT_HOOKS_DISABLED = 'disabled'   # hooks = false  → the desired install state
COMPAT_HOOKS_ENABLED = 'enabled'     # hooks = true   → may double-fire
COMPAT_HOOKS_ABSENT = 'absent'       # key/file absent → grok's default (enabled)


def grok_compat_hooks_state(text, *, _empty_is_absent=True):
    """Classify grok config TOML's ``[compat.claude] hooks`` into one of three
    states (B1). PURE + defensive — never raises.

      * ``hooks = false``  → ``COMPAT_HOOKS_DISABLED`` (the bridge is the sole
        status writer; the install.sh-applied state);
      * ``hooks = true``   → ``COMPAT_HOOKS_ENABLED`` (Claude's hooks may
        double-fire on grok events);
      * key absent / file absent / garbage → ``COMPAT_HOOKS_ABSENT`` (grok's
        own default is enabled, so this reads the same risk as ``enabled``).

    A non-boolean ``hooks`` value (a typo'd config) is treated as ABSENT — we
    only claim "disabled ✓" when the file unambiguously says ``false``.
    """
    if not text or not text.strip():
        return COMPAT_HOOKS_ABSENT
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        # Unparseable TOML: we cannot prove hooks=false, so do not claim the
        # safe state. Treat as absent (same risk class as enabled).
        return COMPAT_HOOKS_ABSENT
    if not isinstance(data, dict):
        return COMPAT_HOOKS_ABSENT
    compat = data.get('compat')
    claude = compat.get('claude') if isinstance(compat, dict) else None
    if not isinstance(claude, dict):
        return COMPAT_HOOKS_ABSENT
    hooks = claude.get('hooks')
    if hooks is False:
        return COMPAT_HOOKS_DISABLED
    if hooks is True:
        return COMPAT_HOOKS_ENABLED
    return COMPAT_HOOKS_ABSENT


def grok_compat_hooks_line(*, home=None, path=None):
    """The read-only "Claude-hooks compat" subtitle for the grok Agents section
    (B1). Loads grok's config (defensive) and maps its state to one string:

      * disabled       → ``"disabled ✓ (status dots fire once)"``
      * enabled/absent → ``"⚠ enabled — Claude's hooks may double-fire on grok
        events (fixed by Install/Update bridge)"``

    Pure-ish glue over ``grok_compat_hooks_state`` (reads the file, never its
    meaning beyond the one key). Never raises.
    """
    src = path or _expand(GROK_CONFIG_PATH, home)
    try:
        with open(src, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        text = ''
    state = grok_compat_hooks_state(text)
    if state == COMPAT_HOOKS_DISABLED:
        return 'disabled ✓ (status dots fire once)'
    return ("⚠ enabled — Claude's hooks may double-fire on grok events "
            "(fixed by Install/Update bridge)")


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

    FB-1b (the CROSS-CORRECTED ruling, P3.5e): when the config declares NO
    ``[models] default`` key, the suffix reads ``"— built-in default (managed
    by <Display>)"`` — NEVER a sole-``[model.*]``-block inference. Coherence
    sweep-2's F11 proposed treating a lone model block as the effective default;
    subscriber-2's OBSERVED live turn DISPROVED it (grok's real default with no
    ``default`` key is the BUILT-IN grok-build, invisible to the config — the
    lone pool-qwen block "looks active and isn't"). So no default key ⇒ say
    "built-in default", do not promote any block.

    ``native_label`` defaults to ``models.NATIVE_LABEL`` (lazy import to keep
    this module GTK-free and import-light). Pure + defensive — never raises.
    """
    import agents as _agents
    agent_id = getattr(settings, 'agent_default', '') or _agents.DEFAULT_AGENT
    return _label_for_agent(agent_id, home=home, native_label=native_label)


def default_model_label_for(settings, project_path, *, home=None,
                            native_label=None):
    """Per-ROW truthful "Default Model" label for ``project_path`` (C2 / P3.5f).

    David's second reveal: a claude-OVERRIDE project's Model submenu wore the
    GLOBAL default agent's label — "Default (Managed by Grok Build…)" on a row
    that actually runs claude — because the window computed ONE label from
    ``agent_default`` and pushed it to every row. This resolves the project's
    EFFECTIVE agent (``settings.effective_agent(project_path)`` — per-project
    override beats the global default) and labels THAT agent's model story, so a
    claude-override row reads claude's native/ccr label exactly as it would when
    claude IS the global default, and a follow-default row matches
    ``default_model_label`` (the global default agent). Pure + defensive.
    """
    agent_id = settings.effective_agent(project_path)
    return _label_for_agent(agent_id, home=home, native_label=native_label)


def _label_for_agent(agent_id, *, home=None, native_label=None):
    """Shared label body for ``default_model_label`` (global) and
    ``default_model_label_for`` (per-row). Given a resolved ``agent_id``,
    return its truthful model-story label per the FB-1b rules. Never raises.
    """
    if native_label is None:
        from models import NATIVE_LABEL
        native_label = NATIVE_LABEL
    import agents as _agents
    adapter = _agents.ADAPTERS.get(agent_id)
    display = adapter.display_name if adapter is not None else agent_id

    cfg = load_agent_config(agent_id, home=home)
    if cfg is None:
        # claude (or any non-native-config agent): the providers/ccr model story.
        return native_label

    src = _display_path(cfg.source_path, home=home)
    base = f'Managed by {display} ({src})'
    if cfg.default_key:
        entry = cfg.default_entry()
        if entry is not None:
            name = entry.name or entry.model or entry.key
            if name:
                return f'{base} — {name}'
        # Config names a default but no matching [model.*] block (a built-in
        # model id) — show the bare key rather than nothing.
        return f'{base} — {cfg.default_key}'
    # FB-1b: NO declared default key. The truth is the agent's BUILT-IN default
    # (invisible to the config); we must NOT infer a sole block as active.
    return f'{base} — built-in default (managed by {display})'


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


# ── FB-1a / FB-1c: native per-project model picker options ────────────────────
#
# noob S7 / subscriber S7 (C2/C4): our own P3.5 README promises "set the
# per-project model to pool-qwen in ProjectMan", but the picker only ever listed
# claude/ccr models — the no-subscription grok flow dead-ended. This surfaces the
# EFFECTIVE agent's NATIVE models for the per-project Model submenu:
#   * grok     → each ``[model.<key>]`` block; the stored value is the KEY
#                (``pool-qwen``) — exactly what GrokAdapter passes to ``-m``;
#   * opencode → each ``provider.<pid>.models.<mid>``; the stored value is the
#                ``<provider>/<model>`` id — what OpencodeAdapter passes to ``-m``.
# The config-declared default (``default_key``) is flagged with a "• default"
# marker (FB-1c, opencode parity); when the config declares NO default key, NO
# entry is marked (the cross-corrected FB-1b truth — the real default is the
# agent's built-in, invisible to the config, so we never fake an active marker).

# Suffix appended to the model whose key matches the config's declared default.
DEFAULT_MARKER = ' • default'   # " • default"


def native_model_options(agent_id, *, home=None, cfg=None):
    """``(ids, labels)`` parallel lists of ``agent_id``'s NATIVE models, or
    ``None`` for an agent with no native model-config surface (claude/unknown —
    that picker stays the ccr/providers list, byte-identical to today).

    ``ids`` are the values written into ``model_overrides[path]`` and passed to
    the adapter's ``-m`` (grok = the config KEY; opencode = ``provider/model``).
    ``labels`` are the human display strings, with the config-declared default
    suffixed ``" • default"`` (FB-1c). An absent/garbage config yields
    ``([], [])`` — the agent IS native but has no parseable models, so the
    submenu shows just the Default entry the row prepends. Pure + defensive
    (the underlying parsers never raise). ``cfg`` injectable for tests.
    """
    if cfg is None:
        cfg = load_agent_config(agent_id, home=home)
    if cfg is None:
        return None
    ids = []
    labels = []
    has_default = bool(cfg.default_key)
    for entry in cfg.models:
        key = getattr(entry, 'key', '') or ''
        if not key:
            continue
        label = entry.name or entry.model or key
        # Show the key alongside a distinct display name so the stored value is
        # legible (grok keys like ``pool-qwen`` ARE the -m value).
        if entry.name and entry.name != key:
            label = f'{entry.name} ({key})'
        if has_default and key == cfg.default_key:
            label = f'{label}{DEFAULT_MARKER}'
        ids.append(key)
        labels.append(label)
    return ids, labels


# ── B2 / M-UX.13: per-agent account / auth status (PRESENCE-BASED) ────────────
#
# Constitution C1/C2 + triage-2 S2/S3/S9: the Agents page showed grok/opencode
# as a binary to INVOKE, never a subscription to HONOR — "is my account
# connected?" had no answer short of running a session. This adds one read-only
# status line per agent on the Agents page, driven by PRESENCE of the agent's
# own token store (existence + size ONLY — credential CONTENTS are never read).
# The doctor "Check" button remains the live probe; this is the at-a-glance line.
#
# Honesty rule (flagged-not-guessed, extended to CODE): we only check auth-file
# paths PROVABLE from this repo's docs/fixtures (claude's .credentials.json,
# grok's auth.json per README). opencode has no verifiable auth-store path here,
# so its line reports what we CAN prove — configured providers from its parsed
# config — and never invents an auth-file path.


def _nonempty_file(path):
    """True iff ``path`` exists and is a non-empty regular file. Existence +
    SIZE only — the file is never opened/read. Never raises."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def claude_account_line(*, home=None, path=None):
    """The Agents-page account status line for claude (B2). Presence-based.

      * ``~/.claude/.credentials.json`` non-empty → "Signed in (credentials
        present)";
      * else → "Not signed in — run `claude` once to sign in".

    Existence + size only; contents never read. Never raises.
    """
    src = path or _expand(CLAUDE_CREDENTIALS_PATH, home)
    if _nonempty_file(src):
        return 'Signed in (credentials present)'
    return 'Not signed in — run `claude` once to sign in'


def grok_account_line(*, home=None, auth_path=None, config_path=None):
    """The Agents-page account status line for grok (B2).

      * ``~/.grok/auth.json`` non-empty → "Signed in (token present)";
      * else if any parsed ``[model.*]`` block carries an ``api_key`` →
        "API key configured (<config path>)";
      * else → "Not signed in — `grok login`".

    The auth file is checked for existence + size only (never read). The
    ``api_key`` fallback honors the README's offline-pool recipe (a per-model
    ``api_key`` means grok never runs its OAuth flow, so no auth.json exists yet
    the account is "configured"). Never raises.
    """
    src = auth_path or _expand(GROK_AUTH_PATH, home)
    if _nonempty_file(src):
        return 'Signed in (token present)'
    cfg = load_grok_config(home=home, path=config_path)
    if _grok_has_api_key(cfg, home=home, path=config_path):
        shown = _display_path(cfg.source_path, home=home)
        return f'API key configured ({shown})'
    return 'Not signed in — `grok login`'


def _grok_has_api_key(cfg, *, home=None, path=None):
    """True iff grok's parsed config has a non-empty ``api_key`` in any
    ``[model.*]`` block. We must re-read the raw config because the parsed
    ``ModelEntry`` deliberately drops credential fields — but we only test the
    PRESENCE of a non-empty key, never surface its value. Never raises."""
    if cfg is None or not cfg.exists:
        return False
    src = path or cfg.source_path or _expand(GROK_CONFIG_PATH, home)
    try:
        with open(src, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return False
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        return False
    model_tbl = data.get('model') if isinstance(data, dict) else None
    if not isinstance(model_tbl, dict):
        return False
    for block in model_tbl.values():
        if isinstance(block, dict):
            key = block.get('api_key')
            if isinstance(key, str) and key.strip():
                return True
    return False


def opencode_account_line(*, home=None, config_path=None):
    """The Agents-page account status line for opencode (B2).

    opencode's auth-store location is NOT verifiable from this repo's fixtures
    or docs, so we DO NOT invent one. We report what is provable from its parsed
    config:

      * providers found → "Providers configured: <n> (<config path>)";
      * none / no config → "No providers found".

    Never raises (the opencode parser guarantees it).
    """
    cfg = load_opencode_config(home=home, path=config_path)
    n = _opencode_provider_count(cfg)
    if n > 0:
        shown = _display_path(cfg.source_path, home=home)
        return f'Providers configured: {n} ({shown})'
    return 'No providers found'


def _opencode_provider_count(cfg):
    """Number of distinct providers in a parsed opencode config. The parser
    flattens to ``<provider>/<model>`` keys, so derive the provider set from the
    leading segment of each entry key. Never raises."""
    if cfg is None or not cfg.models:
        return 0
    providers = set()
    for entry in cfg.models:
        key = getattr(entry, 'key', '') or ''
        providers.add(key.split('/', 1)[0] if '/' in key else key)
    providers.discard('')
    return len(providers)


def account_status_line(agent_id, *, home=None):
    """Dispatch to the right B2 account-status line for ``agent_id``.

    Returns the presence-based status string for claude/grok/opencode, or None
    for agents with no account surface (an unknown id → None). Never raises.
    """
    if agent_id == 'claude':
        return claude_account_line(home=home)
    if agent_id == 'grok':
        return grok_account_line(home=home)
    if agent_id == 'opencode':
        return opencode_account_line(home=home)
    return None


# ── B3 / M-UX.14: is the ccr block "in use"? ──────────────────────────────────
#
# C2/C3 + triage-2 S9: the Claude Code Router block ("Installed — service
# stopped") frightened users who never configured a custom Claude model. This
# pure decision collapses the block to a single self-explaining row when ccr is
# not in use, and shows the full controls when it is.


def ccr_in_use(settings):
    """True iff the ccr block should show its FULL controls (B3).

    ccr is "in use" when the user has configured custom Claude models — i.e.
    ``providers`` is non-empty, OR any custom (``provider/model``) id is set as
    the default or a per-project override. The empty state (``providers == {}``
    AND no custom model overrides) collapses the block to one explanatory row.
    Defensive — tolerates a settings object missing the attributes. Never raises.
    """
    providers = getattr(settings, 'providers', {}) or {}
    if isinstance(providers, dict) and providers:
        return True
    # No providers defined: a default/override referencing a 'provider/model'
    # still means ccr was meant to be used (even if the provider is now gone).
    candidates = [getattr(settings, 'model_default', '') or '']
    overrides = getattr(settings, 'model_overrides', {}) or {}
    if isinstance(overrides, dict):
        candidates.extend(v or '' for v in overrides.values())
    for model in candidates:
        if isinstance(model, str) and '/' in model:
            return True
    return False
