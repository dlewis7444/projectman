import os
import json
import tempfile
from dataclasses import dataclass, asdict


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

    @property
    def resolved_projects_dir(self) -> str:
        return os.path.expanduser(self.projects_dir)

    @property
    def resolved_claude_binary(self) -> str:
        return self.claude_binary.strip() or 'claude'

    @classmethod
    def load(cls, path: str = DEFAULT_SETTINGS_PATH) -> 'Settings':
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            known = {k: v for k, v in data.items()
                     if k in cls.__dataclass_fields__}
            return cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: str = DEFAULT_SETTINGS_PATH) -> None:
        dir_path = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
