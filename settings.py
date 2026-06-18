import os
import json
import tempfile
from dataclasses import dataclass, asdict, field


DEFAULT_SETTINGS_PATH = os.path.expanduser('~/.ProjectMan/settings.json')

# The four Claude Code model tiers. PM can pin each independently to any model
# defined on the active provider (free-text ids), or leave a tier on '' to use
# the provider's first/default model. See models.build_spawn_env.
TIERS = ('opus', 'sonnet', 'haiku', 'subagent')


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
    #                           "models": [str, ...]}}
    #   ``models`` is a LIST of free-text model-id strings (CC strips a trailing
    #   ``[1m]`` itself, so ids like ``glm-5.2:cloud[1m]`` are valid verbatim).
    providers: dict = field(default_factory=dict)
    # model_default: provider_id | ''  — '' = Anthropic (native). The active
    # provider whose base_url receives every tier's model id (CC's
    # ANTHROPIC_BASE_URL is process-wide, so a session mixes model NAMES across
    # tiers but never providers).
    model_default: str = ''
    # model_overrides: {project_path: provider_id | ''}  — per-project provider
    # override ('' pins that project to native; absent = follow model_default).
    model_overrides: dict = field(default_factory=dict)
    # tier_models: {tier: model_id | ''}  — global per-tier assignment against the
    # active provider's model list. '' = use the provider's first model.
    tier_models: dict = field(default_factory=dict)
    # --- Harness binary ---
    # Claude Code is the sole harness. agents['claude']['binary'] is the live
    # value; the legacy ``claude_binary`` key is kept as a fallback for older
    # settings files (mirrored on load by _migrate_claude_binary).
    agents: dict = field(default_factory=dict)

    @property
    def resolved_projects_dir(self) -> str:
        return os.path.expanduser(self.projects_dir)

    @property
    def resolved_claude_binary(self) -> str:
        """The claude binary path: agents['claude']['binary'] wins when set,
        otherwise the legacy ``claude_binary`` key, otherwise 'claude'."""
        claude_cfg = self.agents.get('claude') if isinstance(self.agents, dict) else None
        if isinstance(claude_cfg, dict):
            from_agents = (claude_cfg.get('binary') or '').strip()
            if from_agents:
                return from_agents
        return self.claude_binary.strip() or 'claude'

    def effective_agent(self, project_path: str = '') -> str:
        """Return the harness id for a project.

        Claude Code is the sole harness, so this is always ``'claude'``. Kept as
        a method (not a constant) so terminal.py/window.py/sidebar.py callers
        keep working unchanged — the agent concept is renamed "harness" in
        user-facing UI, but the Python symbol stays (don't rename symbols).
        """
        return 'claude'

    def effective_provider(self, project_path: str = '') -> str:
        """Return the active provider_id for a project ('' = Anthropic native).

        A per-project override takes precedence over the global default. An
        override stored as '' explicitly pins that project to native.
        """
        if project_path and project_path in self.model_overrides:
            return self.model_overrides[project_path] or ''
        return self.model_default or ''

    def uses_custom_provider(self, project_path: str = '') -> bool:
        """True if the effective provider for this project has a base_url
        (i.e. spawn needs env injection rather than native Anthropic)."""
        pid = self.effective_provider(project_path)
        if not pid:
            return False
        prov = self.providers.get(pid) if isinstance(self.providers, dict) else None
        return isinstance(prov, dict) and bool(prov.get('base_url'))

    def any_custom_provider_active(self) -> bool:
        """True if the global default or any per-project override names a
        provider that has a base_url (env injection will run on its spawns)."""
        candidates = [self.model_default]
        if isinstance(self.model_overrides, dict):
            candidates.extend(self.model_overrides.values())
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
            known = {k: v for k, v in data.items()
                     if k in cls.__dataclass_fields__}
            inst = cls(**known)
            inst._migrate_claude_binary()
            inst._migrate_old_model_shape()
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
        """Mirror a legacy ``claude_binary`` into ``agents['claude']['binary']``.

        Idempotent and conservative: only fills the agents entry when the
        legacy key holds a non-empty value AND the agents entry isn't already
        set, so we never forge a misleading binary path or clobber a newer
        agents-side value. The old key is left intact (still honored when the
        agents map is absent — back-compat for any code/file that reads it).
        """
        if not isinstance(self.agents, dict):
            self.agents = {}
        legacy = (self.claude_binary or '').strip()
        if not legacy:
            return
        claude_cfg = self.agents.get('claude')
        if not isinstance(claude_cfg, dict):
            claude_cfg = {}
            self.agents['claude'] = claude_cfg
        if not (claude_cfg.get('binary') or '').strip():
            claude_cfg['binary'] = legacy

    def _migrate_old_model_shape(self) -> None:
        """Migrate the pre-pivot model layer to the new provider-axis shape.

        Pre-pivot (removed in the Claude-Only pivot):
          * providers[*]['models'] was a dict ``{model_id: {"name": str}}`` with
            a sibling ``transformer``; now ``models`` is a list of free-text
            strings and ``transformer`` is gone.
          * model_default / model_overrides held ``'provider/model'`` strings;
            now they hold bare provider ids ('' = native).
          * tier_models did not exist; a pre-pivot ``'provider/model'`` default
            is split into ``model_default=provider`` + a global tier pin.
          * ccr_* / agent_default / agent_overrides fields existed; they are no
            longer dataclass fields, so the ``known``-field filter already
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

        # model_default: 'provider/model' → provider; ''/bare-id stay.
        if isinstance(self.model_default, str) and '/' in self.model_default:
            pid, mid = self.model_default.split('/', 1)
            self.model_default = pid
            if not isinstance(self.tier_models, dict):
                self.tier_models = {}
            for tier in TIERS:
                self.tier_models.setdefault(tier, mid)
        elif not isinstance(self.model_default, str):
            self.model_default = ''

        # model_overrides: 'provider/model' → provider; '' stays; non-str dropped.
        if isinstance(self.model_overrides, dict):
            new_overrides = {}
            for path, val in self.model_overrides.items():
                if not isinstance(val, str):
                    continue
                new_overrides[path] = val.split('/', 1)[0] if '/' in val else val
            self.model_overrides = new_overrides
        else:
            self.model_overrides = {}

        # Scrub tier_models: only keep a value if it's a model on the active
        # provider; otherwise reset to ''. Native (model_default=='') → all ''.
        if not isinstance(self.tier_models, dict):
            self.tier_models = {}
        active_models = []
        if self.model_default and isinstance(self.providers, dict):
            prov = self.providers.get(self.model_default)
            if isinstance(prov, dict):
                active_models = [m for m in prov.get('models', [])
                                 if isinstance(m, str)]
        for tier in TIERS:
            val = self.tier_models.get(tier, '')
            if not isinstance(val, str):
                self.tier_models[tier] = ''
            elif val and val not in active_models:
                self.tier_models[tier] = ''
            else:
                self.tier_models[tier] = val
        # Drop any stray tier keys outside the canonical four.
        for stray in list(self.tier_models.keys()):
            if stray not in TIERS:
                self.tier_models.pop(stray, None)

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