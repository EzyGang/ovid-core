class OvidCoreError(Exception):
    pass


class ConfigurationError(OvidCoreError):
    pass


class CredentialError(OvidCoreError):
    pass


class CodexAuthError(CredentialError):
    pass


class ProviderError(OvidCoreError):
    pass


class ModelResolutionError(OvidCoreError):
    pass


class AgentConstructionError(OvidCoreError):
    pass


class AgentRunError(OvidCoreError):
    pass


class AgentTimeoutError(AgentRunError, TimeoutError):
    pass


class ToolError(OvidCoreError):
    pass


class ToolValidationError(ToolError):
    pass


class ToolExecutionError(ToolError):
    pass


class ToolTimeoutError(ToolExecutionError, TimeoutError):
    pass


class ExtensionCollisionError(AgentConstructionError):
    pass


class PluginError(OvidCoreError):
    pass


class TransportError(OvidCoreError):
    pass
