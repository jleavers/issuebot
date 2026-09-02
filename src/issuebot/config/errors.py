"""Typed configuration errors. Every error has a stable ``code`` for logs and tests."""

from pathlib import Path


class ConfigError(Exception):
    code: str = "config_error"

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def __str__(self) -> str:
        prefix = f"{self.path}: " if self.path is not None else ""
        return f"{prefix}{self.message}"


class MissingWorkflowFile(ConfigError):  # noqa: N818
    code = "missing_workflow_file"


class WorkflowParseError(ConfigError):
    code = "workflow_parse_error"


class FrontMatterNotAMap(ConfigError):  # noqa: N818
    code = "workflow_front_matter_not_a_map"


class MissingEnvironmentVariable(ConfigError):  # noqa: N818
    code = "missing_environment_variable"

    def __init__(self, *, variable: str, field: str, path: Path | None = None) -> None:
        super().__init__(f"{field} references ${variable}, which is unset or empty", path=path)
        self.variable = variable
        self.field = field


class SettingsValidationError(ConfigError):
    code = "invalid_settings"

    def __init__(self, errors: list[tuple[str, str]], *, path: Path | None = None) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} invalid setting(s)", path=path)

    def __str__(self) -> str:
        lines = [super().__str__()]
        lines.extend(f"  {field}: {message}" for field, message in self.errors)
        return "\n".join(lines)
