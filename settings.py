import os
import json
import tempfile
from dataclasses import dataclass, asdict


DEFAULT_SETTINGS_PATH = os.path.expanduser('~/.ProjectMan/settings.json')


@dataclass
class Settings:
    projects_dir: str = '~/.ProjectMan/Projects'
    claude_binary: str = ''
    resume_projects: bool = True
    font_size: int = 11
    scrollback_lines: int = 10000
    audible_bell: bool = False
    multiplexer: str = 'none'

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
