from pathlib import Path

from ovid_core.errors import ConfigurationError
from ovid_core.models import BaseModel


type ConfigPath = tuple[str | int, ...]


class ConfigIssue(BaseModel):
    path: ConfigPath
    message: str
    source_file: Path | None = None

    def __str__(self) -> str:
        path = '.'.join(str(part) for part in self.path) or '<root>'
        source = f' in {self.source_file}' if self.source_file is not None else ''

        return f'{path}{source}: {self.message}'


class ConfigValidationError(ConfigurationError):
    def __init__(self, issues: tuple[ConfigIssue, ...]) -> None:
        self.issues = issues
        super().__init__('; '.join(str(issue) for issue in issues))
