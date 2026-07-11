import os
import re
import json
import tempfile
from dataclasses import dataclass, asdict, field


DEFAULT_SETTINGS_PATH = os.path.expanduser('~/.ProjectMan/settings.json')

# One-shot migration only: legacy spawn auto-appended ``[1m]`` for model ids
# matching this pattern when used on Opus/Fable. After migration the UI toggle
# owns the flag; this regex must not be used at spawn time.
_LEGACY_1M_MODEL_RE = re.compile(r'glm|deepseek', re.IGNORECASE)

# The Claude Code model tiers. PM can pin each independently to any model
# defined on the active provider (free-text ids), or leave a tier on '' to use
# the provider's first/default model. See models.build_spawn_env.
TIERS = ('opus', 'sonnet', 'haiku', 'subagent', 'fable')


@dataclass
class Settings:
    projects_dir: str = '~/.ProjectMan/projects'
    claude_binary: str = ''
    resume_projects: bool = True
    font_size: int = 11
    scrollback_lines: int = 10000
    audible_bell: bool = False
    multiplexer: str = 'none'
    theme: str = 'argonaut'
    debug_logging: bool = False
    sidebar_width: int = 220
    paa_enabled: bool = False
    paa_loop_interval_minutes: int = 30
    paa_budget_tokens: int = 100000
    paa_budget_used: int = 0
    paa_budget_unlimited: bool = False
    paa_allow_haiku: bool = True
    paa_autonomy_level: str = 'suggest'
    paa_budget_month: str = ''
    paa_chat_model: str = 'sonnet'
    paa_scan_model: str = 'haiku'
    paa_stale_days: int = 60
    ntfy_enabled: bool = False
    ntfy_topic: str = ''
    # --- Model layer (Claude-Only + first-class model axis, 2026-06) ---
    # providers: {provider_id: {"name": str, "base_url": str, "api_key": str,
    #                           "models": [str, ...],
    #                           "max_context_tokens": int  # optional
    #                          }}
    #   ``models`` is a LIST of free-text model-id strings. A trailing ``[1m]``
    #   is the per-model 1M-context flag (CC strips it before the API call);
    #   the provider editor's 1M toggle encodes/strips that suffix. Optional
    #   ``max_context_tokens`` injects CLAUDE_CODE_MAX_CONTEXT_TOKENS on spawn
    #   for that custom provider only (native Anthropic is untouched).
    providers: dict = field(default_factory=dict)
    # model_default: provider_id | ''  — global default provider ('' = native).
    # Historical name; this is the default *provider* axis, not a model id.
    model_default: str = ''
    # --- Dual axes (1.4.1): provider vs model, harness-agnostic storage ---
    # provider_overrides: {project_path: provider_id | ''}
    #   Absent → follow model_default. '' → explicit native. Custom id →
    #   Settings.providers[id]. Today Claude uses this heavily; future: all
    #   harnesses may honor custom providers.
    provider_overrides: dict = field(default_factory=dict)
    # model_pins: {project_path: model_id}
    #   Absent → harness/provider default model. Present → adapters may pass
    #   -m (Grok/OpenCode today). Shared shape for future multi-harness picks.
    model_pins: dict = field(default_factory=dict)
    # tier_models: {provider_id: {tier: model_id | ''}}  — per-provider tier
    # assignments. Each custom provider carries its own Opus/Sonnet/Haiku/
    # Subagent/Fable mapping against ITS model list; '' = use the provider's
    # first model. Native ('') is never a key (native spawns inject no tier env).
    # See models.resolve_tier_model / build_spawn_env.
    tier_models: dict = field(default_factory=dict)
    # --- Classifier temperature (per-provider) ---
    # Only ``CLAUDE_CODE_AUTO_MODE_TEMPERATURE`` is live in Claude Code
    # v2.1.190+ (consumed via ``F2a()`` in both classifier stages). The other
    # classifier env vars are registered-but-inert dead entries, so PM exposes
    # only this slider. Temperature is per-provider because the active provider's
    # endpoint must be able to serve the classifier model.
    # classifier_temperature: {provider_id: float} — only emitted when the pid
    #   key exists (so 0.0 can be emitted explicitly; missing key = leave unset).
    classifier_temperature: dict = field(default_factory=dict)
    # --- Harness binary ---
    # Multi-harness: harnesses map + default/overrides. claude_binary migrates
    # into harnesses['claude']['binary'] on load. Legacy settings.json keys
    # agents / agent_default / agent_overrides are dual-read in load().
    harnesses: dict = field(default_factory=dict)
    harness_default: str = 'claude'
    harness_overrides: dict = field(default_factory=dict)
    # --- Host axis (remote SSH projects) ---
    # hosts: {host_id: HostProfile-as-dict} — remotes only; localhost is built-in.
    hosts: dict = field(default_factory=dict)
    # host_section_expanded: {host_id: bool} — legacy expand state (migrated to mode).
    host_section_expanded: dict = field(default_factory=dict)
    # host_section_mode: {host_id: 'all'|'active'|'hidden'}
    #   all    — section expanded, all projects visible
    #   active — section expanded, only attached/detached projects
    #   hidden — section collapsed (header only)
    # Clicking the host title cycles hidden → active → all → hidden.
    host_section_mode: dict = field(default_factory=dict)
    # remote_health_interval_sec: poll interval for remote health; 0 = off.
    remote_health_interval_sec: int = 30

    @property
    def resolved_projects_dir(self) -> str:
        return os.path.expanduser(self.projects_dir)

    @property
    def resolved_claude_binary(self) -> str:
        """The claude binary path: harnesses['claude']['binary'] wins when set,
        otherwise the legacy ``claude_binary`` key, otherwise 'claude'."""
        claude_cfg = self.harnesses.get('claude') if isinstance(self.harnesses, dict) else None
        if isinstance(claude_cfg, dict):
            from_harnesses = (claude_cfg.get('binary') or '').strip()
            if from_harnesses:
                return from_harnesses
        return self.claude_binary.strip() or 'claude'

    def effective_harness(self, project_path: str = '', host_id: str = 'localhost') -> str:
        """Return the harness id for a project.

        Per-project override wins over the global default. Empty override means
        use the default. Keys may be bare paths (legacy) or project_ref form.
        """
        from hosts import lookup_override, LOCALHOST_ID
        hid = host_id or LOCALHOST_ID
        val, found = lookup_override(self.harness_overrides, hid, project_path)
        if found:
            return val or self.harness_default
        return self.harness_default

    def effective_provider(self, project_path: str = '', host_id: str = 'localhost') -> str:
        """Return the active provider_id for a project ('' = native).

        Per-project ``provider_overrides`` wins when the key is present.
        ``''`` explicitly pins native. A non-empty pin that is not a known
        provider id is treated as stale (fall back to ``model_default``).
        """
        from hosts import lookup_override, LOCALHOST_ID
        hid = host_id or LOCALHOST_ID
        val, found = lookup_override(
            self.provider_overrides if isinstance(self.provider_overrides, dict)
            else None,
            hid, project_path,
        )
        if found:
            if not isinstance(val, str):
                return self.model_default or ''
            if val == '':
                return ''
            if isinstance(self.providers, dict) and val in self.providers:
                return val
            return self.model_default or ''
        return self.model_default or ''

    def effective_model(self, project_path: str = '', host_id: str = 'localhost') -> str:
        """Per-project model pin (Grok/OpenCode ``-m`` today; harness-agnostic).

        Reads ``model_pins`` only — never the provider axis.
        """
        from hosts import lookup_override, LOCALHOST_ID
        hid = host_id or LOCALHOST_ID
        val, found = lookup_override(
            self.model_pins if isinstance(self.model_pins, dict) else None,
            hid, project_path,
        )
        if found and isinstance(val, str) and val:
            return val
        return ''

    def model_axis_signature(self, project_path: str = '') -> str:
        """Opaque spawn-time signature for restart-staleness checks.

        Claude today is provider-shaped; Grok/OpenCode are model-pin-shaped.
        Storage remains dual-axis so future multi-harness custom providers can
        use both without another settings rewrite.
        """
        if self.effective_harness(project_path) == 'claude':
            return self.effective_provider(project_path)
        return self.effective_model(project_path)

    def uses_custom_provider(self, project_path: str = '') -> bool:
        """True if the effective provider for this project has a base_url
        (i.e. spawn needs env injection rather than native Anthropic)."""
        pid = self.effective_provider(project_path)
        if not pid:
            return False
        prov = self.providers.get(pid) if isinstance(self.providers, dict) else None
        return isinstance(prov, dict) and bool(prov.get('base_url'))

    def any_custom_provider_active(self) -> bool:
        """True if the global default or any per-project provider override
        names a provider that has a base_url (env injection on its spawns)."""
        candidates = [self.model_default]
        if isinstance(self.provider_overrides, dict):
            candidates.extend(self.provider_overrides.values())
        for pid in candidates:
            if pid and isinstance(self.providers, dict):
                prov = self.providers.get(pid)
                if isinstance(prov, dict) and prov.get('base_url'):
                    return True
        return False

    @classmethod
    def load(cls, path: str | None = None) -> 'Settings':
        if path is None:
            path = DEFAULT_SETTINGS_PATH
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return cls()
            # Dual-read legacy multi-harness key names (agent → harness).
            if 'harnesses' not in data and 'agents' in data:
                data['harnesses'] = data['agents']
            if 'harness_default' not in data and 'agent_default' in data:
                data['harness_default'] = data['agent_default']
            if 'harness_overrides' not in data and 'agent_overrides' in data:
                data['harness_overrides'] = data['agent_overrides']
            # Legacy dual-use model_overrides (pre-1.4.1) — migrate after construct.
            legacy_mo = data.get('model_overrides')
            known = {k: v for k, v in data.items()
                     if k in cls.__dataclass_fields__}
            inst = cls(**known)
            if isinstance(legacy_mo, dict):
                inst._legacy_model_overrides = legacy_mo
            inst._migrate_claude_binary()
            inst._migrate_old_model_shape()
            inst._migrate_host_axis()
            return inst
        except FileNotFoundError:
            # FB-7 (power #2): on a genuine first run (no settings.json yet),
            # PERSIST the defaults so the file exists from launch one — a user
            # who never opens Settings still gets a stable, inspectable config
            # (and tooling/back-ups have something to read). A corrupt/invalid
            # file (below) is NEVER overwritten — that path keeps the in-memory
            # defaults but leaves the on-disk file untouched for recovery.
            inst = cls()
            try:
                inst.save(path)
            except OSError:
                pass
            return inst
        except (json.JSONDecodeError, TypeError):
            return cls()

    def _migrate_claude_binary(self) -> None:
        """Mirror a legacy ``claude_binary`` into ``harnesses['claude']['binary']``.

        Idempotent and conservative: only fills the harnesses entry when the
        legacy key holds a non-empty value AND the harnesses entry isn't already
        set, so we never forge a misleading binary path or clobber a newer
        harnesses-side value. The old key is left intact (still honored when the
        harnesses map is absent — back-compat for any code/file that reads it).
        """
        if not isinstance(self.harnesses, dict):
            self.harnesses = {}
        legacy = (self.claude_binary or '').strip()
        if not legacy:
            return
        claude_cfg = self.harnesses.get('claude')
        if not isinstance(claude_cfg, dict):
            claude_cfg = {}
            self.harnesses['claude'] = claude_cfg
        if not (claude_cfg.get('binary') or '').strip():
            claude_cfg['binary'] = legacy

    def _migrate_old_model_shape(self) -> None:
        """Migrate the pre-pivot model layer to the new provider-axis shape.

        Pre-pivot (removed in the Claude-Only pivot):
          * providers[*]['models'] was a dict ``{model_id: {"name": str}}`` with
            a sibling ``transformer``; now ``models`` is a list of free-text
            strings and ``transformer`` is gone.
          * model_default held ``'provider/model'`` strings; now bare provider
            ids ('' = native). Legacy ``model_overrides`` dual-use map is split
            into ``provider_overrides`` + ``model_pins`` (1.4.1).
          * tier_models did not exist; a pre-pivot ``'provider/model'`` default
            is split into ``model_default=provider`` + a global tier pin.
          * ccr_* fields existed; dropped by known-field filter. harness_default /
            harness_overrides are retained for multi-harness, so the ``known``-field filter already
            dropped them — nothing to do here for those.

        Defensive throughout: a malformed old shape degrades to the new default
        for that field rather than raising. Idempotent: a file already in the
        new shape passes through unchanged.
        """
        if not isinstance(self.providers, dict):
            self.providers = {}
        for pid, prov in list(self.providers.items()):
            if not isinstance(prov, dict):
                continue
            models = prov.get('models')
            if isinstance(models, dict):
                # Old shape: {model_id: {"name": str}} → [model_id, ...].
                prov['models'] = list(models.keys())
            elif isinstance(models, list):
                prov['models'] = [str(m) for m in models]
            elif models is None:
                prov['models'] = []
            else:
                prov['models'] = []
            prov.pop('transformer', None)
            # Normalize optional max_context_tokens (positive int or drop).
            mct = prov.get('max_context_tokens', None)
            if mct is None or mct == '':
                prov.pop('max_context_tokens', None)
            elif isinstance(mct, bool):
                prov.pop('max_context_tokens', None)
            elif isinstance(mct, int) and mct > 0:
                prov['max_context_tokens'] = mct
            elif isinstance(mct, float) and mct > 0 and mct == int(mct):
                prov['max_context_tokens'] = int(mct)
            elif isinstance(mct, str) and mct.strip().isdigit():
                n = int(mct.strip())
                if n > 0:
                    prov['max_context_tokens'] = n
                else:
                    prov.pop('max_context_tokens', None)
            else:
                prov.pop('max_context_tokens', None)

        # model_default: 'provider/model' → provider; ''/bare-id stay.
        if isinstance(self.model_default, str) and '/' in self.model_default:
            pid, mid = self.model_default.split('/', 1)
            self.model_default = pid
            if not isinstance(self.tier_models, dict):
                self.tier_models = {}
            sub = self.tier_models.setdefault(pid, {})
            if not isinstance(sub, dict):
                sub = {}
                self.tier_models[pid] = sub
            for tier in TIERS:
                sub.setdefault(tier, mid)
        elif not isinstance(self.model_default, str):
            self.model_default = ''

        # Split legacy dual-use model_overrides → provider_overrides + model_pins.
        self._migrate_provider_model_axes()

        # tier_models: migrate the legacy GLOBAL shape {tier: model_id} → the
        # per-provider shape {provider_id: {tier: model_id}}. The legacy dict is
        # detected by having string values. Fold it into one provider: the
        # default if it's custom, else the first custom provider, else drop
        # (the values were inert under native anyway).
        if isinstance(self.tier_models, dict) and self.tier_models and any(
                isinstance(v, str) for v in self.tier_models.values()):
            legacy = {t: v for t, v in self.tier_models.items()
                      if isinstance(v, str)}
            target = ''
            if (self.model_default and isinstance(self.providers, dict)
                    and self.model_default in self.providers):
                target = self.model_default
            elif isinstance(self.providers, dict) and self.providers:
                target = sorted(self.providers)[0]
            self.tier_models = ({target: {tier: legacy.get(tier, '')
                                          for tier in TIERS}}
                                if target else {})
        # Normalize + scrub per-provider (see _normalize_tier_models).
        self._normalize_tier_models()
        # One-shot: preserve the old spawn-time auto-[1m] for bare ids that
        # matched glm|deepseek (Opus/Fable only at the time). Runs after tier
        # scrub so pins still match the pre-suffix model list, then both the
        # models list and matching tier pins are rewritten together. After
        # this, the UI 1M toggle owns the flag; spawn emits stored ids verbatim.
        self._migrate_legacy_1m_model_ids()
        self._normalize_classifier_temperature()

    def _migrate_provider_model_axes(self) -> None:
        """Normalize provider_overrides + model_pins; split legacy model_overrides.

        Legacy ``model_overrides`` mixed Claude provider ids and Grok/OC ``-m``
        strings. Split rule (after providers are list-normalized):

          * ``''`` or value in ``providers`` → ``provider_overrides``
          * else → ``model_pins`` (model-shaped ids, including ``provider/model``)

        Also accepts a temporary ``_legacy_model_overrides`` attr set by
        :meth:`load` (not a dataclass field). Idempotent.
        """
        if not isinstance(self.provider_overrides, dict):
            self.provider_overrides = {}
        if not isinstance(self.model_pins, dict):
            self.model_pins = {}

        def _str_map(raw):
            out = {}
            if not isinstance(raw, dict):
                return out
            for path, val in raw.items():
                if isinstance(val, str):
                    out[path] = val
            return out

        self.provider_overrides = _str_map(self.provider_overrides)
        self.model_pins = _str_map(self.model_pins)

        legacy = getattr(self, '_legacy_model_overrides', None)
        if isinstance(legacy, dict) and legacy:
            providers = self.providers if isinstance(self.providers, dict) else {}
            # Prefer explicit new-map keys; only fill paths still unset.
            for path, val in legacy.items():
                if not isinstance(val, str):
                    continue
                if path in self.provider_overrides or path in self.model_pins:
                    continue
                if val == '' or val in providers:
                    self.provider_overrides[path] = val
                else:
                    # Model-shaped (incl. opencode ``provider/model`` -m ids).
                    # Ancient Claude ``provider/model`` slash forms land here
                    # too if never re-saved as bare provider ids — re-pick once.
                    self.model_pins[path] = val
        if hasattr(self, '_legacy_model_overrides'):
            del self._legacy_model_overrides

        # Drop empty model pins; keep '' only on provider_overrides (native pin).
        self.model_pins = {p: v for p, v in self.model_pins.items() if v}

    def _normalize_tier_models(self) -> None:
        """Normalize ``tier_models`` to ``{provider_id: {tier: model_id|''}}``.

        Drops entries for unknown provider ids; for each kept provider, ensures
        a dict with exactly the TIERS keys (str or ''), drops stray tier keys,
        and scrubs any tier value not on that provider's model list to ''.
        Idempotent and defensive against malformed shapes. Does NOT create
        entries for providers that have none — missing == all-default (the
        spawn path reads ``tier_models.get(pid, {})``).
        """
        if not isinstance(self.tier_models, dict) or not isinstance(self.providers, dict):
            self.tier_models = {}
            return
        for pid in list(self.tier_models.keys()):
            if pid not in self.providers:
                self.tier_models.pop(pid, None)
                continue
            sub = self.tier_models.get(pid)
            if not isinstance(sub, dict):
                sub = {}
            prov = self.providers.get(pid)
            models = ([m for m in prov.get('models', []) if isinstance(m, str)]
                      if isinstance(prov, dict) else [])
            new_sub = {}
            for tier in TIERS:
                v = sub.get(tier, '')
                if not isinstance(v, str):
                    v = ''
                elif v and v not in models:
                    v = ''
                new_sub[tier] = v
            self.tier_models[pid] = new_sub

    def _migrate_legacy_1m_model_ids(self) -> None:
        """Append ``[1m]`` to bare model ids that the old spawn hardcode would
        have rewritten for Opus/Fable (ids matching glm|deepseek).

        Idempotent: already-suffixed ids and non-matching ids are left alone.
        Rewrites tier_models pins that still name the bare id so they keep
        resolving after the models list is updated.
        """
        if not isinstance(self.providers, dict):
            return
        for pid, prov in self.providers.items():
            if not isinstance(prov, dict):
                continue
            models = prov.get('models')
            if not isinstance(models, list):
                continue
            rewritten = {}
            new_models = []
            for mid in models:
                if not isinstance(mid, str):
                    continue
                if (not mid.endswith('[1m]')
                        and _LEGACY_1M_MODEL_RE.search(mid)):
                    new_mid = f'{mid}[1m]'
                    rewritten[mid] = new_mid
                    new_models.append(new_mid)
                else:
                    new_models.append(mid)
            if not rewritten:
                continue
            # Deduplicate while preserving order (bare + already-suffixed → one).
            seen = set()
            deduped = []
            for m in new_models:
                if m not in seen:
                    seen.add(m)
                    deduped.append(m)
            prov['models'] = deduped
            if not isinstance(self.tier_models, dict):
                continue
            sub = self.tier_models.get(pid)
            if not isinstance(sub, dict):
                continue
            for tier in TIERS:
                v = sub.get(tier, '')
                if isinstance(v, str) and v in rewritten:
                    sub[tier] = rewritten[v]

    def _normalize_classifier_temperature(self) -> None:
        """Normalize the per-provider classifier-temperature dict.

        Keeps only known providers with finite numeric temperatures. Missing
        provider keys mean "leave unset" for the spawn path.
        """
        if not isinstance(self.classifier_temperature, dict):
            self.classifier_temperature = {}
            return
        if not isinstance(self.providers, dict):
            self.classifier_temperature = {}
            return
        for pid in list(self.classifier_temperature.keys()):
            if pid not in self.providers:
                self.classifier_temperature.pop(pid, None)
                continue
            v = self.classifier_temperature[pid]
            if not isinstance(v, (int, float)) or not __import__('math').isfinite(v):
                self.classifier_temperature.pop(pid, None)

    def _migrate_host_axis(self) -> None:
        """Normalize host-axis fields and project_ref keys on override maps.

        * ``hosts`` — keep only valid HostProfile dicts (via hosts.parse_hosts_map).
        * ``host_section_expanded`` — bool map; drop non-str keys / non-bool values.
        * ``remote_health_interval_sec`` — int >= 0; default 30 if garbage.
        * Override maps (harness/provider/model) — normalize to project_ref keys.
        """
        from hosts import (
            parse_hosts_map, hosts_to_settings_dict, migrate_override_map,
            LOCALHOST_ID,
        )
        parsed = parse_hosts_map(self.hosts)
        self.hosts = hosts_to_settings_dict(parsed)

        if not isinstance(self.host_section_expanded, dict):
            self.host_section_expanded = {}
        else:
            cleaned = {}
            for k, v in self.host_section_expanded.items():
                if isinstance(k, str) and isinstance(v, bool):
                    cleaned[k] = v
            self.host_section_expanded = cleaned

        try:
            iv = int(self.remote_health_interval_sec)
            if iv < 0:
                iv = 30
            self.remote_health_interval_sec = iv
        except (TypeError, ValueError):
            self.remote_health_interval_sec = 30

        self.harness_overrides = migrate_override_map(self.harness_overrides)
        self.provider_overrides = migrate_override_map(self.provider_overrides)
        self.model_pins = migrate_override_map(self.model_pins)

        if LOCALHOST_ID not in self.host_section_expanded:
            self.host_section_expanded[LOCALHOST_ID] = True

        # Migrate expand bools → section mode when mode map is empty/missing keys.
        if not isinstance(self.host_section_mode, dict):
            self.host_section_mode = {}
        cleaned_modes = {}
        for k, v in self.host_section_mode.items():
            if isinstance(k, str) and v in ('all', 'active', 'hidden'):
                cleaned_modes[k] = v
        self.host_section_mode = cleaned_modes
        for hid, expanded in self.host_section_expanded.items():
            if isinstance(hid, str) and hid not in self.host_section_mode:
                self.host_section_mode[hid] = 'all' if expanded else 'hidden'
        if LOCALHOST_ID not in self.host_section_mode:
            self.host_section_mode[LOCALHOST_ID] = 'all'

    def host_profiles(self) -> dict:
        """Return ``{id: HostProfile}`` for configured remotes (not localhost)."""
        from hosts import parse_hosts_map
        return parse_hosts_map(self.hosts)

    def section_mode(self, host_id: str) -> str:
        """``all`` | ``active`` | ``hidden`` for a host section (default ``all``)."""
        if not isinstance(self.host_section_mode, dict):
            return 'all'
        mode = self.host_section_mode.get(host_id, 'all')
        return mode if mode in ('all', 'active', 'hidden') else 'all'

    def set_section_mode(self, host_id: str, mode: str) -> None:
        if mode not in ('all', 'active', 'hidden'):
            mode = 'all'
        if not isinstance(self.host_section_mode, dict):
            self.host_section_mode = {}
        self.host_section_mode[host_id] = mode
        # Keep legacy expand map in sync for older readers.
        if not isinstance(self.host_section_expanded, dict):
            self.host_section_expanded = {}
        self.host_section_expanded[host_id] = mode != 'hidden'

    def section_expanded(self, host_id: str) -> bool:
        """Whether the sidebar section for host_id shows any projects."""
        return self.section_mode(host_id) != 'hidden'

    def set_section_expanded(self, host_id: str, expanded: bool) -> None:
        """Legacy API: map bool expand onto all/hidden (preserves active)."""
        if expanded:
            if self.section_mode(host_id) == 'hidden':
                self.set_section_mode(host_id, 'all')
        else:
            self.set_section_mode(host_id, 'hidden')

    def save(self, path: str | None = None) -> None:
        if path is None:
            path = DEFAULT_SETTINGS_PATH
        dir_path = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp_path, path)
            # settings.json holds provider API keys in cleartext — keep it private.
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise