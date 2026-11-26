from typing import Literal

from pydantic import JsonValue


class OvidCoreError(Exception):
    pass


class ConfigurationError(OvidCoreError):
    pass


class CredentialError(OvidCoreError):
    pass


class AuthenticationError(CredentialError):
    pass


class CodexAuthError(AuthenticationError):
    pass


class ProviderError(OvidCoreError):
    pass


class PersistenceError(OvidCoreError):
    pass


class ModelResolutionError(OvidCoreError):
    pass


class AgentConstructionError(OvidCoreError):
    pass


class AgentRunError(OvidCoreError):
    pass


class AgentTimeoutError(AgentRunError, TimeoutError):
    pass


class UsageLimitError(AgentRunError):
    pass


class ContextWindowError(UsageLimitError):
    pass


class AgentRunInputError(AgentRunError):
    pass


class DeferredToolAnswerError(AgentRunInputError):
    pass


class NothingToResumeError(AgentRunInputError):
    pass


class ResumeLimitError(AgentRunInputError):
    """Refuse to resume a conversation whose runs stopped too many times in a row."""

    def __init__(self, count: int) -> None:
        super().__init__(f'Run was interrupted {count} times. Send a new prompt.')


class ToolError(OvidCoreError):
    pass


class ToolValidationError(ToolError):
    pass


class ToolExecutionError(ToolError):
    pass


class ToolRetryError(ToolExecutionError):
    pass


class ToolTimeoutError(ToolExecutionError, TimeoutError):
    pass


class ToolApprovalRequired(OvidCoreError):
    """Ask the application to approve the current tool call before the tool runs it."""

    def __init__(self, metadata: dict[str, JsonValue] | None = None) -> None:
        super().__init__('Tool call requires approval')
        self.metadata = metadata


class ToolDeferred(OvidCoreError):
    """Give the current tool call to the application, which supplies its result in a later run."""

    def __init__(
        self,
        metadata: dict[str, JsonValue] | None = None,
        *,
        kind: Literal['external', 'question'] = 'external',
    ) -> None:
        super().__init__('Tool call is deferred')
        self.metadata = metadata
        self.kind: Literal['external', 'question'] = kind


class ExtensionCollisionError(AgentConstructionError):
    pass


class PluginError(OvidCoreError):
    pass


class TransportError(OvidCoreError):
    pass


class ServerConstructionError(TransportError):
    pass
