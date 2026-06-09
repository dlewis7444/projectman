import os
import json
import tempfile
from dataclasses import dataclass, asdict, field


DEFAULT_SETTINGS_PATH = os.path.expanduser('~/.ProjectMan/settings.json')


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
    # --- Model-agnostic / claude-code-router (ccr) ---
    # providers: {provider_id: {"name": str, "base_url": str, "api_key": str,
    #                           "transformer": str|None,
    #                           "models": {model_id: {"name": str}}}}
    providers: dict = field(default_factory=dict)
    model_default: str = ''      # '' = Anthropic native; else 'provider_id/model_id'
    # model_overrides: {project_path: 'provider_id/model_id' | ''}
    model_overrides: dict = field(default_factory=dict)
    ccr_managed: bool = True
    ccr_host: str = '127.0.0.1'
    ccr_port: int = 3456
    ccr_api_key: str = ''        # ccr APIKEY + injected auth token; auto-minted if blank
    ccr_binary: str = ''
    # --- Agent-agnostic ---
    # agents: {agent_id: {"binary": str, ...}}. claude_binary migrates into
    #         agents['claude']['binary'] on load; the old key stays honored
    #         when agents is absent (existing user files load unchanged).
    agents: dict = field(default_factory=dict)
    agent_default: str = 'claude'        # global default agent id
    # agent_overrides: {project_path: agent_id}
    agent_overrides: dict = field(default_factory=dict)

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
        """Return the agent id for a project (mirrors ``effective_model``).

        A per-project override takes precedence over the global default. An
        override stored as '' means 'use the default', not 'no agent'.
        """
        if project_path and project_path in self.agent_overrides:
            return self.agent_overrides[project_path] or self.agent_default
        return self.agent_default

    @property
    def resolved_ccr_binary(self) -> str:
        return (self.ccr_binary or '').strip() or 'ccr'

    def effective_model(self, project_path: str = '') -> str:
        """Return the 'provider/model' id for a project ('' = Anthropic native).

        A per-project override takes precedence over the global default. An
        override stored as '' explicitly pins that project to native Claude.
        """
        if project_path and project_path in self.model_overrides:
            return self.model_overrides[project_path] or ''
        return self.model_default or ''

    def uses_custom_model(self, project_path: str = '') -> bool:
        """True if the effective model for this project needs ccr."""
        model = self.effective_model(project_path)
        if not model or '/' not in model:
            return False
        provider = model.split('/', 1)[0]
        return provider in self.providers

    def any_custom_model_active(self) -> bool:
        """True if the global default or any per-project override needs ccr."""
        candidates = [self.model_default]
        candidates.extend(self.model_overrides.values())
        for model in candidates:
            if model and '/' in model and model.split('/', 1)[0] in self.providers:
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
            return inst
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
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
